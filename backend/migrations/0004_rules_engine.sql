-- 0004: rate cards, reconciliation runs, per-order status and findings.
--
-- Rate cards are effective-dated: the card in force on an order's ordered_on date applies.
-- Findings are persistent. A re-run updates them, re-opens them if the problem comes back and
-- marks them 'resolved' when the money has arrived, so claims never lose their findings.

-- amount * bps / 10000, rounded half up, in integer arithmetic. Used by every fee rule.
CREATE FUNCTION recon_pct(amount BIGINT, bps INTEGER) RETURNS BIGINT
    LANGUAGE sql IMMUTABLE PARALLEL SAFE
    AS $$ SELECT (amount * bps + 5000) / 10000 $$;

-- Pincode prefix -> courier zone. The longest matching prefix wins; no match means zone D.
CREATE TABLE zone_prefixes (
    prefix TEXT PRIMARY KEY CHECK (prefix ~ '^[0-9]{1,6}$'),
    zone   CHAR(1) NOT NULL CHECK (zone IN ('A', 'B', 'C', 'D'))
);

CREATE TABLE channel_cards (
    id                      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    partner_id              BIGINT      NOT NULL REFERENCES partners (id),
    effective_from          DATE        NOT NULL,
    commission_bps          INTEGER     NOT NULL DEFAULT 0 CHECK (commission_bps BETWEEN 0 AND 10000),
    fixed_paise             INTEGER     NOT NULL DEFAULT 0 CHECK (fixed_paise >= 0),
    ship_extra_per_kg_paise INTEGER     NOT NULL DEFAULT 0 CHECK (ship_extra_per_kg_paise >= 0),
    tcs_bps                 INTEGER     NOT NULL DEFAULT 0 CHECK (tcs_bps BETWEEN 0 AND 10000),
    tds_bps                 INTEGER     NOT NULL DEFAULT 0 CHECK (tds_bps BETWEEN 0 AND 10000),
    pg_fee_bps              INTEGER     NOT NULL DEFAULT 0 CHECK (pg_fee_bps BETWEEN 0 AND 10000),
    note                    TEXT,
    created_by              BIGINT REFERENCES users (id),
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (partner_id, effective_from)
);

CREATE TABLE channel_ship_slabs (
    card_id   BIGINT  NOT NULL REFERENCES channel_cards (id) ON DELETE CASCADE,
    max_g     INTEGER NOT NULL CHECK (max_g > 0),  -- up to and including this chargeable weight
    fee_paise INTEGER NOT NULL CHECK (fee_paise >= 0),
    PRIMARY KEY (card_id, max_g)
);

CREATE TABLE courier_cards (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    partner_id     BIGINT      NOT NULL REFERENCES partners (id),
    effective_from DATE        NOT NULL,
    cod_flat_paise INTEGER     NOT NULL DEFAULT 0 CHECK (cod_flat_paise >= 0),
    cod_bps        INTEGER     NOT NULL DEFAULT 0 CHECK (cod_bps BETWEEN 0 AND 10000),
    return_pct     INTEGER     NOT NULL DEFAULT 100 CHECK (return_pct BETWEEN 0 AND 1000),
    note           TEXT,
    created_by     BIGINT REFERENCES users (id),
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (partner_id, effective_from)
);

CREATE TABLE courier_zone_rates (
    card_id    BIGINT  NOT NULL REFERENCES courier_cards (id) ON DELETE CASCADE,
    zone       CHAR(1) NOT NULL CHECK (zone IN ('A', 'B', 'C', 'D')),
    base_paise INTEGER NOT NULL CHECK (base_paise >= 0),  -- first 500 g
    addl_paise INTEGER NOT NULL CHECK (addl_paise >= 0),  -- each further started 500 g
    PRIMARY KEY (card_id, zone)
);

CREATE TABLE recon_settings (
    key   TEXT  PRIMARY KEY,
    value JSONB NOT NULL
);
INSERT INTO recon_settings (key, value) VALUES ('receipt_wait_days', '7'::jsonb);

CREATE TABLE recon_runs (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    as_of         DATE        NOT NULL,
    status        TEXT        NOT NULL DEFAULT 'running'
                  CHECK (status IN ('running', 'succeeded', 'failed')),
    orders_total  INTEGER,
    stats         JSONB       NOT NULL DEFAULT '{}'::jsonb,
    stage_timings JSONB       NOT NULL DEFAULT '{}'::jsonb,
    error         TEXT,
    triggered_by  BIGINT REFERENCES users (id),
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ
);

-- Latest verdict per order. Rewritten by every successful run.
CREATE TABLE order_status (
    order_id       BIGINT  PRIMARY KEY REFERENCES orders (id) ON DELETE CASCADE,
    run_id         BIGINT  NOT NULL REFERENCES recon_runs (id),
    status         TEXT    NOT NULL CHECK (status IN (
                       'settled', 'pending', 'short_paid', 'unpaid_past_due',
                       'freight_overcharged', 'return_not_received', 'cancelled')),
    expected_paise BIGINT  NOT NULL,
    received_paise BIGINT  NOT NULL,
    variance_paise BIGINT  NOT NULL,  -- received - expected; negative = we are short
    flags          TEXT[]  NOT NULL DEFAULT '{}'
);
CREATE INDEX order_status_status_idx ON order_status (status);

CREATE TABLE findings (
    id               BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    order_id         BIGINT      NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
    partner_id       BIGINT      NOT NULL REFERENCES partners (id),  -- who owes us / overcharged
    type             TEXT        NOT NULL CHECK (type IN (
                         'unpaid_past_due', 'short_paid', 'freight_overcharged',
                         'return_not_received')),
    amount_paise     BIGINT      NOT NULL CHECK (amount_paise >= 0),
    since_on         DATE        NOT NULL,  -- the age clock starts here (due date, or when billed)
    claim_deadline   DATE        NOT NULL,  -- since_on + the partner's claim window
    status           TEXT        NOT NULL DEFAULT 'open' CHECK (status IN (
                         'open', 'claimed', 'recovered', 'rejected', 'written_off', 'resolved')),
    first_seen_run   BIGINT      NOT NULL REFERENCES recon_runs (id),
    last_seen_run    BIGINT      NOT NULL REFERENCES recon_runs (id),
    resolved_at      TIMESTAMPTZ,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (order_id, type)
);
CREATE INDEX findings_open_idx ON findings (partner_id, since_on) WHERE status IN ('open', 'claimed');
CREATE INDEX findings_status_idx ON findings (status, type);