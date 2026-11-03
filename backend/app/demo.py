"""Developer CLI: load the sample data set through the real import pipeline.

    python -m app.demo load ../sample_data/small            # seed + import all 12 files
    python -m app.demo load ../sample_data/xl --reset       # wipe first (benchmark runs)
    python -m app.demo reset                                # wipe all imported data
    python -m app.demo recon --as-of 2026-11-12             # runs the rule engine
    python -m app.demo review                               # build the review queue
    python -m app.demo review --ai                          # ... and ask the model too
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
from app.recon import matching
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


def _review_with_ai(conn: Connection, args: argparse.Namespace) -> None:
    import csv

    from app.ai.llm import LLMClient
    from app.ai.rerank import rerank

    llm = LLMClient.from_settings()
    llm.on_wait = lambda secs, why: print(f"      ... rate limited ({why}), waiting {secs:.0f}s",
                                          file=sys.stderr, flush=True)
    started = time.perf_counter()
    st = rerank(conn, llm, everything=args.all, limit=args.limit)
    print(f"asked the model about {st.asked} payments in {time.perf_counter() - started:.0f}s: "
          f"agreed with the rules' first choice {st.agreed}, picked another {st.other}, "
          f"said unsure {st.none}, errors {st.errors}, tokens {st.tokens}")
    for note in st.notes:
        print("  note:", note)
    if not args.truth:
        return
    flagged = {r["order_ref"] for r in csv.DictReader(args.truth.open(encoding="utf-8"))
               if "missing_order_id" in r["flags"]}
    rules_ok = ai_ok = ai_wrong = 0
    for grp in matching.queue(conn, limit=100000):
        if grp["ai_verdict"] is None or not grp["suggestions"]:
            continue
        top = grp["suggestions"][0]["order_ref_raw"] in flagged
        pick = next((s for s in grp["suggestions"] if s["ai_pick"]), None)
        rules_ok += top
        if pick is not None:
            ai_ok += pick["order_ref_raw"] in flagged
            ai_wrong += pick["order_ref_raw"] not in flagged
    print(f"on those payments: the rules' first choice was right {rules_ok} times; the model's "
          f"pick was right {ai_ok} times and wrong {ai_wrong} times")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    load_p = sub.add_parser("load", help="seed and import every file in a sample data folder")
    load_p.add_argument("dir", type=Path)
    load_p.add_argument("--reset", action="store_true", help="wipe imported data first")
    review_p = sub.add_parser("review", help="find payments with no order ID and suggest "
                                             "their orders")
    review_p.add_argument("--ai", action="store_true",
                          help="also ask the model about the payments the rules are unsure of")
    review_p.add_argument("--all", action="store_true",
                          help="with --ai: ask about every payment, not only the unsure ones")
    review_p.add_argument("--limit", type=int, default=50, help="with --ai: how many payments")
    review_p.add_argument("--truth", type=Path, default=None,
                          help="with --ai: truth.csv of a generated data set, to score the picks")
    recon_p = sub.add_parser("recon", help="run the reconciliation rules engine")
    recon_p.add_argument("--as-of", type=date.fromisoformat, default=None,
                         help="judge overdue-ness as of this date (default: today)")
    sub.add_parser("reset", help="wipe all imported data (keeps users)")
    args = ap.parse_args()
    with psycopg.connect(get_settings().database_url, row_factory=dict_row) as conn:
        if args.cmd == "review":
            start = time.perf_counter()
            stats = matching.refresh(conn)
            print(f"{stats['groups_open']} payments need review, {stats['suggestions']} "
                  f"suggestions ({time.perf_counter() - start:.2f}s)")
            if args.ai:
                _review_with_ai(conn, args)
                return 0
            for grp in matching.queue(conn, limit=10):
                best = grp["suggestions"][0] if grp["suggestions"] else None
                label = (f"{best['order_ref_raw']} ({best['confidence']}, {best['score']})"
                         if best else "no candidate")
                print(f"  {grp['partner_code']:<4}{grp['occurred_on']}  "
                      f"₹{(grp['sale_paise'] or 0) / 100:>10,.2f}  -> {label}")
            return 0
        
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