-- 0007: claims. A claim is a batch of findings raised with one partner. Each finding in it has
-- its own outcome, because a partner may pay some orders, reject others and part-pay a few.
--
-- Findings keep their own status (open, claimed, recovered, rejected, written_off, resolved);
-- finding_events records every change, so "who did what and when" is never lost.

ALTER TABLE findings
    ADD COLUMN recovered_paise BIGINT NOT NULL DEFAULT 0 CHECK (recovered_paise >= 0);

CREATE TABLE claims (
    id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    partner_id   BIGINT NOT NULL REFERENCES partners (id),
    status       TEXT   NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'submitted', 'closed')),
    partner_ref  TEXT,                     -- the ticket / case number the partner gave us
    note         TEXT,
    created_by   BIGINT REFERENCES users (id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    submitted_by BIGINT REFERENCES users (id),
    submitted_on DATE,                     -- the day it was sent to the partner
    submitted_at TIMESTAMPTZ,
    closed_at    TIMESTAMPTZ
);
CREATE INDEX claims_status_idx ON claims (status, partner_id);

CREATE TABLE claim_items (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    claim_id        BIGINT NOT NULL REFERENCES claims (id) ON DELETE CASCADE,
    finding_id      BIGINT NOT NULL REFERENCES findings (id),
    claimed_paise   BIGINT NOT NULL CHECK (claimed_paise >= 0),
    outcome         TEXT   NOT NULL DEFAULT 'pending' CHECK (outcome IN (
                        'pending', 'recovered', 'partial', 'rejected', 'written_off',
                        'withdrawn')),
    recovered_paise BIGINT NOT NULL DEFAULT 0 CHECK (recovered_paise >= 0),
    note            TEXT,
    decided_by      BIGINT REFERENCES users (id),
    decided_at      TIMESTAMPTZ,
    CHECK (recovered_paise <= claimed_paise)
);
-- a finding can be in only one live claim at a time (it may be claimed again after a rejection)
CREATE UNIQUE INDEX claim_items_one_live_idx ON claim_items (finding_id) WHERE outcome = 'pending';
CREATE INDEX claim_items_claim_idx ON claim_items (claim_id);

CREATE TABLE finding_events (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    finding_id BIGINT      NOT NULL REFERENCES findings (id) ON DELETE CASCADE,
    at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_id    BIGINT REFERENCES users (id),
    kind       TEXT        NOT NULL,
    detail     JSONB       NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX finding_events_finding_idx ON finding_events (finding_id, at);