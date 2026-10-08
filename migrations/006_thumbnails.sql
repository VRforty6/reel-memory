-- 006_thumbnails.sql — per-memory thumbnail + media kind for the Milestone A
-- Android UX (thumbnail-forward Library/Search cards, Videos/Images/Links
-- filters). Purely additive: no existing column, index, or behavior changes.
--
-- Adds on memories:
--   1. thumbnail_jpeg BYTEA NULL — one small downscaled JPEG (max 320px wide)
--      persisted once at ingest from an already-extracted frame. Raw frames
--      are still deleted after processing; only this single small JPEG is
--      retained. Old memories (ingested before this migration) keep NULL and
--      the API returns 404 for their thumbnail — the app shows a placeholder.
--      Thumbnails are never reprocessed or backfilled.
--   2. media_kind VARCHAR(16) NULL — 'video' | 'image' | 'album' | 'article',
--      set once by the worker at ingest from the resolved source. NULL for
--      METADATA_ONLY / unknown. Used only for honest Library filtering.
--
-- No index: thumbnails are fetched by primary key with the memory row.

ALTER TABLE memories ADD COLUMN thumbnail_jpeg BYTEA;
ALTER TABLE memories ADD COLUMN media_kind VARCHAR(16);
