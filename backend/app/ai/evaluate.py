"""How good are the mapping suggestions? Run them on the 12 sample files and compare what the
suggested mapping PRODUCES with what the hand-approved mapping produces.

    python -m app.ai.evaluate ../sample_data/small            # rules only
    python -m app.ai.evaluate ../sample_data/small --llm      # rules, then the model if unsure
    python -m app.ai.evaluate ../sample_data/small --force-llm --only m3,pg   # some files

A suggestion counts as correct when the rows it yields are identical to the gold mapping's
(zero-amount lines ignored), not when the JSON looks the same: two different specs that read
the file identically are both right.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from app.ai.llm import LLMClient
from app.ai.rules import Hint
from app.ai.suggest import suggest_mapping
from app.ingest.catalog import SOURCES, SourceDef
from app.ingest.mapping import MappingSpec
from app.ingest.normalize import canonical
from app.ingest.reader import read_table
from app.ingest.transform import transform

_LEDGER_COLS = ["row_no", "order_ref", "line_type", "amount_paise", "occurred_on", "reference",
                "awb", "extras"]


@dataclass
class Row:
    file: str
    source: str       # rules | llm | llm_repaired | none
    identical: bool
    detail: str
    model: str = ""   # "", "ok", or what the model got wrong (only when the model was asked)


def _outcome(path: Path, spec: MappingSpec, partner_types: dict[str, str]) -> pl.DataFrame:
    table = read_table(path, spec.format, spec.referenced_columns())
    good = transform(table, spec, partner_types).good
    if spec.kind == "ledger":
        good = good.filter(pl.col("amount_paise") != 0).select(_LEDGER_COLS)
        return good.sort("row_no", "line_type", "amount_paise")
    return good.sort("row_no")


def compare(path: Path, suggested: dict | None, gold: SourceDef,
            partner_types: dict[str, str]) -> tuple[bool, str]:
    if suggested is None:
        return False, "no mapping produced"
    try:
        got = _outcome(path, MappingSpec.model_validate(suggested), partner_types)
    except Exception as exc:
        return False, f"suggested mapping failed: {str(exc)[:120]}"
    want = _outcome(path, gold.spec(), partner_types)
    if got.shape != want.shape:
        return False, f"{got.height} rows, expected {want.height}"
    cols = [c for c in want.columns if c in got.columns]
    if got.select(cols).equals(want.select(cols)):
        return True, ""
    diff = [c for c in cols if not got[c].equals(want[c])]
    return False, f"columns differ: {diff}"


def known_refs(data_dir: Path, partner_types: dict[str, str]) -> frozenset[str]:
    orders = next(s for s in SOURCES if s.kind == "orders")
    spec = orders.spec()
    table = read_table(data_dir / orders.filename, spec.format, spec.referenced_columns())
    col = spec.orders.order_ref.lower()  # type: ignore[union-attr]
    return frozenset(table.df.select(canonical(pl.col(col)))[col].drop_nulls().to_list())


def evaluate(data_dir: Path, use_llm: bool = False, force_llm: bool = False,
             pause: float = 0.0, only: list[str] | None = None,
             on_row: Callable[[Row], None] | None = None) -> list[Row]:
    rates = json.loads((data_dir / "rate_cards.json").read_text(encoding="utf-8"))
    types = {p["code"]: p["type"] for p in rates["partners"]}
    refs = known_refs(data_dir, types)
    rows = []
    llm = LLMClient.from_settings()
    llm.on_wait = lambda secs, why: print(f"      ... rate limited ({why}), waiting {secs:.0f}s",
                                          file=sys.stderr, flush=True)
    for src in SOURCES:
        if only and not any(o in src.filename for o in only):
            continue
        path = data_dir / src.filename
        hint = Hint(source_kind=src.kind, partner_type=types[src.partner],
                    known_refs=None if src.kind == "orders" else refs)
        s = suggest_mapping(path, hint, use_llm=use_llm, partner_types=types,
                            force_llm=force_llm, llm=llm)
        ok, detail = compare(path, s.spec, src, types)
        model = ""
        if s.model_candidate is not None:
            m_ok, m_detail = compare(path, s.model_candidate.spec, src, types)
            model = "ok" if m_ok else (m_detail or "failed")
        row = Row(src.filename, s.source, ok, detail or "; ".join(s.warnings), model)
        rows.append(row)
        if on_row:
            on_row(row)
        if pause and (use_llm or force_llm):
            time.sleep(pause)  # stay under the provider's per-minute limits
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("dir", type=Path)
    ap.add_argument("--llm", action="store_true", help="also ask the model (needs GROQ_API_KEY)")
    ap.add_argument("--force-llm", action="store_true",
                    help="ask the model about every file, not only the uncertain ones")
    ap.add_argument("--pause", type=float, default=0.0,
                    help="seconds to wait between files when the model is used")
    ap.add_argument("--only", default="",
                    help="comma-separated parts of file names, e.g. m3,pg,cod_remittance_cb")
    args = ap.parse_args()

    def show(r: Row) -> None:
        model = f"  model: {r.model}" if r.model else ""
        print(f"  {'ok  ' if r.identical else 'FAIL'} {r.file:<28}{r.source:<14}{r.detail}{model}",
              flush=True)

    only = [o.strip() for o in args.only.split(",") if o.strip()]
    rows = evaluate(args.dir, args.llm or args.force_llm, args.force_llm, args.pause, only,
                    show)
    good = sum(r.identical for r in rows)
    print(f"{good}/{len(rows)} files read identically to the hand-approved mapping")
    asked = [r for r in rows if r.model]
    if asked:
        print(f"the model on its own: {sum(r.model == 'ok' for r in asked)}/{len(asked)} correct")
    return 0 if good == len(rows) else 1


if __name__ == "__main__":
    sys.exit(main())