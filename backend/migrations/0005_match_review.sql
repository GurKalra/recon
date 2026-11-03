-- 0005: the review queue for payments that arrived without an order ID.
--
-- A "match group" is the set of ledger lines that belong to one payment (a wide row, or the
-- consecutive rows of a long-layout payment). Suggestions rank the orders it probably belongs to.
-- A person accepts one, rejects them, or links the payment by hand. Nothing is ever linked
-- automatically, and every link can be undone.

CREATE TABLE match_groups (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    partner_id    BIGINT   NOT NULL REFERENCES partners (id),
    import_id     BIGINT   NOT NULL REFERENCES imports (id) ON DELETE CASCADE,
    first_row     INTEGER  NOT NULL,                 -- source row in the file
    last_row      INTEGER  NOT NULL,
    reference     TEXT,                              -- UTR / settlement id of the first line
    occurred_on   DATE     NOT NULL,
    line_ids      BIGINT[] NOT NULL,                 -- every line that moves with the group,
                                                     -- including verbatim duplicate copies
    n_lines       INTEGER  NOT NULL,
    sale_paise    BIGINT,                            -- the order value this payment carries
    net_paise     BIGINT   NOT NULL,
    has_refund    BOOLEAN  NOT NULL DEFAULT false,
    status        TEXT     NOT NULL DEFAULT 'open'
                  CHECK (status IN ('open', 'linked', 'dismissed')),
    linked_order_id BIGINT REFERENCES orders (id),
    link_method   TEXT CHECK (link_method IN ('suggestion', 'manual')),
    decided_by    BIGINT REFERENCES users (id),
    decided_at    TIMESTAMPTZ,
    note          TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (import_id, first_row)
);
CREATE INDEX match_groups_open_idx ON match_groups (partner_id, occurred_on) WHERE status = 'open';

-- Candidate search looks orders up by partner and exact value.
CREATE INDEX orders_channel_value_idx ON orders (channel_partner_id, value_paise);

CREATE TABLE match_suggestions (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    group_id    BIGINT   NOT NULL REFERENCES match_groups (id) ON DELETE CASCADE,
    order_id    BIGINT   NOT NULL REFERENCES orders (id) ON DELETE CASCADE,
    rank        SMALLINT NOT NULL,
    score       NUMERIC(4, 3) NOT NULL CHECK (score BETWEEN 0 AND 1),
    confidence  TEXT     NOT NULL CHECK (confidence IN ('high', 'medium', 'low')),
    reasons     JSONB    NOT NULL,                   -- plain-English list shown to the reviewer
    features    JSONB    NOT NULL,                   -- the numbers behind the score
    status      TEXT     NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'accepted', 'rejected', 'superseded')),
    decided_by  BIGINT REFERENCES users (id),
    decided_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (group_id, order_id)
);
CREATE INDEX match_suggestions_group_idx ON match_suggestions (group_id, rank);