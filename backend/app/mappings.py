"""The mapping wizard's logic: suggest, preview, approve, version, roll back, spot format drift.

A mapping is only ever *approved by a person*; the AI and the rules only propose. Approving
writes a new version and retires the old one, so the history is never rewritten and a rollback is
just another new version copied from an old one.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path

from psycopg import Connection
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from app.ai.mask import is_sensitive_header
from app.ai.profile import FileProfile, profile_file
from app.ai.rules import Hint
from app.ai.suggest import Suggestion, suggest_mapping
from app.ingest.mapping import MappingSpec
from app.ingest.reader import FileReadError, read_table
from app.ingest.transform import transform

SOURCE_KINDS = ("orders", "settlement", "cod_remittance", "freight_invoice", "warehouse_receipt")
PREVIEW_ROWS = 20
SAMPLE_ROWS = 2000
MAX_REJECT_RATE = 0.02       # the blueprint's 98% parse-success rule


class MappingInvalid(ValueError):
    """The mapping cannot be used; the message says why in plain words."""


def spec_kind_for(source_kind: str) -> str:
    return {"orders": "orders", "warehouse_receipt": "receipts"}.get(source_kind, "ledger")


# ---- facts from the database -------------------------------------------------------------------


def partner_types(conn: Connection) -> dict[str, str]:
    return {r["code"]: r["type"] for r in conn.execute("SELECT code, type FROM partners")}


def known_refs(conn: Connection) -> frozenset[str]:
    return frozenset(r["order_ref"] for r in conn.execute("SELECT order_ref FROM orders"))


def _partner(conn: Connection, partner_id: int) -> dict:
    row = conn.execute("SELECT id, code, name, type FROM partners WHERE id = %s AND is_active",
                       (partner_id,)).fetchone()
    if row is None:
        raise MappingInvalid("No such partner.")
    return row


def detect_sources(conn: Connection, fingerprint: str) -> list[dict]:
    """Sources whose approved mapping was made for exactly this header row."""
    return conn.execute(
        """SELECT s.id AS source_id, s.name, s.kind, p.id AS partner_id, p.code AS partner_code,
                  p.name AS partner_name, m.version
           FROM source_mappings m JOIN sources s ON s.id = m.source_id
           JOIN partners p ON p.id = s.partner_id
           WHERE m.status = 'approved' AND m.header_fingerprint = %s
           ORDER BY p.code, s.name""", (fingerprint,)).fetchall()


def approved_examples(conn: Connection) -> list[tuple[list[str], dict]]:
    """Every approved mapping as (columns it uses, spec): the AI's worked examples."""
    out = []
    for r in conn.execute("SELECT mapping FROM source_mappings WHERE status = 'approved'"):
        try:
            out.append((MappingSpec.model_validate(r["mapping"]).referenced_columns(),
                        r["mapping"]))
        except ValidationError:
            continue
    return out


# ---- describing a file -------------------------------------------------------------------------


def profile_summary(p: FileProfile) -> dict:
    """What the screen shows about an unknown file. Samples are already masked."""
    return {
        "header_row": p.header_row, "rows": p.rows, "file_type": p.format.kind,
        "footer_markers": list(p.format.footer_markers), "fingerprint": p.table.fingerprint,
        "columns": [{"name": c.name, "looks_like": c.kind, "filled": round(c.filled, 3),
                     "distinct": c.distinct, "samples": c.samples[:4], "sensitive": c.sensitive}
                    for c in p.columns],
    }


def ai_status(use_ai: bool, source: str, attempts: list[str]) -> str:
    """Why the answer came from the AI or from the rules, in one word the screen can explain."""
    if not use_ai:
        return "off"
    if any(a.startswith("no API key") for a in attempts):
        return "no_key"
    if any(a.startswith("model unavailable") for a in attempts):
        return "unavailable"
    if source.startswith("llm"):
        return "used"
    if any(a.startswith("model round") for a in attempts):
        return "asked_kept"
    return "not_needed"


def suggestion_dict(s: Suggestion, use_ai: bool = True) -> dict:
    return {
        "ai_status": ai_status(use_ai, s.source, s.attempts),
        "mapping": s.spec, "produced_by": s.source,
        "check": {"ok": s.check.ok, "errors": s.check.errors, "rows_checked": s.check.rows,
                  "rows_rejected": s.check.rejects},
        "notes": [{"field": n.field, "value": n.value, "confidence": n.confidence,
                   "reason": n.reason} for n in s.notes],
        "warnings": s.warnings, "unmapped_columns": s.unmapped, "attempts": s.attempts,
        "model": s.llm_model, "tokens": s.llm_tokens,
    }


