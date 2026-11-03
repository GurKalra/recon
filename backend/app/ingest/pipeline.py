"""Import one file end to end: verify, read, validate, load, link, record what happened."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from psycopg import Connection
from psycopg.types.json import Jsonb

from app.ingest import load
from app.ingest.mapping import MappingSpec
from app.ingest.reader import FileReadError, read_table
from app.ingest.transform import transform


@dataclass
class ImportResult:
    import_id: int
    status: str  # succeeded | duplicate | failed
    rows_total: int = 0
    rows_loaded: int = 0
    rows_rejected: int = 0
    rows_duplicate: int = 0
    warnings: list[dict[str, object]] = field(default_factory=list)
    timings: dict[str, float] = field(default_factory=dict)
    error: str | None = None


@contextmanager
def _timed(timings: dict[str, float], stage: str) -> Iterator[None]:
    start = time.perf_counter()
    try:
        yield
    finally:
        timings[stage] = round(time.perf_counter() - start, 3)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def import_file(conn: Connection, source_id: int, path: Path,
                created_by: int | None = None) -> ImportResult:
    """Import `path` using the source's approved mapping. Never raises for bad DATA: bad rows
    become rejects. A structural problem (missing columns, unreadable file) marks the import
    failed and loads nothing."""
    source = conn.execute(
        "SELECT s.partner_id, p.type AS partner_type FROM sources s "
        "JOIN partners p ON p.id = s.partner_id WHERE s.id = %s", (source_id,)
    ).fetchone()
    if source is None:
        raise ValueError(f"unknown source {source_id}")
    approved = conn.execute(
        "SELECT id, mapping, header_fingerprint FROM source_mappings "
        "WHERE source_id = %s AND status = 'approved'", (source_id,)
    ).fetchone()
    if approved is None:
        raise ValueError(f"source {source_id} has no approved mapping")

    sha, size = _sha256(path), path.stat().st_size
    existing = conn.execute(
        "SELECT id, rows_total, rows_loaded, rows_rejected, rows_duplicate FROM imports "
        "WHERE source_id = %s AND file_sha256 = %s AND status <> 'failed'", (source_id, sha)
    ).fetchone()
    if existing:
        return ImportResult(existing["id"], "duplicate", existing["rows_total"] or 0,
                            existing["rows_loaded"] or 0, existing["rows_rejected"] or 0,
                            existing["rows_duplicate"] or 0)

    import_id = conn.execute(
        "INSERT INTO imports (source_id, mapping_id, filename, file_sha256, file_bytes, status, "
        "created_by, started_at) VALUES (%s, %s, %s, %s, %s, 'running', %s, now()) RETURNING id",
        (source_id, approved["id"], path.name, sha, size, created_by),
    ).fetchone()["id"]
    conn.commit()  # the 'running' row must survive a later rollback

    timings: dict[str, float] = {}
    result = ImportResult(import_id, "succeeded", timings=timings)
    try:
        spec = MappingSpec.model_validate(approved["mapping"])
        with _timed(timings, "read"):
            table = read_table(path, spec.format, spec.referenced_columns())
        if table.fingerprint != approved["header_fingerprint"]:
            result.warnings.append({"code": "header_fingerprint_changed",
                                    "note": "the partner may have changed their file layout"})
        partner_types = {r["code"]: r["type"] for r in
                         conn.execute("SELECT code, type FROM partners").fetchall()}
        with _timed(timings, "transform"):
            out = transform(table, spec, partner_types)
        result.warnings += out.warnings
        with _timed(timings, "load"):
            counts = _load(conn, spec, import_id, source, out.good)
        if spec.kind == "orders":  # settlements that arrived before their orders get linked now
            with _timed(timings, "link"):
                link_counts = load.relink_unlinked(conn)
            if link_counts["ledger_lines_linked"] or link_counts["receipts_linked"]:
                result.warnings.append({"code": "linked_late", **link_counts})
        if counts.in_file_duplicates:
            result.warnings.append({
                "code": "in_file_duplicates", "rows": counts.in_file_duplicates,
                "note": "stored and flagged, never double counted"})
        result.rows_total, result.rows_rejected = out.rows_total, out.rejects_total
        result.rows_loaded, result.rows_duplicate = counts.loaded, counts.skipped
        timings["total"] = round(sum(timings.values()), 3)
        for reject in out.rejects:
            conn.execute(
                "INSERT INTO import_rejects (import_id, row_no, reason, raw) "
                "VALUES (%s, %s, %s, %s)",
                (import_id, reject["row_no"], reject["reason"], Jsonb(reject["raw"])),
            )
        conn.execute(
            "UPDATE imports SET status = 'succeeded', stage = 'done', progress = 1, "
            "rows_total = %s, rows_loaded = %s, rows_rejected = %s, rows_duplicate = %s, "
            "stage_timings = %s, warnings = %s, finished_at = now() WHERE id = %s",
            (result.rows_total, result.rows_loaded, result.rows_rejected, result.rows_duplicate,
             Jsonb(timings), Jsonb(result.warnings), import_id),
        )
        conn.commit()
    except Exception as exc:  # structural failure: nothing from this file is kept
        conn.rollback()
        known = isinstance(exc, FileReadError)
        result.status = "failed"
        result.error = str(exc) if known else f"{type(exc).__name__}: {exc}"
        conn.execute(
            "UPDATE imports SET status = 'failed', error = %s, finished_at = now() WHERE id = %s",
            (result.error, import_id),
        )
        conn.commit()
        if not known:
            raise
    return result


def _load(conn: Connection, spec: MappingSpec, import_id: int, source, good):
    if spec.kind == "ledger":
        return load.load_ledger(
            conn, import_id, source["partner_id"], source["partner_type"], good)
    if spec.kind == "orders":
        return load.load_orders(conn, import_id, good)
    return load.load_receipts(conn, import_id, good)


def describe(result: ImportResult) -> str:
    return json.dumps({"status": result.status, "loaded": result.rows_loaded,
                       "rejected": result.rows_rejected, "timings": result.timings})