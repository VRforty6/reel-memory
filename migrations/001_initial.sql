-- Reel Memory initial schema — PRD §38 (core data model).
-- Apply with: psql $DATABASE_URL -f migrations/001_initial.sql
-- (The API's dev-mode init_db() creates the same shape via SQLAlchemy.)

CREATE EXTENSION IF NOT EXISTS vector;
-- gen_random_uuid() is built into PostgreSQL 13+.

CREATE TABLE users (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    email       TEXT NOT NULL UNIQUE
);

CREATE TABLE source_items (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id           UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    platform          TEXT NOT NULL,            -- 'instagram' (PRD §53: adapter per platform)
    platform_item_id  TEXT NOT NULL,            -- canonical source identity (shortcode)
    canonical_url     TEXT NOT NULL,            -- tracking params stripped
    original_url      TEXT NOT NULL,            -- exactly as shared
    creator_handle    TEXT,
    caption           TEXT,
    published_at      TIMESTAMPTZ,
    source_status     TEXT NOT NULL DEFAULT 'UNKNOWN',
    -- PRD §13 duplicate bookkeeping:
    first_saved_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_saved_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    save_count        INTEGER NOT NULL DEFAULT 1,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_source_items_user_platform_item
        UNIQUE (user_id, platform, platform_item_id)
);
CREATE INDEX idx_source_items_user ON source_items (user_id);

CREATE TABLE captures (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_item_id  UUID NOT NULL REFERENCES source_items(id) ON DELETE CASCADE,
    captured_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    capture_source  TEXT NOT NULL DEFAULT 'api'   -- e.g. 'android_share'
);
CREATE INDEX idx_captures_source_item ON captures (source_item_id);

CREATE TABLE memories (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    source_item_id      UUID NOT NULL UNIQUE REFERENCES source_items(id) ON DELETE CASCADE,
    title               TEXT,
    summary             TEXT,
    category            TEXT,
    language            TEXT,
    processing_version  TEXT,
    processing_status   TEXT NOT NULL DEFAULT 'CAPTURED',  -- PRD §30 state machine
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_memories_status ON memories (processing_status);

CREATE TABLE memory_segments (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    memory_id     UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    modality      TEXT NOT NULL
        CONSTRAINT ck_memory_segments_modality
        CHECK (modality IN ('speech','ocr','visual','caption','summary')),
    start_ms      INTEGER,
    end_ms        INTEGER,
    content       TEXT NOT NULL,
    -- 1536 must match EMBEDDING_DIM in config; change both together.
    embedding     vector(1536),
    metadata_json JSONB NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_memory_segments_memory ON memory_segments (memory_id);
-- NOTE: add an ivfflat index on embedding once the table has real data, e.g.:
-- CREATE INDEX idx_memory_segments_embedding
--   ON memory_segments USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

CREATE TABLE memory_tags (
    memory_id   UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    tag         TEXT NOT NULL,
    confidence  DOUBLE PRECISION,
    CONSTRAINT pk_memory_tags PRIMARY KEY (memory_id, tag)
);

CREATE TABLE processing_jobs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    memory_id       UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    status          TEXT NOT NULL DEFAULT 'QUEUED',  -- QUEUED | RUNNING | DONE | FAILED
    stage           TEXT,                            -- current ProcessingStatus value
    attempt_count   INTEGER NOT NULL DEFAULT 0,
    failure_code    TEXT,                            -- PRD §31 FailureCode name
    failure_message TEXT,
    started_at      TIMESTAMPTZ,
    finished_at     TIMESTAMPTZ
);
CREATE INDEX idx_processing_jobs_status ON processing_jobs (status);
CREATE INDEX idx_processing_jobs_memory ON processing_jobs (memory_id);
