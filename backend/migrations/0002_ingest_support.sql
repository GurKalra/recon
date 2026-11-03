-- 0002: what the ingestion pipeline needs on top of the foundation schema.

-- Non-fatal notices from an import: duplicate rows, mangled ids, header changes, capped rejects.
ALTER TABLE imports ADD COLUMN warnings JSONB NOT NULL DEFAULT '[]'::jsonb;

-- Row number in the partner's original file, so a claim's evidence can point at the exact row.
ALTER TABLE ledger_lines ADD COLUMN source_row INTEGER;