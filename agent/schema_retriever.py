import os
import re
import logging
import psycopg2
import psycopg2.extras
import pgvector.psycopg2
import httpx
import sqlglot
from sqlglot import exp
from typing import Optional
from .config import PG_DSN, EMBEDDING_API_URL, EMBEDDING_MODEL, EMBEDDING_API_KEY

logger = logging.getLogger(__name__)

COMMON_FIELDS = {
    "id", "name", "short_name", "desc", "description", "comment", "type",
    "datetime", "create_time", "update_time", "dt", "date", "time", "fake_datetime",
    "code", "stock_code", "symbol", "ts_code", "ticker", "company_name",
    "trade_day", "trade_date",
    "open", "high", "low", "close", "volume", "turnover",
    "limit_up", "limit_down", "total_volume", "total_turnover", "vwap",
}


_KV_COMMENT_RE = re.compile(r"(?:\||^)\s*(\w+)\s*=\s*([^|]+?)(?=\s*\||\s*$)")

def _parse_kv_comment(comment: str) -> dict[str, str]:
    if not comment:
        return {}
    return {k.lower(): v.strip() for k, v in _KV_COMMENT_RE.findall(comment)}


def parse_ddl_to_meta(ddl: str) -> dict | None:
    if not ddl:
        return None
    try:
        parsed = sqlglot.parse_one(ddl, read="hive")
    except Exception as e:
        logger.error("Failed to parse ddl: %s, err: %s", ddl, e)
        return None
    if not isinstance(parsed, exp.Create):
        return None

    table = parsed.this.this if isinstance(parsed.this, exp.Schema) else parsed.this
    raw_name = table.name

    if "." in raw_name:
        name_parts = raw_name.split(".", 1)
        schema_name = name_parts[0]
        table_name = name_parts[1]
    else:
        schema_name = table.db or ""
        table_name = raw_name

    table_comment = ""
    table_kv = {}
    for prop in (parsed.args.get("properties") or []):
        if isinstance(prop, exp.SchemaCommentProperty):
            table_comment = prop.this.name
            table_kv = _parse_kv_comment(table_comment)
            break

    fields = []
    for col in parsed.find_all(exp.ColumnDef):
        col_name = col.name.lower()
        if col_name in COMMON_FIELDS:
            continue
        comment_text = ""
        for constraint in col.args.get("constraints", []):
            kind = constraint.args.get("kind")
            if isinstance(kind, exp.CommentColumnConstraint):
                comment_text = kind.this.name.strip()
                break
        if comment_text:
            fields.append(f"{col_name}:{comment_text}")
        else:
            fields.append(col_name)

    return {
        "schema_name": schema_name,
        "table_name": table_name,
        "table_comment": table_comment,
        "desc": table_kv.get("desc", ""),
        "types": table_kv.get("type", "").replace("，", ",").split(",") if table_kv.get("type") else [],
        "kv": table_kv,
        "fields": fields,
    }


def build_doc_from_meta(meta: dict, db: str, table: str, comment: str = "") -> str:
    if not meta:
        return f"库:{db}，表:{table}，描述:{comment or 'unknow'}"
    desc = comment or meta["desc"] or meta["table_comment"] or table.replace("_", " ")
    types = ",".join(meta["types"])
    doc = f"库:{db}，表:{table}，描述:{desc}"
    if types:
        doc += f"，类型:{types}"
    if meta["fields"]:
        doc += f"，字段:{','.join(meta['fields'])}"
    return doc


def _extract_distinctive_fields(ddl: str) -> list[str]:
    if not ddl:
        return []
    try:
        parsed = sqlglot.parse_one(ddl, read="hive")
    except Exception:
        return []
    if not isinstance(parsed, exp.Create):
        return []
    fields = []
    for col in parsed.find_all(exp.ColumnDef):
        col_name = col.name.lower()
        if col_name in COMMON_FIELDS:
            continue
        comment_text = ""
        for constraint in col.args.get("constraints", []):
            kind = constraint.args.get("kind")
            if isinstance(kind, exp.CommentColumnConstraint):
                comment_text = kind.this.name.strip()
                break
        if comment_text:
            fields.append(f"{col_name}:{comment_text}")
        else:
            fields.append(col_name)
    return fields


def _build_doc(db: str, table_name: str, desc: str, type_: str, ddl: str) -> str:
    doc = f"库:{db}，表:{table_name}，描述:{desc} 类型:{type_}"
    if ddl:
        fields_from_ddl = _extract_distinctive_fields(ddl)
        if fields_from_ddl:
            doc += f"，字段:{','.join(fields_from_ddl)}"
    return doc


