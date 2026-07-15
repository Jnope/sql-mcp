import os
import json
import signal
import logging
import pandas as pd
import numpy as np
from transwarp.timelyre import DatabaseConn
from .sql_validator import validate_readonly
from .config import (
    TIMELYRE_LOGIN_TIMEOUT, TIMELYRE_PROXY, TIMELYRE_CONN, TIMELYRE_DEFAULT_DB,
    TIMELYRE_SESSION_TIMEOUT, TIMELYRE_USER, TIMELYRE_PASSWORD, TIMELYRE_TOKEN,
    PYTHON_TIMEOUT,
)

logger = logging.getLogger(__name__)

SAFE_BUILTINS = {
    "abs": abs,
    "all": all,
    "any": any,
    "bool": bool,
    "dict": dict,
    "enumerate": enumerate,
    "filter": filter,
    "float": float,
    "int": int,
    "len": len,
    "list": list,
    "map": map,
    "max": max,
    "min": min,
    "print": print,
    "range": range,
    "round": round,
    "set": set,
    "sorted": sorted,
    "str": str,
    "sum": sum,
    "tuple": tuple,
    "type": type,
    "zip": zip,
    "isinstance": isinstance,
    "json": json,
}


class _TimeoutError(Exception):
    pass


def _timeout_handler(signum, frame):
    raise _TimeoutError(f"Python execution exceeded {PYTHON_TIMEOUT}s")


