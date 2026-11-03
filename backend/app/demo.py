"""Developer CLI: load the sample data set through the real import pipeline.

    python -m app.demo load ../sample_data/small            # seed + import all 12 files
    python -m app.demo load ../sample_data/xl --reset       # wipe first (benchmark runs)
    python -m app.demo reset                                # wipe all imported data
    python -m app.demo recon --as-of 2026-11-12             # runs the rule engine
"""

from __future__ import annotations

import argparse
import sys
import time
from datetime import date
from pathlib import Path

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row

from app.config import get_settings
from app.ingest.catalog import LOAD_ORDER
from app.ingest.pipeline import import_file
from app.recon.engine import run_reconciliation
from app.seed import seed

_WIPE = (
    "TRUNCATE findings, order_status, recon_runs, import_rejects, ledger_lines, "
    "warehouse_receipts, orders, imports, source_mappings, sources, zone_prefixes, "
    "partners RESTART IDENTITY CASCADE"
)

def reset(conn: Connection) -> None:
    conn.execute(_WIPE)
    conn.commit()


def load_dataset(conn: Connection, data_dir: Path, *, echo: bool = True) -> dict[str, object]:
    started = time.perf_counter()
    source_ids = seed(conn, data_dir)
    rows: list[dict[str, object]] = []
    if echo:
        print(f"{'file':<28}{'rows':>8}{'loaded':>8}{'reject':>7}{'dup':>6}"
              f"{'read':>7}{'xform':>7}{'load':>7}{'link':>7}{'total':>7}")
    for s in LOAD_ORDER:
        result = import_file(conn, source_ids[s.filename], data_dir / s.filename)
        t = result.timings
        rows.append({"file": s.filename, "result": result})
        if echo:
            print(f"{s.filename:<28}{result.rows_total:>8}{result.rows_loaded:>8}"
                  f"{result.rows_rejected:>7}{result.rows_duplicate:>6}"
                  f"{t.get('read', 0):>7.2f}{t.get('transform', 0):>7.2f}{t.get('load', 0):>7.2f}"
                  f"{t.get('link', 0):>7.2f}{t.get('total', 0):>7.2f}"
                  + ("" if result.status == "succeeded"
                     else f"  <- {result.status}: {result.error}"))
    summary = conn.execute(
        """
        SELECT (SELECT count(*) FROM orders) AS orders,
               (SELECT count(*) FROM ledger_lines) AS lines,
               (SELECT count(*) FROM ledger_lines WHERE order_id IS NULL AND order_ref IS NULL)
                   AS lines_without_ref,
               (SELECT count(*) FROM ledger_lines WHERE order_id IS NULL AND order_ref IS NOT NULL)
                   AS lines_orphaned,
               (SELECT count(*) FROM warehouse_receipts) AS receipts
        """
    ).fetchone()
    elapsed = round(time.perf_counter() - started, 2)
    if echo:
        print(f"\norders={summary['orders']}  ledger_lines={summary['lines']}  "
              f"receipts={summary['receipts']}")
        print(f"lines without an order id (review queue): {summary['lines_without_ref']}   "
              f"lines whose order is unknown: {summary['lines_orphaned']}")
        print(f"whole dataset: {elapsed}s")
    return {"seconds": elapsed, "summary": summary, "imports": rows}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    load_p = sub.add_parser("load", help="seed and import every file in a sample data folder")
    load_p.add_argument("dir", type=Path)
    load_p.add_argument("--reset", action="store_true", help="wipe imported data first")
    recon_p = sub.add_parser("recon", help="run the reconciliation rules engine")
    recon_p.add_argument("--as-of", type=date.fromisoformat, default=None,
                         help="judge overdue-ness as of this date (default: today)")
    sub.add_parser("reset", help="wipe all imported data (keeps users)")
    args = ap.parse_args()
    with psycopg.connect(get_settings().database_url, row_factory=dict_row) as conn:
        if args.cmd == "recon":
            r = run_reconciliation(conn, args.as_of or date.today())
            print(f"run {r.run_id} as of {r.as_of}: {r.orders} orders in "
                  f"{r.timings.get('total', 0)}s")
            for status, n in sorted(r.by_status.items(), key=lambda kv: -kv[1]):
                print(f"  {status:<22}{n:>8}")
            print(f"open findings {r.findings_open} (new {r.findings_new}, "
                  f"resolved {r.findings_resolved}); money at risk "
                  f"₹{r.money_at_risk_paise / 100:,.2f}")
            return 0
        if args.cmd == "reset":
            reset(conn)
            print("Imported data wiped.")
            return 0
        if args.reset:
            reset(conn)
        load_dataset(conn, args.dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())