def suggest(conn: Connection, path: Path, partner_id: int, source_kind: str,
            use_ai: bool = True) -> dict:
    if source_kind not in SOURCE_KINDS:
        raise MappingInvalid(f"Source kind must be one of {', '.join(SOURCE_KINDS)}.")
    partner = _partner(conn, partner_id)
    hint = Hint(source_kind=source_kind, partner_type=partner["type"],
                known_refs=known_refs(conn) if source_kind != "orders" else None)
    s = suggest_mapping(path, hint, use_llm=use_ai, partner_types=partner_types(conn),
                        extra_examples=approved_examples(conn))
    return suggestion_dict(s, use_ai)


# ---- preview -----------------------------------------------------------------------------------


def _json_value(v: object) -> object:
    return v.isoformat() if hasattr(v, "isoformat") else v


def preview(path: Path, spec_dict: dict, types: dict[str, str], source_kind: str | None = None,
            rows: int = PREVIEW_ROWS) -> dict:
    """Run the real transform on the first rows and show what would be stored and what would be
    refused. Nothing is written."""
    try:
        spec = MappingSpec.model_validate(spec_dict)
    except ValidationError as exc:
        raise MappingInvalid("The mapping is not valid: " + "; ".join(
            f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:5])) from exc
    if source_kind and spec.kind != spec_kind_for(source_kind):
        raise MappingInvalid(f"A {source_kind} source needs a '{spec_kind_for(source_kind)}' "
                             f"mapping, not '{spec.kind}'.")
    try:
        table = read_table(path, spec.format, spec.referenced_columns())
    except FileReadError as exc:
        raise MappingInvalid(str(exc)) from exc
    sample = dataclasses.replace(table, df=table.df.head(SAMPLE_ROWS))
    out = transform(sample, spec, types)
    good = out.good.drop([c for c in ("extras",) if c in out.good.columns])
    rejects = []
    for r in out.rejects[:rows]:
        raw = {k: v for k, v in r["raw"].items() if not is_sensitive_header(k)}
        rejects.append({"row_no": r["row_no"], "reason": r["reason"], "raw": raw})
    rate = out.rejects_total / max(out.rows_total, 1)
    warnings = [str(w.get("note") or w.get("code")) for w in out.warnings]
    if spec.kind == "orders" and "order_ref" in good.columns:
        dupes = good.height - good["order_ref"].n_unique()
        if dupes:
            warnings.append(f"{dupes} repeated order IDs in the first {out.rows_total} rows.")
    if spec.kind == "ledger" and spec.ledger and not spec.ledger.order_ref:
        warnings.append("No order ID column: payments will wait in the review queue.")
    return {
        "rows_in_file": table.df.height, "rows_checked": out.rows_total,
        "rows_rejected": out.rejects_total, "reject_rate": round(rate, 4),
        "ok": rate <= MAX_REJECT_RATE and out.rows_total > 0,
        "columns": [c for c in good.columns if c != "row_no"],
        "rows": [{k: _json_value(v) for k, v in row.items()} for row in good.head(rows).to_dicts()],
        "rejects": rejects, "warnings": warnings,
        "fingerprint": table.fingerprint,
    }


# ---- versions ----------------------------------------------------------------------------------


@dataclass
class Approved:
    source_id: int
    mapping_id: int
    version: int
    created: bool          # False when this exact mapping was already the approved one


def approve(conn: Connection, user_id: int | None, path: Path, partner_id: int,
            source_kind: str, name: str, spec_dict: dict, types: dict[str, str],
            note: str | None = None) -> Approved:
    """Make `spec_dict` the approved mapping for the (partner, kind, name) source. Refused unless
    it really works on this file."""
    if source_kind not in SOURCE_KINDS:
        raise MappingInvalid(f"Source kind must be one of {', '.join(SOURCE_KINDS)}.")
    name = name.strip()
    if not name:
        raise MappingInvalid("Give the source a name.")
    _partner(conn, partner_id)
    result = preview(path, spec_dict, types, source_kind)
    if not result["ok"]:
        raise MappingInvalid(
            f"This mapping would reject {result['rows_rejected']} of {result['rows_checked']} "
            f"rows ({result['reject_rate']:.0%}); at most {MAX_REJECT_RATE:.0%} is allowed.")
    spec = MappingSpec.model_validate(spec_dict)
    stored = spec.model_dump(mode="json", exclude_none=True)
    source_id = conn.execute(
        """INSERT INTO sources (partner_id, kind, name) VALUES (%s, %s, %s)
           ON CONFLICT (partner_id, kind, name) DO UPDATE SET name = EXCLUDED.name
           RETURNING id""", (partner_id, source_kind, name)).fetchone()["id"]
    current = conn.execute(
        "SELECT id, version, mapping, header_fingerprint FROM source_mappings "
        "WHERE source_id = %s AND status = 'approved' FOR UPDATE", (source_id,)).fetchone()
    if current and current["mapping"] == stored \
            and current["header_fingerprint"] == result["fingerprint"]:
        conn.commit()
        return Approved(source_id, current["id"], current["version"], False)
    return _new_version(conn, source_id, stored, result["fingerprint"], user_id, note)