class Executor:
    _instance: "Executor | None" = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._conns: dict[str, DatabaseConn] = {}
        return cls._instance

    def __init__(self):
        pass

    def _conn_key(self, schema_name: str, db: str) -> str:
        return f"{schema_name}:{db}"

    @staticmethod
    def _get_conn_info(schema_name: str) -> dict:
        prefix = f"TIMELYRE_PROXY_{schema_name.upper()}"
        proxy = os.environ.get(prefix)
        if not proxy:
            logger.warning("No env %s for %s, falling back to defaults", prefix, schema_name)
            return {
                "jdbc_http_proxy": TIMELYRE_PROXY,
                "real_conn": TIMELYRE_CONN,
                "db_user": TIMELYRE_USER,
                "db_password": TIMELYRE_PASSWORD,
                "db_token": TIMELYRE_TOKEN,
            }
        conn = os.environ.get(f"TIMELYRE_CONN_{schema_name.upper()}", proxy)
        user = os.environ.get(f"TIMELYRE_USER_{schema_name.upper()}", "")
        password = os.environ.get(f"TIMELYRE_PASSWORD_{schema_name.upper()}", "")
        token = os.environ.get(f"TIMELYRE_TOKEN_{schema_name.upper()}", "")
        return {
            "jdbc_http_proxy": proxy,
            "real_conn": conn,
            "db_user": user,
            "db_password": password,
            "db_token": token,
        }

    def db_conn(self, schema_name: str, db: str | None = None) -> DatabaseConn:
        db = db or TIMELYRE_DEFAULT_DB
        key = self._conn_key(schema_name, db)
        if key not in self._conns:
            info = self._get_conn_info(schema_name)
            kwargs = dict(
                jdbc_http_proxy=info["jdbc_http_proxy"],
                real_conn=info["real_conn"],
                db=db,
                auth_type="ldap",
                disable_cancel=True,
                session_timeout=TIMELYRE_SESSION_TIMEOUT,
                login_timeout=TIMELYRE_LOGIN_TIMEOUT,
            )
            if info.get("db_user"):
                kwargs["username"] = info["db_user"]
            if info.get("db_password"):
                kwargs["password"] = info["db_password"]
            if info.get("db_token"):
                kwargs["token"] = info["db_token"]
            self._conns[key] = DatabaseConn(**kwargs)
        return self._conns[key]

    def execute_sql(self, sql: str, schema_name: str = "", db: str | None = None) -> dict:
        logger.info("Executing SQL on %s.%s: %s", schema_name, db or TIMELYRE_DEFAULT_DB, sql)

        conn = self.db_conn(schema_name, db)
        df = conn.query_raw_data(sql=sql)

        if df is None:
            return {"columns": [], "rows": [], "rowCount": 0}

        if isinstance(df, pd.DataFrame):
            return self._df_to_result(df)

        if isinstance(df, list):
            if len(df) == 0:
                return {"columns": [], "rows": [], "rowCount": 0}
            first = df[0]
            if isinstance(first, dict):
                columns = list(first.keys())
                rows = [[row.get(c) for c in columns] for row in df]
                return {"columns": columns, "rows": rows, "rowCount": len(rows)}
            return {"columns": [f"col_{i}" for i in range(len(first))], "rows": df, "rowCount": len(df)}

        return {"columns": ["result"], "rows": [[str(df)]], "rowCount": 1}

    def execute_python(self, code: str, data: str | None = None) -> dict:
        local_vars: dict = {}
        if data:
            parsed = json.loads(data)
            local_vars["df"] = pd.DataFrame(parsed.get("rows", []), columns=parsed.get("columns"))

        exec_globals = {
            "pd": pd,
            "np": np,
            "json": json,
            "__builtins__": SAFE_BUILTINS,
        }

        old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(PYTHON_TIMEOUT)
        try:
            exec(code, exec_globals, local_vars)
        except _TimeoutError:
            raise ValueError(f"Python执行超时({PYTHON_TIMEOUT}秒)")
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)

        result = local_vars.get("result")
        if result is None:
            for key in ("df", "result_df"):
                if key in local_vars and isinstance(local_vars[key], pd.DataFrame):
                    result = local_vars[key]
                    break

        if isinstance(result, pd.DataFrame):
            return self._df_to_result(result)

        if isinstance(result, (list, dict)):
            return {"value": json.dumps(result, ensure_ascii=False, default=str)}

        if isinstance(result, (int, float, str, bool)):
            return {"value": str(result)}

        return {"value": str(result) if result is not None else ""}

    def get_table_ddl(self, schema_name: str, db: str | None, table_name: str) -> str:
        db = db or TIMELYRE_DEFAULT_DB
        full_table = f"{schema_name}.{db}.{table_name}"
        try:
            conn = self.db_conn(schema_name, db)
            ddl_df = conn.run_sql(f"SHOW CREATE TABLE {full_table}")
            if ddl_df is None:
                return ""
            if isinstance(ddl_df, pd.DataFrame) and not ddl_df.empty:
                return str(ddl_df.iloc[0, 0])
            if isinstance(ddl_df, list) and len(ddl_df) > 0:
                if isinstance(ddl_df[0], dict):
                    return str(list(ddl_df[0].values())[0])
                return str(ddl_df[0])
            return str(ddl_df)
        except Exception as e:
            logger.warning("Failed to get DDL for %s: %s", full_table, e)
            return ""

    def list_databases(self, schema_name: str) -> list[str]:
        try:
            conn = self.db_conn(schema_name, TIMELYRE_DEFAULT_DB)
            result = conn.show_databases()
            if isinstance(result, list):
                return [str(r) for r in result]
            if isinstance(result, pd.DataFrame) and not result.empty:
                return result.iloc[:, 0].tolist()
            return []
        except Exception as e:
            logger.warning("Failed to list databases for %s: %s", schema_name, e)
            return []

    def list_tables(self, schema_name: str, db: str) -> list[str]:
        try:
            conn = self.db_conn(schema_name, db)
            result = conn.show_tables()
            if isinstance(result, list):
                return [str(r) for r in result]
            if isinstance(result, pd.DataFrame) and not result.empty:
                return result.iloc[:, 0].tolist()
            return []
        except Exception as e:
            logger.warning("Failed to list tables for %s.%s: %s", schema_name, db, e)
            return []

    def close(self):
        for key, conn in self._conns.items():
            try:
                conn.close_connection()
            except Exception:
                pass
        self._conns.clear()

    @staticmethod
    def _df_to_result(df: pd.DataFrame) -> dict:
        return {
            "columns": df.columns.tolist(),
            "rows": df.where(pd.notna(df), None).values.tolist(),
            "rowCount": len(df),
        }
