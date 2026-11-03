-- 0003: make "attach order_id to freshly imported lines" cheap.
-- Both indexes are partial: they only ever contain the (normally tiny) set of unlinked rows.

CREATE INDEX ledger_lines_unlinked_ref_idx
    ON ledger_lines (order_ref) WHERE order_id IS NULL AND order_ref IS NOT NULL;

CREATE INDEX warehouse_receipts_unlinked_idx
    ON warehouse_receipts (order_ref) WHERE order_id IS NULL;