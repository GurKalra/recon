-- 0008: sign-in. Sessions are server-side (the cookie holds a random token; we keep only its hash),
-- failed sign-ins are counted to slow guessing, and the audit log records who did what.
-- The audit log is append-only by convention and must be kept for at least a year.

CREATE TABLE sessions (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    token_hash   CHAR(64)    NOT NULL UNIQUE,
    user_id      BIGINT      NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    csrf_token   TEXT        NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL,
    ip           TEXT,
    user_agent   TEXT
);
CREATE INDEX sessions_user_idx ON sessions (user_id);
CREATE INDEX sessions_expiry_idx ON sessions (expires_at);

CREATE TABLE login_attempts (
    id      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    email   TEXT        NOT NULL,   -- lower-cased
    ip      TEXT,
    success BOOLEAN     NOT NULL
);
CREATE INDEX login_attempts_email_idx ON login_attempts (email, at DESC);
CREATE INDEX login_attempts_ip_idx    ON login_attempts (ip, at DESC);

CREATE TABLE audit_log (
    id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_id   BIGINT REFERENCES users (id),
    action    TEXT        NOT NULL,   -- e.g. 'login', 'claim.submit', 'evidence.download'
    entity    TEXT,                   -- e.g. 'claim'
    entity_id BIGINT,
    detail    JSONB       NOT NULL DEFAULT '{}'::jsonb,
    ip        TEXT
);
CREATE INDEX audit_log_at_idx     ON audit_log (at DESC);
CREATE INDEX audit_log_entity_idx ON audit_log (entity, entity_id);
CREATE INDEX audit_log_user_idx   ON audit_log (user_id, at DESC);

-- Nobody changes history: the application role may add rows but not edit or remove them.
CREATE FUNCTION audit_log_is_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only';
END $$;
CREATE TRIGGER audit_log_no_change BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_is_append_only();