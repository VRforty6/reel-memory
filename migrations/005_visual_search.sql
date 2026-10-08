-- 005_visual_search.sql — visual frame embeddings for visual search.
--
-- Adds:
--   1. memory_frame_embeddings: one row per indexed video frame / album photo,
--      carrying a local joint image/text embedding (OpenCLIP ViT-B-32, 512-d).
--      The existing 1536-d text embeddings are untouched.
--   2. HNSW index on the new visual embedding column.
--   3. HNSW index on the existing text embedding column
--      (memory_segments.embedding), which until now had no approximate
--      index — only a comment in 001_initial.sql.
--
-- Index choice: HNSW over IVFFlat. Rationale:
--   * HNSW needs no training step and no periodic reindex after inserts,
--     which fits a continuously-ingesting personal library.
--   * IVFFlat requires choosing nlists up front and degrades without
--     retraining as the table grows; it also forces a maintenance story
--     (reindex cadence) this project does not need yet.
--   * Dataset assumption: personal library, 10^3–10^5 memories, each with
--     <= 24 frame rows. HNSW(m=16, ef_construction=64) is the standard
--     starting point for this scale with pgvector.
--   * Cosine distance (<=> on L2-normalized vectors == cosine distance)
--     matches how the embeddings are produced (L2-normalized) and queried.
--
-- Dimension contract: VISUAL_EMBEDDING_DIM in app/config.py must equal the
-- vector(N) below. The worker refuses to index when the loaded model dim
-- differs (VISUAL_INDEX_FAILED) so a mismatch can never silently corrupt
-- retrieval.

CREATE TABLE IF NOT EXISTS memory_frame_embeddings (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    memory_id   UUID NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    frame_index INTEGER NOT NULL,          -- dense 0..N-1 over the memory's indexed frames
    timestamp_ms INTEGER NOT NULL,         -- ms into the video; 0 for album photos
    selection   TEXT NOT NULL              -- 'uniform' | 'scene' | 'photo'
        CHECK (selection IN ('uniform', 'scene', 'photo')),
    album_index INTEGER,                   -- 0-based photo/video position in an album; NULL otherwise
    embedding   vector(512) NOT NULL,      -- OpenCLIP ViT-B-32 (laion2b_s34b_b79k), L2-normalized
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One memory's frames share a dense index; no uniqueness constraint is
-- needed beyond the PK (backfill appends per album file with a global
-- offset — see app/pipeline/frame_index.py).
CREATE INDEX IF NOT EXISTS ix_frame_embeddings_memory
    ON memory_frame_embeddings (memory_id);

-- HNSW cosine index for the visual retrieval branch of hybrid search.
-- <=> on L2-normalized vectors is cosine distance.
CREATE INDEX IF NOT EXISTS ix_frame_embeddings_hnsw
    ON memory_frame_embeddings
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);

-- The text embedding column (vector(1536)) backs the existing semantic
-- branch of hybrid search. Give it the same HNSW treatment; cosine ops
-- match the application's cosine-similarity ranking.
CREATE INDEX IF NOT EXISTS ix_memory_segments_embedding_hnsw
    ON memory_segments
    USING hnsw (embedding vector_cosine_ops)
    WITH (m = 16, ef_construction = 64);
