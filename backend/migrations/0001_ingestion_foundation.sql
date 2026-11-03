-- 0001: ingestion foundation.
--
-- Conventions
--   * Money is BIGINT paise everywhere. Never floats.
--   * Ledger amounts are signed from the brand's point of view: credit (+) = money in,
--     debit (-) = money out or deducted.
--   * Dates are IST calendar dates (DATE), not timestamps.
--   * order_ref columns hold the CANONICAL reference: upper-case, [A-Z0-9] only.

CREATE TABLE users (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email         TEXT        NOT NULL,
    password_hash TEXT        NOT NULL,
    full_name     TEXT        NOT NULL,
    role          TEXT        NOT NULL CHECK (role IN ('finance_exec', 'cofounder')),
    is_active     BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX users_email_key ON users (lower(email));

CREATE TABLE partners (
    id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    code              TEXT        NOT NULL UNIQUE CHECK (code ~ '^[A-Z0-9_]{2,16}$'),
    name              TEXT        NOT NULL,
    type              TEXT        NOT NULL
                      CHECK (type IN ('website', 'marketplace', 'quick_commerce', 'courier')),
    payout_sla_days   SMALLINT    NOT NULL DEFAULT 7  CHECK (payout_sla_days BETWEEN 0 AND 90),
    claim_window_days SMALLINT    NOT NULL DEFAULT 30 CHECK (claim_window_days BETWEEN 1 AND 365),
    tolerance_paise   INTEGER     NOT NULL DEFAULT 1000 CHECK (tolerance_paise >= 0),
    is_active         BOOLEAN     NOT NULL DEFAULT TRUE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE sources (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    partner_id BIGINT      NOT NULL REFERENCES partners (id),
    kind       TEXT        NOT NULL CHECK (kind IN (
                   'orders', 'settlement', 'cod_remittance', 'freight_invoice',
                   'warehouse_receipt')),
    name       TEXT        NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (partner_id, kind, name)
);

-- Append-only history of column mappings. Rollback = a NEW version copied from an old one.
CREATE TABLE source_mappings (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id          BIGINT      NOT NULL REFERENCES sources (id),
    version            INTEGER     NOT NULL CHECK (version >= 1),
    mapping            JSONB       NOT NULL,
    header_fingerprint TEXT        NOT NULL,
    status             TEXT        NOT NULL DEFAULT 'draft'
                       CHECK (status IN ('draft', 'approved', 'superseded')),
    note               TEXT,
    created_by         BIGINT REFERENCES users (id),
    approved_by        BIGINT REFERENCES users (id),
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    approved_at        TIMESTAMPTZ,
    UNIQUE (source_id, version),
    CHECK (status = 'draft' OR approved_at IS NOT NULL)
);
CREATE UNIQUE INDEX source_mappings_one_approved
    ON source_mappings (source_id) WHERE status = 'approved';

CREATE TABLE imports (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id      BIGINT      NOT NULL REFERENCES sources (id),
    mapping_id     BIGINT REFERENCES source_mappings (id),
    filename       TEXT        NOT NULL,
    file_sha256    TEXT        NOT NULL CHECK (file_sha256 ~ '^[0-9a-f]{64}$'),
    file_bytes     BIGINT      NOT NULL CHECK (file_bytes >= 0),
    status         TEXT        NOT NULL DEFAULT 'queued'
                   CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
    stage          TEXT,
    progress       REAL        NOT NULL DEFAULT 0 CHECK (progress BETWEEN 0 AND 1),
    rows_total     INTEGER,
    rows_loaded    INTEGER,
    rows_rejected  INTEGER,
    rows_duplicate INTEGER,
    stage_timings  JSONB       NOT NULL DEFAULT '{}'::jsonb,
    error          TEXT,
    created_by     BIGINT REFERENCES users (id),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at     TIMESTAMPTZ,
    finished_at    TIMESTAMPTZ
);
-- The same file uploaded twice is the same import; a failed attempt may be retried.
CREATE UNIQUE INDEX imports_one_per_file
    ON imports (source_id, file_sha256) WHERE status <> 'failed';
CREATE INDEX imports_source_created_idx ON imports (source_id, created_at DESC);

CREATE TABLE import_rejects (
    id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    import_id BIGINT  NOT NULL REFERENCES imports (id) ON DELETE CASCADE,
    row_no    INTEGER NOT NULL,
    reason    TEXT    NOT NULL,
    raw       JSONB   NOT NULL
);
CREATE INDEX import_rejects_import_idx ON import_rejects (import_id);

CREATE TABLE orders (
    id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    channel_partner_id BIGINT NOT NULL REFERENCES partners (id),
    courier_partner_id BIGINT REFERENCES partners (id),
    order_ref          TEXT   NOT NULL CHECK (order_ref ~ '^[A-Z0-9]+$'),
    order_ref_raw      TEXT   NOT NULL,
    awb                TEXT,
    ordered_on         DATE   NOT NULL,
    delivered_on       DATE,
    returned_on        DATE,
    payment_mode       TEXT   NOT NULL CHECK (payment_mode IN ('prepaid', 'cod')),
    value_paise        BIGINT NOT NULL CHECK (value_paise >= 0),
    weight_g           INTEGER CHECK (weight_g > 0),
    length_cm          SMALLINT CHECK (length_cm > 0),
    width_cm           SMALLINT CHECK (width_cm > 0),
    height_cm          SMALLINT CHECK (height_cm > 0),
    pincode            TEXT CHECK (pincode ~ '^[0-9]{6}$'),
    delivery_status    TEXT   NOT NULL CHECK (delivery_status IN (
                           'placed', 'shipped', 'delivered', 'returned', 'cancelled')),
    import_id          BIGINT REFERENCES imports (id) ON DELETE SET NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (channel_partner_id, order_ref),
    CHECK (delivered_on IS NULL OR delivered_on >= ordered_on),
    CHECK (returned_on  IS NULL OR returned_on  >= ordered_on)
);
CREATE INDEX orders_courier_ref_idx ON orders (courier_partner_id, order_ref)
    WHERE courier_partner_id IS NOT NULL;
CREATE INDEX orders_awb_idx    ON orders (awb) WHERE awb IS NOT NULL;
CREATE INDEX orders_status_idx ON orders (delivery_status, delivered_on);

-- One row per money movement (a settlement component, a freight charge, a remittance...).
-- Wide partner rows (value + commission + tcs in one row) are exploded into several lines.
CREATE TABLE ledger_lines (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    import_id     BIGINT   NOT NULL REFERENCES imports (id) ON DELETE CASCADE,
    partner_id    BIGINT   NOT NULL REFERENCES partners (id),  -- who paid or billed us
    order_id      BIGINT REFERENCES orders (id),
    order_ref     TEXT CHECK (order_ref ~ '^[A-Z0-9]+$'),      -- NULL when the row had none
    order_ref_raw TEXT,
    awb           TEXT,
    line_type     TEXT     NOT NULL CHECK (line_type IN (
                      'sale', 'refund', 'commission', 'fixed_fee', 'shipping_fee',
                      'return_fee', 'pg_fee', 'freight', 'return_freight', 'cod_fee',
                      'tcs', 'tds', 'adjustment')),
    amount_paise  BIGINT   NOT NULL,
    occurred_on   DATE     NOT NULL,
    reference     TEXT,                                        -- UTR / invoice number
    line_hash     CHAR(64) NOT NULL,                           -- sha256 of the row content
    dup_seq       SMALLINT NOT NULL DEFAULT 0 CHECK (dup_seq >= 0),
    -- Re-uploading overlapping files is harmless (same hash, same dup_seq -> skipped).
    -- A verbatim repeat INSIDE one file gets dup_seq > 0 and is flagged, not double counted.
    UNIQUE (partner_id, line_hash, dup_seq)
);
CREATE INDEX ledger_lines_partner_ref_idx ON ledger_lines (partner_id, order_ref);
CREATE INDEX ledger_lines_order_idx       ON ledger_lines (order_id) WHERE order_id IS NOT NULL;
CREATE INDEX ledger_lines_awb_idx         ON ledger_lines (awb) WHERE awb IS NOT NULL;
CREATE INDEX ledger_lines_date_idx        ON ledger_lines (occurred_on);
CREATE INDEX ledger_lines_import_idx      ON ledger_lines (import_id);
CREATE INDEX ledger_lines_unlinked_idx    ON ledger_lines (partner_id, occurred_on)
    WHERE order_id IS NULL;

CREATE TABLE warehouse_receipts (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    import_id   BIGINT NOT NULL REFERENCES imports (id) ON DELETE CASCADE,
    order_id    BIGINT REFERENCES orders (id),
    order_ref   TEXT   NOT NULL CHECK (order_ref ~ '^[A-Z0-9]+$'),
    received_on DATE   NOT NULL,
    condition   TEXT,
    UNIQUE (order_ref, received_on)
);
CREATE INDEX warehouse_receipts_order_idx ON warehouse_receipts (order_id)
    WHERE order_id IS NOT NULL;