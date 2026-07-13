import os
import json
import signal
import logging
import pandas as pd
import numpy as np
from transwarp.timelyre import DatabaseConn
from .sql_validator import validate_readonly

logger = logging.getLogger(__name__)

TIMELYRE_PROXY = os.environ.get("TIMELYRE_PROXY", "http://127.0.0.1:8090")
TIMELYRE_CONN = os.environ.get("TIMELYRE_CONN", "127.0.0.1:8090")
TIMELYRE_DB = os.environ.get("TIMELYRE_DB", "meta_data")

PYTHON_TIMEOUT = int(os.environ.get("PYTHON_EXEC_TIMEOUT", "30"))

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
    def __init__(self):
        self._conn: DatabaseConn | None = None

    @property
    def db_conn(self) -> DatabaseConn:
        if self._conn is None:
            self._conn = DatabaseConn(
                jdbc_http_proxy=TIMELYRE_PROXY,
                real_conn=TIMELYRE_CONN,
                db=TIMELYRE_DB,
                auto_close=False,
            )
        return self._conn

    def execute_sql(self, sql: str) -> dict:
        sql = validate_readonly(sql)
        logger.info("Executing SQL: %s", sql)

        conn = self.db_conn
        df = conn.run_sql(sql)

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

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close_connection()
            except Exception:
                pass
            self._conn = None

    @staticmethod
    def _df_to_result(df: pd.DataFrame) -> dict:
        return {
            "columns": df.columns.tolist(),
            "rows": df.where(pd.notna(df), None).values.tolist(),
            "rowCount": len(df),
        }
