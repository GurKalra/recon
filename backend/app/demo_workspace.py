"""The demo workspace: one click loads the bundled sample data through the real import pipeline,
builds the review queue and runs the reconciliation, so the screens open populated.

It only runs on an empty workspace; it never overwrites or wipes anything. The same sample files
can be downloaded and uploaded through the wizard to try the real flow by hand."""

from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path

from psycopg import Connection

from app import claims
from app.demo import load_dataset
from app.ingest.catalog import SOURCES
from app.recon import matching
from app.recon.engine import run_reconciliation


class DemoUnavailable(RuntimeError):
    """The workspace cannot be loaded (already has data, or the sample files are missing)."""


# the files a person may download: the 12 partner files. Never the answer key (truth.csv).
def sample_files() -> dict[str, dict]:
    return {s.filename: {"partner": s.partner, "kind": s.kind, "label": s.name}
            for s in SOURCES}


def has_data(conn: Connection) -> bool:
    row = conn.execute(
        "SELECT (SELECT count(*) FROM orders) + (SELECT count(*) FROM imports) AS n").fetchone()
    return row["n"] > 0


def dataset_today(directory: Path) -> date | None:
    try:
        return date.fromisoformat(json.loads((directory / "manifest.json").read_text())["today"])
    except (OSError, ValueError, KeyError):
        return None


def status(conn: Connection, directory: Path) -> dict:
    files = [{"name": name, "size_bytes": (directory / name).stat().st_size, **meta}
             for name, meta in sample_files().items() if (directory / name).is_file()]
    return {"loaded": has_data(conn), "available": len(files) == len(sample_files()),
            "as_of": str(dataset_today(directory) or ""), "files": files}


def load(conn: Connection, directory: Path) -> dict:
    """Seed the partners and sources, import the 12 files, build the review queue, run the
    reconciliation as of the day the data set was generated."""
    if has_data(conn):
        raise DemoUnavailable("This workspace already has data. The demo only loads into an empty "
                              "one.")
    as_of = dataset_today(directory)
    if as_of is None or not all((directory / n).is_file() for n in sample_files()):
        raise DemoUnavailable("The sample files are not installed on this server.")
    started = time.perf_counter()
    imported = load_dataset(conn, directory, echo=False)
    review = matching.refresh(conn)
    result = run_reconciliation(conn, as_of)
    claims.sync_after_run(conn)
    return {
        "as_of": str(as_of), "seconds": round(time.perf_counter() - started, 2),
        "orders": imported["summary"]["orders"], "ledger_lines": imported["summary"]["lines"],
        "payments_to_review": review["groups_open"],
        "money_at_risk_paise": result.money_at_risk_paise,
        "findings_open": result.findings_open,
    }