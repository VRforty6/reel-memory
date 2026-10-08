-- 004: auth + monetization (commercialization track).
--
-- Extends users with password_hash / google_sub / tier / pro_expires_at,
-- and adds refresh_tokens (server-side rotation records) and usage_counters
-- (freemium quota accounting). Safe to apply to existing databases: all new
-- user columns are nullable or have defaults; existing rows read as tier
-- 'free' with no password and no linked Google account.

ALTER TABLE users
    ADD COLUMN password_hash TEXT,
    ADD COLUMN google_sub TEXT,
    ADD COLUMN tier TEXT NOT NULL DEFAULT 'free',
    ADD COLUMN pro_expires_at TIMESTAMPTZ;
ALTER TABLE users
    ADD CONSTRAINT uq_users_google_sub UNIQUE (google_sub);

CREATE TABLE refresh_tokens (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash  TEXT NOT NULL UNIQUE,   -- sha256 of the token; never the token itself
    jti         TEXT NOT NULL,
    expires_at  TIMESTAMPTZ NOT NULL,
    revoked     BOOLEAN NOT NULL DEFAULT FALSE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_refresh_tokens_user ON refresh_tokens (user_id);

CREATE TABLE usage_counters (
    user_id     UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    bucket      TEXT NOT NULL,          -- 'captures' | 'questions' | 'verifications'
    period      TEXT NOT NULL,          -- 'YYYY-MM' (monthly) | 'YYYY-MM-DD' (daily)
    count       INTEGER NOT NULL DEFAULT 0,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT pk_usage_counters PRIMARY KEY (user_id, bucket, period)
);
