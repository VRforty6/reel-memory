-- 007_search_fts_index.sql — index the exact expression used by FTS_SQL.
-- Safe for existing data; no rows are rewritten or reset.
-- Use CONCURRENTLY so applying this migration does not block normal writes.

CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_memory_segments_content_fts
    ON memory_segments
    USING gin (to_tsvector('english', content));