def embed(texts: list[str]) -> list[list[float]]:
    resp = httpx.post(
        EMBEDDING_API_URL,
        json={"model": EMBEDDING_MODEL, "input": texts},
        headers={"Authorization": f"Bearer {EMBEDDING_API_KEY}"},
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    return [d["embedding"] for d in data["data"]]


class SchemaRetriever:
    def __init__(self, dsn: str = PG_DSN):
        self.dsn = dsn
        self._conn: Optional[psycopg2.extensions.connection] = None

    @property
    def conn(self):
        if self._conn is None or self._conn.closed:
            self._conn = psycopg2.connect(self.dsn)
            pgvector.psycopg2.register_vector(self._conn)
        return self._conn

    def init_db(self):
        with self.conn.cursor() as cur:
            cur.execute("SELECT to_regclass('table_embeddings')")
            if cur.fetchone()[0] is None:
                raise RuntimeError(
                    "表 table_embeddings 不存在，请先执行 docker/init.sql 初始化数据库"
                )
        self.conn.commit()
        logger.info("pgvector table_embeddings confirmed")

    def build_index(self, tables: list[dict]):
        rows = []
        for t in tables:
            desc = t.get('desc', '')
            types: list[str] = t.get("types", [])
            doc = _build_doc(t['db'], t['table_name'], desc, ",".join(types), t.get('ddl', ''))
            emb = embed([doc])[0]
            rows.append((
                t["schema_name"],
                t["db"],
                t["table_name"],
                doc,
                desc,
                t.get("ddl", ""),
                types,
                emb,
            ))

        with self.conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO table_embeddings
                    (schema_name, db, table_name, doc, "desc", ddl, types, embedding)
                VALUES %s
                ON CONFLICT (schema_name, db, table_name)
                DO UPDATE SET doc       = EXCLUDED.doc,
                              "desc"      = EXCLUDED."desc",
                              ddl       = EXCLUDED.ddl,
                              types     = EXCLUDED.types,
                              embedding = EXCLUDED.embedding
                """,
                rows,
                template="(%s, %s, %s, %s, %s, %s, %s, %s)",
            )
        self.conn.commit()
        logger.info("Built index for %d tables", len(rows))

    def retrieve(self, question: str, top_n: int = 5) -> list[dict]:
        query_emb = embed([question])[0]
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT schema_name, db, table_name, doc, "desc", ddl, types,
                       embedding <=> %s::vector AS distance
                FROM table_embeddings
                ORDER BY embedding <=> %s::vector
                LIMIT %s
                """,
                (query_emb, query_emb, top_n),
            )
            hits = cur.fetchall()

        return [
            {
                "schema_name": r[0],
                "db": r[1],
                "table_name": r[2],
                "doc": r[3],
                "desc": r[4],
                "ddl": r[5],
                "types": r[6],
                "_distance": float(r[7]),
            }
            for r in hits
        ]

    def upsert_table(
        self,
        schema_name: str,
        db: str,
        table_name: str,
        desc: str = "",
        types: list[str] | None = None,
        ddl: str = "",
    ):
        doc = _build_doc(db, table_name, desc, ",".join(types or []), ddl)
        emb = embed([doc])[0]
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO table_embeddings
                    (schema_name, db, table_name, doc, "desc", ddl, types, embedding)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (schema_name, db, table_name)
                DO UPDATE SET doc       = EXCLUDED.doc,
                              "desc"      = EXCLUDED."desc",
                              ddl       = EXCLUDED.ddl,
                              types     = EXCLUDED.types,
                              embedding = EXCLUDED.embedding
                """,
                (schema_name, db, table_name, doc, desc, ddl, types or [], emb),
            )
        self.conn.commit()
        logger.info("Upserted vector for %s.%s.%s", schema_name, db, table_name)

    def delete_table_vector(self, schema_name: str, db: str, table_name: str):
        with self.conn.cursor() as cur:
            cur.execute(
                "DELETE FROM table_embeddings WHERE schema_name = %s AND db = %s AND table_name = %s",
                (schema_name, db, table_name),
            )
            deleted = cur.rowcount
        self.conn.commit()
        logger.info("Deleted vector for %s.%s.%s (rows: %s)", schema_name, db, table_name, deleted)
        return deleted

    def update_table_ddl(self, schema_name: str, db: str, table_name: str, ddl: str):
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT doc, \"desc\", types FROM table_embeddings WHERE schema_name = %s AND db = %s AND table_name = %s",
                (schema_name, db, table_name),
            )
            row = cur.fetchone()
        if row is None:
            logger.warning("Table %s.%s.%s not found for DDL update", schema_name, db, table_name)
            return 0

        old_doc, types = row
        m = re.match(r'库:\S+ 表:\S+ 描述:(.*?) 类型:(.*?)( 字段:.*)?$', old_doc or "")
        desc = m.group(1) if m else ""
        type_ = m.group(2) if m else ""

        new_doc = _build_doc(db, table_name, desc, type_, ddl)
        emb = embed([new_doc])[0] if new_doc != old_doc else None

        with self.conn.cursor() as cur:
            if emb is not None:
                cur.execute(
                    """
                    UPDATE table_embeddings
                    SET ddl = %s, doc = %s, embedding = %s
                    WHERE schema_name = %s AND db = %s AND table_name = %s
                    """,
                    (ddl, new_doc, emb, schema_name, db, table_name),
                )
            else:
                cur.execute(
                    """
                    UPDATE table_embeddings
                    SET ddl = %s
                    WHERE schema_name = %s AND db = %s AND table_name = %s
                    """,
                    (ddl, schema_name, db, table_name),
                )
            updated = cur.rowcount
        self.conn.commit()
        if updated:
            logger.info("Updated DDL+doc for %s.%s.%s", schema_name, db, table_name)
        return updated

    def list_all_tables_light(self) -> list[dict]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT schema_name, db, table_name, \"desc\", types FROM table_embeddings ORDER BY schema_name, db, table_name"
            )
            rows = cur.fetchall()
        return [
            {
                "schema_name": r[0],
                "db": r[1],
                "table_name": r[2],
                "desc": r[3],
                "types": r[4],
            }
            for r in rows
        ]

    def get_ddl_by_names(self, tables: list[tuple[str, str, str]]) -> dict[tuple[str, str, str], str]:
        if not tables:
            return {}
        placeholders = ",".join("(%s,%s,%s)" for _ in tables)
        params = []
        for s, d, t in tables:
            params.extend([s, d, t])
        with self.conn.cursor() as cur:
            cur.execute(
                f"SELECT schema_name, db, table_name, ddl FROM table_embeddings "
                f"WHERE (schema_name, db, table_name) IN ({placeholders})",
                params,
            )
            rows = cur.fetchall()
        return {(r[0], r[1], r[2]): r[3] for r in rows}

    def close(self):
        if self._conn and not self._conn.closed:
            self._conn.close()