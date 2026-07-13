import os
import logging
import psycopg2
import psycopg2.extras
import pgvector.psycopg2
import httpx
from typing import Optional

logger = logging.getLogger(__name__)

PG_DSN = os.environ.get(
    "PGVECTOR_DSN",
    "host=172.18.192.76 port=16543 dbname=sqlagent user=postgres password=postgres",
)

EMBEDDING_API_URL = os.environ.get(
    "EMBEDDING_API_URL", "http://172.18.192.76:11434/v1/embeddings"
)
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "bge-m3")
EMBEDDING_DIM = int(os.environ.get("EMBEDDING_DIM", "1024"))
EMBEDDING_API_KEY = os.environ.get("EMBEDDING_API_KEY", "not-needed")


def _embed(texts: list[str]) -> list[list[float]]:
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
            doc = f"库:{t['db']} 表:{t['table_name']} 描述:{t.get('desc', '')} 类型:{t.get('type', '')}"
            emb = _embed([doc])[0]
            rows.append((
                t["schema_name"],
                t["db"],
                t["table_name"],
                doc,
                t.get("types", []),
                emb,
            ))

        with self.conn.cursor() as cur:
            psycopg2.extras.execute_values(
                cur,
                """
                INSERT INTO table_embeddings
                    (schema_name, db, table_name, doc, types, embedding)
                VALUES %s
                ON CONFLICT (schema_name, db, table_name)
                DO UPDATE SET doc      = EXCLUDED.doc,
                              types    = EXCLUDED.types,
                              embedding = EXCLUDED.embedding
                """,
                rows,
                template="(%s, %s, %s, %s, %s, %s)",
            )
        self.conn.commit()
        logger.info("Built index for %d tables", len(rows))

    def retrieve(self, question: str, top_n: int = 5) -> list[dict]:
        query_emb = _embed([question])[0]
        with self.conn.cursor() as cur:
            cur.execute(
                """
                SELECT schema_name, db, table_name, doc, types,
                       embedding <=> %s AS distance
                FROM table_embeddings
                ORDER BY embedding <=> %s
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
                "types": r[4],
                "_distance": float(r[5]),
            }
            for r in hits
        ]

    def upsert_table(
        self,
        schema_name: str,
        db: str,
        table_name: str,
        desc: str = "",
        type: str = "",
        types: list[str] | None = None,
    ):
        doc = f"库:{db} 表:{table_name} 描述:{desc} 类型:{type}"
        emb = _embed([doc])[0]
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO table_embeddings
                    (schema_name, db, table_name, doc, types, embedding)
                VALUES (%s, %s, %s, %s, %s, %s)
                ON CONFLICT (schema_name, db, table_name)
                DO UPDATE SET doc      = EXCLUDED.doc,
                              types    = EXCLUDED.types,
                              embedding = EXCLUDED.embedding
                """,
                (schema_name, db, table_name, doc, types or [], emb),
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

    def close(self):
        if self._conn and not self._conn.closed:
            self._conn.close()