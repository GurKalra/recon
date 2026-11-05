"""Seed partners, sources and approved mappings for the demo workspace. Safe to re-run."""

from __future__ import annotations

import json
from pathlib import Path

from psycopg import Connection
from psycopg.types.json import Jsonb

from app.ingest.catalog import SOURCES
from app.ingest.reader import read_table

_EFFECTIVE_FROM = "2000-01-01"  # the demo cards have always been in force


def _upsert_partners(conn: Connection, cards: dict) -> None:
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


def ensure_partners(conn: Connection, data_dir: Path) -> int:
    """A brand-new install has no partners, so the upload wizard would have nothing to choose.
    Add the brief's partners (and only them: no orders, rate cards or mappings) when the table is
    empty. Returns how many were added; 0 when there were already partners or no file to read."""
    if conn.execute("SELECT count(*) AS n FROM partners").fetchone()["n"]:
        return 0
    path = data_dir / "rate_cards.json"
    if not path.is_file():
        return 0
    cards = json.loads(path.read_text(encoding="utf-8"))
    _upsert_partners(conn, cards)
    conn.commit()
    return len(cards["partners"])


def seed(conn: Connection, data_dir: Path) -> dict[str, int]:
    """Create partners (from rate_cards.json), the 12 sources and their approved mappings.

    Returns {filename: source_id}. The mapping's header fingerprint comes from the sample file,
    exactly as it would when a finance executive approves a mapping on a real file.
    """
    cards = json.loads((data_dir / "rate_cards.json").read_text(encoding="utf-8"))
    _upsert_partners(conn, cards)
    _seed_rate_cards(conn, cards)
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


def _partner_id(conn: Connection, code: str) -> int:
    return conn.execute("SELECT id FROM partners WHERE code = %s", (code,)).fetchone()["id"]


def _seed_rate_cards(conn: Connection, cards: dict) -> None:
    """Rate cards, zone prefixes and settings from rate_cards.json. Existing cards are kept."""
    for zone, rule in cards["zone_rules"].items():
        for prefix in rule["pincode_prefixes"]:
            conn.execute(
                "INSERT INTO zone_prefixes (prefix, zone) VALUES (%s, %s) "
                "ON CONFLICT (prefix) DO UPDATE SET zone = EXCLUDED.zone", (prefix, zone))
    conn.execute(
        "INSERT INTO recon_settings (key, value) VALUES ('receipt_wait_days', %s) "
        "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
        (Jsonb(cards["receipt_wait_days"]),))

    channel_cards = dict(cards["channel_cards"])
    # The website's payment gateway fee is a channel rule with no commission.
    channel_cards["WEB"] = {"commission_bps": 0, "fixed_paise": 0, "ship_slabs": [],
                            "ship_extra_per_kg_paise": 0, "tcs_bps": 0, "tds_bps": 0,
                            "pg_fee_bps": cards["pg_fee_bps"]}
    for code, c in channel_cards.items():
        partner_id = _partner_id(conn, code)
        card = conn.execute(
            """
            INSERT INTO channel_cards (partner_id, effective_from, commission_bps, fixed_paise,
                                       ship_extra_per_kg_paise, tcs_bps, tds_bps, pg_fee_bps, note)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'built-in demo card')
            ON CONFLICT (partner_id, effective_from) DO NOTHING RETURNING id
            """,
            (partner_id, _EFFECTIVE_FROM, c["commission_bps"], c["fixed_paise"],
             c["ship_extra_per_kg_paise"], c["tcs_bps"], c["tds_bps"], c.get("pg_fee_bps", 0)),
        ).fetchone()
        if card:  # only a brand-new card gets its slabs
            for max_g, fee in c["ship_slabs"]:
                conn.execute("INSERT INTO channel_ship_slabs (card_id, max_g, fee_paise) "
                             "VALUES (%s, %s, %s)", (card["id"], max_g, fee))

    for code, c in cards["courier_cards"].items():
        partner_id = _partner_id(conn, code)
        card = conn.execute(
            """
            INSERT INTO courier_cards (partner_id, effective_from, cod_flat_paise, cod_bps,
                                       return_pct, note)
            VALUES (%s, %s, %s, %s, %s, 'built-in demo card')
            ON CONFLICT (partner_id, effective_from) DO NOTHING RETURNING id
            """,
            (partner_id, _EFFECTIVE_FROM, c["cod_flat"], c["cod_bps"], c["return_pct"]),
        ).fetchone()
        if card:
            for zone in c["base"]:
                conn.execute(
                    "INSERT INTO courier_zone_rates (card_id, zone, base_paise, addl_paise) "
                    "VALUES (%s, %s, %s, %s)", (card["id"], zone, c["base"][zone], c["addl"][zone]))