-- 003: per-user intent history for the decision brief pipeline.
--
-- A lightweight JSON list on the user record (newest last), e.g.
-- [{"domain":"purchase","intent":"decide","label":"deciding whether to buy this",
--   "source":"model","memory_id":"...","at":"2026-09-22T..."}].
-- The API layer caps its length; no new tables.

ALTER TABLE users
    ADD COLUMN intent_history JSONB NOT NULL DEFAULT '[]';
