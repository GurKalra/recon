-- 0006: optional extra columns from partner files, and what the AI said about a payment.
--
-- `extras` holds columns a partner's file carries that we do not model (SKU, city, remarks...),
-- as a flat JSON object. The mapping decides which columns are kept. They never affect the
-- reconciliation maths; they help a person (and the AI) tell similar orders apart.

ALTER TABLE orders       ADD COLUMN extras JSONB;
ALTER TABLE ledger_lines ADD COLUMN extras JSONB;

ALTER TABLE match_groups
    ADD COLUMN ai_verdict TEXT CHECK (ai_verdict IN ('picked', 'none', 'error')),
    ADD COLUMN ai_model   TEXT,
    ADD COLUMN ai_at      TIMESTAMPTZ,
    ADD COLUMN ai_note    TEXT;               -- the model's one-line reason, or the error

ALTER TABLE match_suggestions
    ADD COLUMN ai_pick       BOOLEAN,         -- the model preferred this candidate
    ADD COLUMN ai_confidence TEXT CHECK (ai_confidence IN ('high', 'medium', 'low')),
    ADD COLUMN ai_reason     TEXT;