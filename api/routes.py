import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from agent.schema_retriever import SchemaRetriever, embed
from api.auth import require_admin
from api.models import TableSchemaIn, TableSchemaUpdate, BatchSyncIn, VectorOut
from agent.executor import Executor
from api.sync_task import trigger_sync_now

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
            f"SELECT schema_name, db, table_name, doc, \"desc\", ddl, types FROM table_embeddings WHERE {where_sql} ORDER BY schema_name, db, table_name",
            params,
        )
        rows = cur.fetchall()
    return [VectorOut(schema_name=r[0], db=r[1], table_name=r[2], doc=r[3], desc=r[4], ddl=r[5], types=r[6]) for r in rows]


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
            "SELECT schema_name, db, table_name, doc, \"desc\", ddl, types FROM table_embeddings WHERE schema_name = %s AND db = %s AND table_name = %s",
            (schema_name, db, table_name),
        )
        row = cur.fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"表 {schema_name}.{db}.{table_name} 不在向量库中")
    return VectorOut(schema_name=row[0], db=row[1], table_name=row[2], doc=row[3], desc=row[4], ddl=row[5], types=row[6])


@router.post("/tables", response_model=VectorOut, status_code=201)
async def upsert_table(
    body: TableSchemaIn,
    admin: str = Depends(require_admin),
):
    retriever = get_retriever()
    doc = f"库:{body.db} 表:{body.table_name} 描述:{body.desc} 类型:{body.type}"
    retriever.upsert_table(body.schema_name, body.db, body.table_name, body.desc, body.type, body.types, body.ddl)

    return VectorOut(schema_name=body.schema_name, db=body.db, table_name=body.table_name, doc=doc, ddl=body.ddl, types=body.types)


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
            "types": t.types,
            "ddl": t.ddl,
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
        emb = embed([doc])[0]
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Embedding 服务调用失败: {e}")

    set_parts = ["doc = %s", "\"desc\" = %s", "types = %s", "embedding = %s"]
    set_values = [doc, desc, types, emb]
    if body.ddl is not None:
        set_parts.append("ddl = %s")
        set_values.append(body.ddl)

    set_values.extend([schema_name, db, table_name])
    with retriever.conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE table_embeddings
            SET {', '.join(set_parts)}
            WHERE schema_name = %s AND db = %s AND table_name = %s
            RETURNING schema_name, db, table_name, doc, "desc", ddl, types
            """,
            set_values,
        )
        row = cur.fetchone()
    retriever.conn.commit()
    logger.info("Updated vector for %s.%s.%s", schema_name, db, table_name)
    return VectorOut(schema_name=row[0], db=row[1], table_name=row[2], doc=row[3], desc=row[4], ddl=row[5], types=row[6])


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
    import os

    jsonc_path = os.environ.get(
        "TABLE_RAG_PATH",
        os.path.join(os.path.dirname(__file__), "..", "data", "table_for_rag.json"),
    )
    jsonc_path = os.path.normpath(jsonc_path)

    with open(jsonc_path, "r", encoding="utf-8") as f:
        raw = f.read()
    tables = json.loads(raw, strict=False)

    schemas = [
        {
            "schema_name": t["schema"],
            "db": t["db"],
            "table_name": t["table"],
            "desc": t.get("desc", ""),
            "types": (str(t.get("type", ""))).split(","),
            "ddl": t.get("ddl", ""),
        }
        for t in tables
    ]

    retriever = get_retriever()
    try:
        retriever.build_index(schemas)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"同步失败: {e}")
    logger.info("Synced %d tables from table_for_rag.json", len(schemas))
    return {"status": "ok", "message": f"同步完成，共 {len(schemas)} 张表"}


@router.post("/sync-ddl", status_code=200)
async def sync_ddl_from_timelyre(admin: str = Depends(require_admin)):
    retriever = get_retriever()
    executor = Executor()

    with retriever.conn.cursor() as cur:
        cur.execute(
            "SELECT schema_name, db, table_name FROM table_embeddings WHERE ddl = ''"
        )
        rows = cur.fetchall()

    if not rows:
        return {"status": "ok", "message": "所有表已有 DDL，无需同步"}

    success = 0
    failed = 0
    for schema_name, db, table_name in rows:
        ddl = executor.get_table_ddl(schema_name, db, table_name)
        if ddl:
            retriever.update_table_ddl(schema_name, db, table_name, ddl)
            success += 1
        else:
            failed += 1

    logger.info("Synced DDL from timelyre: %d success, %d failed", success, failed)
    return {
        "status": "ok",
        "message": f"DDL 同步完成: {success} 成功, {failed} 失败, {len(rows)} 需要同步",
    }


@router.post("/sync-all", status_code=202)
async def sync_all_from_timelyre(admin: str = Depends(require_admin)):
    msg = trigger_sync_now()
    return {"status": "accepted", "message": msg}