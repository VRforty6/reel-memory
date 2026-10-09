-- 008_category_taxonomy.sql
-- Hierarchical per-user category tree + memory assignments.
-- Keeps memories.category as a compatibility leaf label for existing clients.

CREATE TABLE IF NOT EXISTS categories (
    id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    parent_id uuid REFERENCES categories(id) ON DELETE CASCADE,
    name varchar(128) NOT NULL,
    slug varchar(128) NOT NULL,
    path text NOT NULL,
    depth integer NOT NULL CHECK (depth >= 0),
    description text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_categories_user_path UNIQUE (user_id, path)
);

CREATE INDEX IF NOT EXISTS ix_categories_user_id ON categories(user_id);
CREATE INDEX IF NOT EXISTS ix_categories_parent_id ON categories(parent_id);

CREATE TABLE IF NOT EXISTS memory_categories (
    memory_id uuid NOT NULL REFERENCES memories(id) ON DELETE CASCADE,
    category_id uuid NOT NULL REFERENCES categories(id) ON DELETE CASCADE,
    is_primary boolean NOT NULL DEFAULT false,
    confidence double precision,
    source varchar(32) NOT NULL DEFAULT 'rules-v1',
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT pk_memory_categories PRIMARY KEY (memory_id, category_id)
);

CREATE INDEX IF NOT EXISTS ix_memory_categories_category_id
    ON memory_categories(category_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_categories_one_primary
    ON memory_categories(memory_id)
    WHERE is_primary = true;
