-- 0009: files waiting in the upload wizard. The file itself lives on disk under a random name;
-- this row remembers what it was, who sent it, and what we learned about it.

CREATE TABLE uploads (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    filename    TEXT        NOT NULL,                 -- cleaned label, never a path
    stored_name TEXT        NOT NULL UNIQUE,          -- random name on disk
    kind        TEXT        NOT NULL CHECK (kind IN ('csv', 'xlsx')),
    file_bytes  BIGINT      NOT NULL CHECK (file_bytes > 0),
    file_sha256 TEXT        NOT NULL CHECK (file_sha256 ~ '^[0-9a-f]{64}$'),
    status      TEXT        NOT NULL DEFAULT 'staged'
                CHECK (status IN ('staged', 'imported', 'discarded')),
    profile     JSONB       NOT NULL DEFAULT '{}'::jsonb,   -- masked column summary
    detected    JSONB       NOT NULL DEFAULT '[]'::jsonb,   -- sources whose approved header matches
    created_by  BIGINT REFERENCES users (id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    import_id   BIGINT REFERENCES imports (id) ON DELETE SET NULL
);
CREATE INDEX uploads_status_idx ON uploads (status, created_at);