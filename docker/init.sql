CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS table_embeddings (
    id               SERIAL PRIMARY KEY,
    schema_name      TEXT NOT NULL,
    db               TEXT NOT NULL,
    table_name       TEXT NOT NULL,
    doc              TEXT NOT NULL DEFAULT '',
    "desc"           TEXT NOT NULL DEFAULT '' COMMENT '表作用描述',
    ddl              TEXT NOT NULL DEFAULT '',
    types            TEXT[] NOT NULL DEFAULT '{}',
    embedding        vector(1024),

    UNIQUE (schema_name, db, table_name)
);

CREATE INDEX IF NOT EXISTS idx_te_embedding
    ON table_embeddings USING hnsw (embedding vector_cosine_ops);

CREATE INDEX IF NOT EXISTS idx_te_schema_db
    ON table_embeddings (schema_name, db);

-- 兼容已存在的表：新增 ddl 列
ALTER TABLE table_embeddings ADD COLUMN IF NOT EXISTS ddl TEXT NOT NULL DEFAULT '';
ALTER TABLE table_embeddings ADD COLUMN IF NOT EXISTS "desc" TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS sync_log (
    id          SERIAL PRIMARY KEY,
    sync_time   TIMESTAMPTZ DEFAULT now(),
    total_count INT NOT NULL DEFAULT 0,
    status      TEXT DEFAULT 'running'
);

-- 定时任务互斥锁
CREATE TABLE IF NOT EXISTS sync_lock (
    lock_key    TEXT PRIMARY KEY,
    locked_at   TIMESTAMPTZ DEFAULT now()
);