def _new_version(conn: Connection, source_id: int, stored: dict, fingerprint: str,
                 user_id: int | None, note: str | None) -> Approved:
    version = conn.execute("SELECT coalesce(max(version), 0) + 1 AS v FROM source_mappings "
                           "WHERE source_id = %s", (source_id,)).fetchone()["v"]
    conn.execute("UPDATE source_mappings SET status = 'superseded' WHERE source_id = %s "
                 "AND status = 'approved'", (source_id,))
    mapping_id = conn.execute(
        """INSERT INTO source_mappings (source_id, version, mapping, header_fingerprint, status,
                                        note, created_by, approved_by, approved_at)
           VALUES (%s, %s, %s, %s, 'approved', %s, %s, %s, now()) RETURNING id""",
        (source_id, version, Jsonb(stored), fingerprint, note, user_id, user_id)
    ).fetchone()["id"]
    conn.commit()
    return Approved(source_id, mapping_id, version, True)


def history(conn: Connection, source_id: int) -> list[dict]:
    return conn.execute(
        """SELECT m.id, m.version, m.status, m.note, m.created_at, m.approved_at,
                  m.header_fingerprint, m.mapping, u.full_name AS approved_by
           FROM source_mappings m LEFT JOIN users u ON u.id = m.approved_by
           WHERE m.source_id = %s ORDER BY m.version DESC""", (source_id,)).fetchall()


def rollback(conn: Connection, source_id: int, version: int, user_id: int | None) -> Approved:
    """Make an old version current again by copying it into a NEW version."""
    old = conn.execute("SELECT mapping, header_fingerprint FROM source_mappings "
                       "WHERE source_id = %s AND version = %s", (source_id, version)).fetchone()
    if old is None:
        raise MappingInvalid("No such version.")
    current = conn.execute("SELECT version FROM source_mappings WHERE source_id = %s "
                           "AND status = 'approved'", (source_id,)).fetchone()
    if current and current["version"] == version:
        raise MappingInvalid("That version is already the current one.")
    return _new_version(conn, source_id, old["mapping"], old["header_fingerprint"], user_id,
                        f"rolled back to version {version}")


def list_sources(conn: Connection) -> list[dict]:
    return conn.execute(
        """SELECT s.id, s.name, s.kind, p.id AS partner_id, p.code AS partner_code,
                  p.name AS partner_name, m.version AS approved_version,
                  m.approved_at, m.header_fingerprint,
                  (SELECT max(i.finished_at) FROM imports i
                   WHERE i.source_id = s.id AND i.status = 'succeeded') AS last_import_at,
                  (SELECT count(*) FROM imports i
                   WHERE i.source_id = s.id AND i.status = 'succeeded') AS imports
           FROM sources s JOIN partners p ON p.id = s.partner_id
           LEFT JOIN source_mappings m ON m.source_id = s.id AND m.status = 'approved'
           ORDER BY p.code, s.kind, s.name""").fetchall()


# ---- drift -------------------------------------------------------------------------------------


def drift(conn: Connection, source_id: int, path: Path) -> dict | None:
    """None when the file has the layout the approved mapping was made for. Otherwise what changed:
    columns the mapping needs that are gone, and columns that are new."""
    current = conn.execute(
        "SELECT mapping, header_fingerprint, version FROM source_mappings "
        "WHERE source_id = %s AND status = 'approved'", (source_id,)).fetchone()
    if current is None:
        raise MappingInvalid("This source has no approved mapping yet.")
    spec = MappingSpec.model_validate(current["mapping"])
    try:
        prof = profile_file(path)     # finds the header row itself, as the wizard did
    except FileReadError as exc:
        raise MappingInvalid(str(exc)) from exc
    table = prof.table
    if table.fingerprint == current["header_fingerprint"]:
        return None
    present = set(table.header)
    needed = spec.referenced_columns()
    return {
        "approved_version": current["version"],
        "missing_columns": [c for c in needed if c not in present],
        "new_columns": [c for c in table.header if c not in set(needed)],
        "still_works": all(c in present for c in needed),
        "file_columns": prof.names(),
    }