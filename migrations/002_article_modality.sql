-- 002: allow the 'article' segment modality (website ingestion).
--
-- Website ingestion (POST /v1/captures/url, platform "web") stores the
-- extracted main article text as a memory segment with modality 'article'.
-- This migration widens the modality check constraint; it is safe to apply
-- to existing databases (no data changes, constraint only gains a value).

ALTER TABLE memory_segments
    DROP CONSTRAINT ck_memory_segments_modality;

ALTER TABLE memory_segments
    ADD CONSTRAINT ck_memory_segments_modality
    CHECK (modality IN ('speech','ocr','visual','caption','summary','article'));
