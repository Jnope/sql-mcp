import asyncio
import logging
import os

from agent.executor import Executor
from agent.schema_retriever import SchemaRetriever, build_doc_from_meta, parse_ddl_to_meta, embed

logger = logging.getLogger(__name__)

SYNC_SCHEMAS = [s.strip() for s in os.environ.get("SYNC_SCHEMAS", "quark1,quark2").split(",") if s.strip()]
SYNC_EXCLUDE_DBS = set(
    s.strip() for s in os.environ.get(
        "SYNC_EXCLUDE_DBS",
        "default,timelyre_cache,system,live_board,project_board,meta_data"
    ).split(",") if s.strip()
)
SYNC_INTERVAL = int(os.environ.get("SYNC_INTERVAL_HOURS", "24")) * 3600
SYNC_LOCK_ID = 114514

_sync_task: asyncio.Task | None = None


def _acquire_lock(retriever: SchemaRetriever) -> bool:
    with retriever.conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(%s)", (SYNC_LOCK_ID,))
        return cur.fetchone()[0]


def _release_lock(retriever: SchemaRetriever):
    try:
        with retriever.conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (SYNC_LOCK_ID,))
        retriever.conn.commit()
    except Exception as e:
        logger.warning("Failed to release sync lock: %s", e)


def _sync_schema_tables(schema_name: str, executor: Executor, retriever: SchemaRetriever):
    databases = executor.list_databases(schema_name)
    if not databases:
        logger.warning("No databases found for schema %s", schema_name)
        return

    total = 0
    success = 0
    for db in databases:
        if db in SYNC_EXCLUDE_DBS:
            logger.debug("Skipping excluded db: %s, in schema: %s", db, schema_name)
            continue
        tables = executor.list_tables(schema_name, db)
        if not tables:
            continue

        for table_name in tables:
            total += 1
            ddl = executor.get_table_ddl(schema_name, db, table_name)
            if not ddl:
                logger.warning("No DDL for %s.%s.%s", schema_name, db, table_name)
                continue

            meta = parse_ddl_to_meta(ddl)
            doc = build_doc_from_meta(meta, db, table_name)
            table_desc = meta["desc"] if meta else ""
            table_types = meta["types"] if meta else []
            try:
                emb = embed([doc])[0]
            except Exception as e:
                logger.warning("Embedding failed for %s.%s.%s: %s", schema_name, db, table_name, e)
                continue

            with retriever.conn.cursor() as cur:
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
                    (schema_name, db, table_name, doc, table_desc, ddl, table_types, emb),
                )
            retriever.conn.commit()
            success += 1

        logger.info("Synced %s.%s: %d/%d tables", schema_name, db, success, total)

    logger.info("Schema %s done: %d/%d tables synced", schema_name, success, total)


def _run_sync():
    executor = Executor()
    retriever = SchemaRetriever()
    try:
        retriever.init_db()
    except Exception as e:
        logger.error("Failed to init pgvector: %s", e)
        return

    if not _acquire_lock(retriever):
        logger.info("Sync already running on another worker, skipping")
        retriever.close()
        return

    try:
        for schema_name in SYNC_SCHEMAS:
            try:
                _sync_schema_tables(schema_name, executor, retriever)
            except Exception as e:
                logger.error("Sync failed for schema %s: %s", schema_name, e)
    finally:
        _release_lock(retriever)
        retriever.close()


async def start_sync_task():
    global _sync_task
    if _sync_task is not None:
        return
    _sync_task = asyncio.create_task(_sync_loop())
    logger.info("Sync task started, interval=%ds, schemas=%s", SYNC_INTERVAL, SYNC_SCHEMAS)


async def stop_sync_task():
    global _sync_task
    if _sync_task is not None:
        _sync_task.cancel()
        try:
            await _sync_task
        except asyncio.CancelledError:
            pass
        _sync_task = None
        logger.info("Sync task stopped")


async def _sync_loop():
    await asyncio.sleep(10)
    while True:
        try:
            await asyncio.to_thread(_run_sync)
        except Exception as e:
            logger.error("Sync loop error: %s", e)
        await asyncio.sleep(SYNC_INTERVAL)


def trigger_sync_now() -> str:
    asyncio.create_task(asyncio.to_thread(_run_sync))
    return f"Sync triggered for schemas: {SYNC_SCHEMAS}"