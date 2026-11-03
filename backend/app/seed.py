"""Seed partners, sources and approved mappings for the demo workspace. Safe to re-run."""

from __future__ import annotations

import json
from pathlib import Path

from psycopg import Connection
from psycopg.types.json import Jsonb

from app.ingest.catalog import SOURCES
from app.ingest.reader import read_table


def seed(conn: Connection, data_dir: Path) -> dict[str, int]:
    """Create partners (from rate_cards.json), the 12 sources and their approved mappings.

    Returns {filename: source_id}. The mapping's header fingerprint comes from the sample file,
    exactly as it would when a finance executive approves a mapping on a real file.
    """
    cards = json.loads((data_dir / "rate_cards.json").read_text(encoding="utf-8"))
    for p in cards["partners"]:
        conn.execute(
            """
            INSERT INTO partners (code, name, type, payout_sla_days, claim_window_days,
                                  tolerance_paise)
            VALUES (%(code)s, %(name)s, %(type)s, %(payout_sla_days)s, %(claim_window_days)s,
                    %(tol)s)
            ON CONFLICT (code) DO UPDATE SET
                name = EXCLUDED.name, type = EXCLUDED.type,
                payout_sla_days = EXCLUDED.payout_sla_days,
                claim_window_days = EXCLUDED.claim_window_days,
                tolerance_paise = EXCLUDED.tolerance_paise
            """,
            {**p, "tol": cards["tolerance_paise"]},
        )
    source_ids: dict[str, int] = {}
    for s in SOURCES:
        partner_id = conn.execute(
            "SELECT id FROM partners WHERE code = %s", (s.partner,)
        ).fetchone()["id"]
        source_id = conn.execute(
            """
            INSERT INTO sources (partner_id, kind, name) VALUES (%s, %s, %s)
            ON CONFLICT (partner_id, kind, name) DO UPDATE SET name = EXCLUDED.name
            RETURNING id
            """,
            (partner_id, s.kind, s.name),
        ).fetchone()["id"]
        source_ids[s.filename] = source_id
        has_mapping = conn.execute(
            "SELECT 1 FROM source_mappings WHERE source_id = %s", (source_id,)
        ).fetchone()
        if not has_mapping:
            spec = s.spec()
            table = read_table(data_dir / s.filename, spec.format, spec.referenced_columns())
            conn.execute(
                """
                INSERT INTO source_mappings (source_id, version, mapping, header_fingerprint,
                                             status, note, approved_at)
                VALUES (%s, 1, %s, %s, 'approved', 'built-in demo mapping', now())
                """,
                (source_id, Jsonb(spec.model_dump(mode="json", exclude_none=True)),
                 table.fingerprint),
            )
    conn.commit()
    return source_ids