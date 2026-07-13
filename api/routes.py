import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from agent.schema_retriever import SchemaRetriever, _embed
from api.auth import require_admin
from api.models import TableSchemaIn, TableSchemaUpdate, BatchSyncIn, VectorOut

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/vector", tags=["vector"])

_retriever: SchemaRetriever | None = None


def get_retriever() -> SchemaRetriever:
    global _retriever
    if _retriever is None:
        _retriever = SchemaRetriever()
        try:
            _retriever.init_db()
        except Exception as e:
            raise HTTPException(
                status_code=503,
                detail=f"向量库连接失败: {e}",
            )
    return _retriever


@router.get("/tables", response_model=list[VectorOut])
async def list_tables(
    schema_name: str | None = Query(None, description="按实例名过滤"),
    db: str | None = Query(None, description="按库名过滤"),
    admin: str = Depends(require_admin),
):
    retriever = get_retriever()
    where_parts = []
    params = []
    if schema_name:
        where_parts.append("schema_name = %s")
        params.append(schema_name)
    if db:
        where_parts.append("db = %s")
        params.append(db)
    where_sql = " AND ".join(where_parts) if where_parts else "TRUE"

    with retriever.conn.cursor() as cur:
        cur.execute(
            f"SELECT schema_name, db, table_name, doc, types FROM table_embeddings WHERE {where_sql} ORDER BY schema_name, db, table_name",
            params,
        )
        rows = cur.fetchall()
    return [VectorOut(schema_name=r[0], db=r[1], table_name=r[2], doc=r[3], types=r[4]) for r in rows]


@router.get("/tables/{schema_name}/{db}/{table_name}", response_model=VectorOut)
async def get_table(
    schema_name: str,
    db: str,
    table_name: str,
    admin: str = Depends(require_admin),
):
    retriever = get_retriever()
    with retriever.conn.cursor() as cur:
        cur.execute(
            "SELECT schema_name, db, table_name, doc, types FROM table_embeddings WHERE schema_name = %s AND db = %s AND table_name = %s",
            (schema_name, db, table_name),
        )
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"表 {schema_name}.{db}.{table_name} 不在向量库中")
    return VectorOut(schema_name=row[0], db=row[1], table_name=row[2], doc=row[3], types=row[4])


@router.post("/tables", response_model=VectorOut, status_code=201)
async def upsert_table(
    body: TableSchemaIn,
    admin: str = Depends(require_admin),
):
    retriever = get_retriever()
    doc = f"库:{body.db} 表:{body.table_name} 描述:{body.desc} 类型:{body.type}"
    try:
        emb = _embed([doc])[0]
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Embedding 服务调用失败: {e}")

    retriever.upsert_table(body.schema_name, body.db, body.table_name, body.desc, body.type, body.types)

    return VectorOut(schema_name=body.schema_name, db=body.db, table_name=body.table_name, doc=doc, types=body.types)


@router.post("/tables/batch", status_code=200)
async def batch_upsert_tables(
    body: BatchSyncIn,
    admin: str = Depends(require_admin),
):
    retriever = get_retriever()
    schemas = [
        {
            "schema_name": t.schema_name,
            "db": t.db,
            "table_name": t.table_name,
            "desc": t.desc,
            "type": t.type,
            "types": t.types,
        }
        for t in body.tables
    ]
    try:
        retriever.build_index(schemas)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"批量写入失败: {e}")
    return {"status": "ok", "count": len(body.tables)}


@router.put("/tables/{schema_name}/{db}/{table_name}", response_model=VectorOut)
async def update_table(
    schema_name: str,
    db: str,
    table_name: str,
    body: TableSchemaUpdate,
    admin: str = Depends(require_admin),
):
    retriever = get_retriever()

    with retriever.conn.cursor() as cur:
        cur.execute(
            "SELECT schema_name FROM table_embeddings WHERE schema_name = %s AND db = %s AND table_name = %s",
            (schema_name, db, table_name),
        )
        if cur.fetchone() is None:
            raise HTTPException(status_code=404, detail=f"表 {schema_name}.{db}.{table_name} 不在向量库中")

    desc = body.desc or ""
    type_ = body.type or ""
    doc = f"库:{db} 表:{table_name} 描述:{desc} 类型:{type_}"
    types = body.types or []
    try:
        emb = _embed([doc])[0]
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Embedding 服务调用失败: {e}")

    with retriever.conn.cursor() as cur:
        cur.execute(
            """
            UPDATE table_embeddings
            SET doc = %s, types = %s, embedding = %s
            WHERE schema_name = %s AND db = %s AND table_name = %s
            RETURNING schema_name, db, table_name, doc, types
            """,
            (doc, types, emb, schema_name, db, table_name),
        )
        row = cur.fetchone()
    retriever.conn.commit()
    logger.info("Updated vector for %s.%s.%s", schema_name, db, table_name)
    return VectorOut(schema_name=row[0], db=row[1], table_name=row[2], doc=row[3], types=row[4])


@router.delete("/tables/{schema_name}/{db}/{table_name}", status_code=200)
async def delete_table(
    schema_name: str,
    db: str,
    table_name: str,
    admin: str = Depends(require_admin),
):
    retriever = get_retriever()
    deleted = retriever.delete_table_vector(schema_name, db, table_name)
    if deleted == 0:
        raise HTTPException(status_code=404, detail=f"表 {schema_name}.{db}.{table_name} 不在向量库中")
    return {"status": "ok", "message": f"已删除 {schema_name}.{db}.{table_name} 的向量"}


@router.post("/sync", status_code=200)
async def sync_from_jsonc(admin: str = Depends(require_admin)):
    import json
    import re
    import os

    jsonc_path = os.environ.get(
        "TABLE_RAG_PATH",
        os.path.join(os.path.dirname(__file__), "..", "data", "table_for_rag.jsonc"),
    )
    jsonc_path = os.path.normpath(jsonc_path)

    with open(jsonc_path, "r", encoding="utf-8") as f:
        raw = f.read()
    json_str = re.sub(r"//.*?$", "", raw, flags=re.MULTILINE)
    tables = json.loads(json_str)

    schemas = [
        {
            "schema_name": t["schema"],
            "db": t["db"],
            "table_name": t["table"],
            "desc": t.get("desc", ""),
            "type": t.get("type", ""),
        }
        for t in tables
    ]

    retriever = get_retriever()
    try:
        retriever.build_index(schemas)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"同步失败: {e}")
    logger.info("Synced %d tables from table_for_rag.jsonc", len(schemas))
    return {"status": "ok", "message": f"同步完成，共 {len(schemas)} 张表"}
