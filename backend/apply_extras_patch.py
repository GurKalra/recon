#!/usr/bin/env python
"""One-time patch: optional extra columns (SKU, city, remarks) + the AI re-ranker's schema hooks.

Run from ~/Projects/recon/backend:   python apply_extras_patch.py
It checks that every edit can be made BEFORE touching any file. If it stops with "CANNOT APPLY",
nothing was changed: tell Claude which file and message, and it will send that file in full.
You can delete this script afterwards.
"""

import sys
from pathlib import Path

OPS = []


def op(path, old, new):
    OPS.append((path, old, new))




# ======================= app/ingest/mapping.py =======================
M = "app/ingest/mapping.py"
op(M, "import hashlib\n", "import hashlib\nimport re\n")
op(M, "from typing import Literal\n", "from typing import Literal, Self\n")
op(M, '''class LedgerFields(_Strict):
    order_ref: str | None = None''', '''_EXTRA_KEY = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
MAX_EXTRAS = 10


def _check_extras(extras: dict[str, str]) -> dict[str, str]:
    """Optional extra columns kept as-is (SKU, city, remarks...): {key in our data: column}."""
    if len(extras) > MAX_EXTRAS:
        raise ValueError(f"at most {MAX_EXTRAS} extra columns can be kept")
    for key in extras:
        if not _EXTRA_KEY.match(key):
            raise ValueError(f"extra column key {key!r} must be lower-case letters, digits "
                             "and underscores")
    return extras


class LedgerFields(_Strict):
    order_ref: str | None = None''')
op(M, '''    emitters: tuple[Emitter, ...] = Field(min_length=1)
    net_check: NetCheck | None = None
''', '''    emitters: tuple[Emitter, ...] = Field(min_length=1)
    net_check: NetCheck | None = None
    extras: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _extras_are_valid(self) -> Self:
        _check_extras(self.extras)
        return self
''')
op(M, '''    delivered_on: DateCol | None = None
    returned_on: DateCol | None = None


class ReceiptFields''', '''    delivered_on: DateCol | None = None
    returned_on: DateCol | None = None
    extras: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _extras_are_valid(self) -> Self:
        _check_extras(self.extras)
        return self


class ReceiptFields''')
op(M, '''            if ledger.net_check:
                add(ledger.net_check.column)''', '''            if ledger.net_check:
                add(ledger.net_check.column)
            for col in ledger.extras.values():
                add(col)''')
op(M, '''            for value in self.orders.model_dump().values():
                if isinstance(value, str):
                    add(value)
                elif isinstance(value, dict):
                    add(value["column"])''', '''            for name, value in self.orders.model_dump().items():
                if name == "extras":
                    for col in value.values():
                        add(col)
                elif isinstance(value, str):
                    add(value)
                elif isinstance(value, dict):
                    add(value["column"])''')

# ======================= app/ingest/transform.py =======================
T = "app/ingest/transform.py"
op(T, '''_NO_TEXT = pl.lit(None, dtype=pl.Utf8)
''', '''_NO_TEXT = pl.lit(None, dtype=pl.Utf8)


def _extras(extras: dict[str, str]) -> pl.Expr:
    """The optional extra columns of a row as one JSON object (text), or null. Empty cells are
    dropped by the database (jsonb_strip_nulls) when the row is stored."""
    if not extras:
        return _NO_TEXT
    fields = [nz.text(norm_header(col)).alias(key) for key, col in extras.items()]
    return pl.struct(fields).struct.json_encode()
''')
op(T, '''    cols.append((nz.text(ref_col) if ref_col else _NO_TEXT).alias("order_ref_raw"))''',
   '''    cols.append((nz.text(ref_col) if ref_col else _NO_TEXT).alias("order_ref_raw"))
    cols.append(_extras(f.extras).alias("extras"))''')
op(T, '''            (pl.col(f"_amt{i}") * e.sign).alias("amount_paise"),
            "occurred_on", "reference",''', '''            (pl.col(f"_amt{i}") * e.sign).alias("amount_paise"),
            "occurred_on", "reference", "extras",''')
op(T, '''    pin = nz.text(c(f.pincode)) if f.pincode else _NO_TEXT
    cols += [pin.alias("pincode"),''', '''    cols.append(_extras(f.extras).alias("extras"))
    pin = nz.text(c(f.pincode)) if f.pincode else _NO_TEXT
    cols += [pin.alias("pincode"),''')
op(T, '''"weight_g", "length_cm", "width_cm", "height_cm", "pincode", "delivery_status")
    return result''', '''"weight_g", "length_cm", "width_cm", "height_cm", "pincode", "delivery_status", "extras")
    return result''')

# ======================= app/ingest/load.py =======================
L = "app/ingest/load.py"
op(L, "line_type text, amount_paise bigint, occurred_on date, reference text\"\"\",",
   "line_type text, amount_paise bigint, occurred_on date, reference text, extras text\"\"\",")
op(L, "pincode text, delivery_status text\"\"\",", "pincode text, delivery_status text, extras text\"\"\",")
op(L, '''                                  line_type, amount_paise, occurred_on, reference, line_hash,
                                  dup_seq, source_row)''', '''                                  line_type, amount_paise, occurred_on, reference, line_hash,
                                  dup_seq, source_row, extras)''')
op(L, '''               seq.amount_paise, seq.occurred_on, seq.reference, seq.line_hash, seq.dup_seq,
               seq.row_no
        FROM seq''', '''               seq.amount_paise, seq.occurred_on, seq.reference, seq.line_hash, seq.dup_seq,
               seq.row_no, NULLIF(jsonb_strip_nulls(seq.extras::jsonb), '{{}}'::jsonb)
        FROM seq''')
op(L, '''                                value_paise, weight_g, length_cm, width_cm, height_cm, pincode,
                                delivery_status, import_id)''', '''                                value_paise, weight_g, length_cm, width_cm, height_cm, pincode,
                                delivery_status, import_id, extras)''')
op(L, '''                   s.length_cm, s.width_cm, s.height_cm, s.pincode, s.delivery_status, %(imp)s
            FROM (SELECT''', '''                   s.length_cm, s.width_cm, s.height_cm, s.pincode, s.delivery_status, %(imp)s,
                   NULLIF(jsonb_strip_nulls(s.extras::jsonb), '{}'::jsonb)
            FROM (SELECT''')
op(L, '''                import_id = EXCLUDED.import_id, updated_at = now()
            WHERE (orders.courier_partner_id, orders.awb, orders.delivered_on,
                   orders.returned_on, orders.delivery_status, orders.value_paise)
                  IS DISTINCT FROM
                  (EXCLUDED.courier_partner_id, EXCLUDED.awb, EXCLUDED.delivered_on,
                   EXCLUDED.returned_on, EXCLUDED.delivery_status, EXCLUDED.value_paise)''',
   '''                extras = coalesce(EXCLUDED.extras, orders.extras),
                import_id = EXCLUDED.import_id, updated_at = now()
            WHERE (orders.courier_partner_id, orders.awb, orders.delivered_on,
                   orders.returned_on, orders.delivery_status, orders.value_paise,
                   orders.extras)
                  IS DISTINCT FROM
                  (EXCLUDED.courier_partner_id, EXCLUDED.awb, EXCLUDED.delivered_on,
                   EXCLUDED.returned_on, EXCLUDED.delivery_status, EXCLUDED.value_paise,
                   coalesce(EXCLUDED.extras, orders.extras))''')

# ======================= app/ingest/catalog.py =======================
C = "app/ingest/catalog.py"
op(C, '''            "returned_on": {"column": "Returned On", "format": "iso"},
        }}),''', '''            "returned_on": {"column": "Returned On", "format": "iso"},
            "extras": {"sku": "SKU", "city": "City"},
        }}),''')
op(C, '''        order_ref="Order ID", reference="UTR",
        date={"column": "Settlement Date", "format": "dmy"},''', '''        order_ref="Order ID", reference="UTR", extras={"sku": "SKU"},
        date={"column": "Settlement Date", "format": "dmy"},''')
op(C, '''        _CSV, order_ref="order_id", reference="utr",
        date={"column": "settled_on", "format": "dmony"},''', '''        _CSV, order_ref="order_id", reference="utr",
        extras={"city": "city", "remarks": "remarks"},
        date={"column": "settled_on", "format": "dmony"},''')
op(C, '''        _CSV, order_ref="Order No", date={"column": "Payout Date", "format": "dmy"},''',
   '''        _CSV, order_ref="Order No", date={"column": "Payout Date", "format": "dmy"},
        extras={"store_city": "Store City"},''')
op(C, '''        _CSV, order_ref="Order ID", date={"column": "Settled On", "format": "iso"},
        emitters=[
            {"amount": "Gross (INR)"''', '''        _CSV, order_ref="Order ID", date={"column": "Settled On", "format": "iso"},
        extras={"narration": "Narration"},
        emitters=[
            {"amount": "Gross (INR)"''')

# ======================= app/ai/evaluate.py =======================
E = "app/ai/evaluate.py"
op(E, '''_LEDGER_COLS = ["row_no", "order_ref", "line_type", "amount_paise", "occurred_on", "reference",
                "awb"]''', '''_LEDGER_COLS = ["row_no", "order_ref", "line_type", "amount_paise", "occurred_on", "reference",
                "awb", "extras"]''')

# ======================= app/ai/rules.py =======================
R = "app/ai/rules.py"
op(R, '''def _used(spec: dict) -> set[str]:''', '''_EXTRA_NAME = re.compile(r"sku|item|product|title|city|state|region|store|remark|narration|"
                         r"description|note|comment")


def _extras_for(profile: FileProfile, used: set[str]) -> dict[str, str]:
    """Unmapped columns worth keeping as extras: things that tell similar orders apart (SKU,
    city, remarks). Columns that look personal (names, phones, addresses) are never kept."""
    found: dict[str, str] = {}
    for c in profile.columns:
        if c.name in used or c.sensitive or c.kind in ("empty",) or not _EXTRA_NAME.search(c.name):
            continue
        key = re.sub(r"[^a-z0-9]+", "_", c.name).strip("_")
        key = key if key[:1].isalpha() else f"c_{key}"
        found[key[:31]] = c.name
    return dict(list(found.items())[:10])


def _used(spec: dict) -> set[str]:''')
op(R, '''    spec["ledger"] = {k: v for k, v in spec["ledger"].items() if v is not None}
    unmapped = [c.name for c in profile.columns if c.name not in _used(spec)]''',
   '''    spec["ledger"] = {k: v for k, v in spec["ledger"].items() if v is not None}
    extras = _extras_for(profile, _used(spec))
    if extras:
        spec["ledger"]["extras"] = extras
        for key, col in extras.items():
            notes.append(Note(f"extra:{key}", col, "medium", "kept as an extra column"))
    unmapped = [c.name for c in profile.columns if c.name not in _used(spec)]''')
op(R, '''    spec = {"kind": "orders", "format": _format_dict(profile), "orders": fields}
    return Draft(spec, notes, warnings,''', '''    extras = _extras_for(profile, used)
    if extras:
        fields["extras"] = extras
        for key, col in extras.items():
            notes.append(Note(f"extra:{key}", col, "medium", "kept as an extra column"))
    spec = {"kind": "orders", "format": _format_dict(profile), "orders": fields}
    return Draft(spec, notes, warnings,''')

# ======================= app/recon/matching.py =======================
G = "app/recon/matching.py"
op(G, '''    0.20  how many of the fee lines (commission, TCS, TDS, shipping, ...) agree with the rate
          card; half marks when the payment carries no checkable fee lines
''', '''    0.20  how many of the fee lines (commission, TCS, TDS, shipping, ...) agree with the rate
          card; half marks when the payment carries no checkable fee lines

On top of that, extra columns the partner's file carried (remarks, SKU, city) adjust the score:
a piece of the order reference in the remarks is strong evidence for that order, a matching SKU
or city adds a little, a different city or SKU counts against it. With no extras nothing changes.
''')
op(G, "from collections import defaultdict\nfrom dataclasses import dataclass",
   "import re\nfrom collections import defaultdict\nfrom dataclasses import dataclass, field")
op(G, "    SELECT g.id AS group_id, g.line_ids, o.id AS order_id, o.order_ref_raw, o.value_paise,",
   "    SELECT g.id AS group_id, g.line_ids, o.id AS order_id, o.order_ref_raw, o.order_ref,\n           o.extras AS order_extras, o.value_paise,")
op(G, "SELECT p.group_id, p.order_id, p.order_ref_raw, p.value_paise, p.days_off, p.due_on,",
   "SELECT p.group_id, p.order_id, p.order_ref_raw, p.order_ref, p.order_extras, p.value_paise,\n       p.days_off, p.due_on,")
op(G, '''# ---- 1. groups ---------------------------------------------------------------------------------''',
'''# ---- extra evidence from the partner's own columns (pure, unit-tested) -------------------------

_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9\\-_/]{4,}")
_STOP = {"SETTLEMENT", "PAYMENT", "REFUND", "ORDER", "PYMT", "REFERENCE", "TRANSFER"}


@dataclass
class Evidence:
    adjust: float = 0.0
    notes: list[str] = field(default_factory=list)
    detail: dict = field(default_factory=dict)


def _near(token: str, canon: str) -> bool:
    """token appears in canon, allowing one wrong character (a mistyped reference)."""
    n = len(token)
    if n < 6 or n > len(canon):
        return False
    return any(sum(a != b for a, b in zip(token, canon[i:i + n], strict=True)) <= 1
               for i in range(len(canon) - n + 1))


def extras_evidence(group: dict[str, str], order_ref: str,
                    order_extras: dict[str, str] | None) -> Evidence:
    """How much the partner's extra columns say about whether this payment is for this order."""
    ev = Evidence()
    canon = re.sub(r"[^A-Z0-9]", "", order_ref.upper())
    best = 0.0
    for value in group.values():
        for tok in _TOKEN.findall(str(value).upper()):
            tok = re.sub(r"[^A-Z0-9]", "", tok)
            if tok in _STOP or len(tok) < 5:
                continue
            if tok in canon:
                strength = 0.30 if len(tok) >= 0.6 * len(canon) else 0.22
                if strength > best:
                    best = strength
                    ev.detail["fragment"] = tok
                    ev.notes.append(f"The remarks contain '{tok}', part of this order's "
                                    "reference")
            elif _near(tok, canon) and best < 0.12:
                best = 0.12
                ev.detail["fragment"] = tok
                ev.notes.append(f"The remarks contain '{tok}', one character off a part of "
                                "this order's reference")
    ev.adjust += best
    theirs = {k.lower(): str(v).strip().lower() for k, v in (order_extras or {}).items()}
    for kind, plus, minus in (("sku", 0.10, -0.15), ("city", 0.08, -0.15)):
        mine = next((str(v).strip().lower() for k, v in group.items() if kind in k.lower()),
                    None)
        other = next((v for k, v in theirs.items() if kind in k), None)
        if not mine or not other:
            continue
        if mine == other:
            ev.adjust += plus
            ev.detail[kind] = "match"
            ev.notes.append(f"Same {kind.upper() if kind == 'sku' else kind}: {other}")
        else:
            ev.adjust += minus
            ev.detail[kind] = "differs"
            ev.notes.append(f"Different {kind.upper() if kind == 'sku' else kind}: the "
                            f"payment says {mine}, the order says {other}")
    return ev


# ---- 1. groups ---------------------------------------------------------------------------------''')
op(G, '''    per_group: dict[int, list[dict]] = defaultdict(list)
    for c in cands:
        if (c["group_id"], c["order_id"]) in rejected:
            continue
        c["score"] = score(c["days_off"], c["n_ok"], c["n_checked"])
        per_group[c["group_id"]].append(c)
    for rows in per_group.values():
        rows.sort(key=lambda c: (-c["score"], abs(c["days_off"]), c["order_id"]))''',
'''    group_extras = _group_extras(conn)
    per_group: dict[int, list[dict]] = defaultdict(list)
    for c in cands:
        if (c["group_id"], c["order_id"]) in rejected:
            continue
        c["base"] = score(c["days_off"], c["n_ok"], c["n_checked"])
        ev = extras_evidence(group_extras.get(c["group_id"], {}), c["order_ref"],
                             c["order_extras"])
        c["evidence"] = ev
        c["raw"] = round(c["base"] + ev.adjust, 3)   # can exceed 1; only the ranking uses it
        c["score"] = min(1.0, max(0.0, c["raw"]))
        per_group[c["group_id"]].append(c)
    for rows in per_group.values():
        rows.sort(key=lambda c: (-c["raw"], abs(c["days_off"]), c["order_id"]))''')
op(G, '''        runner = rows[1]["score"] if len(rows) > 1 else None''', '''        runner = rows[1]["raw"] if len(rows) > 1 else None''')
op(G, '''            conf = confidence(c["score"], runner if rank == 1 else None, contested) \\
                if rank == 1 else ("medium" if c["score"] >= 0.65 else "low")
            why = reasons_for(c["value_paise"], c["days_off"], c["n_ok"], c["n_checked"])''',
'''            conf = confidence(c["raw"], runner if rank == 1 else None, contested) \\
                if rank == 1 else ("medium" if c["score"] >= 0.65 else "low")
            why = reasons_for(c["value_paise"], c["days_off"], c["n_ok"], c["n_checked"])
            why = why[:-1] + c["evidence"].notes + why[-1:]''')
op(G, '''                        "fee_lines_ok": c["n_ok"], "fee_lines_checked": c["n_checked"],
                        "contested": contested})))''', '''                        "fee_lines_ok": c["n_ok"], "fee_lines_checked": c["n_checked"],
                        "contested": contested, "base_score": c["base"], "raw_score": c["raw"],
                        "evidence": c["evidence"].detail})))''')
op(G, '''def suggest(conn: Connection, top_n: int = 3)''', '''def _group_extras(conn: Connection) -> dict[int, dict[str, str]]:
    """For every open payment, the extra columns its lines carried (first value per key)."""
    out: dict[int, dict[str, str]] = {}
    rows = conn.execute(
        """SELECT g.id AS gid, l.extras FROM match_groups g
           JOIN ledger_lines l ON l.id = ANY(g.line_ids)
           WHERE g.status = 'open' AND l.extras IS NOT NULL""").fetchall()
    for r in rows:
        bucket = out.setdefault(r["gid"], {})
        for key, value in r["extras"].items():
            bucket.setdefault(key, str(value))
    return out


def suggest(conn: Connection, top_n: int = 3)''')
op(G, "               g.link_method, g.note\n        FROM match_groups g JOIN partners p ON p.id = g.partner_id",
   "               g.link_method, g.note, g.ai_verdict, g.ai_note\n        FROM match_groups g JOIN partners p ON p.id = g.partner_id")
op(G, "                   s.rank, s.score, s.confidence, s.reasons, s.status\n            FROM match_suggestions s JOIN orders o ON o.id = s.order_id",
   "                   s.rank, s.score, s.confidence, s.reasons, s.status, s.ai_pick, s.ai_confidence,\n                   s.ai_reason\n            FROM match_suggestions s JOIN orders o ON o.id = s.order_id")

# ======================= tests/test_ai_mapping.py =======================
op("tests/test_ai_mapping.py", '(["Remarks"] if junk else [])', '(["Internal Code"] if junk else [])')

# ======================= app/demo.py =======================
D = "app/demo.py"
op(D, "    python -m app.demo review                               # build the review queue\n",
   "    python -m app.demo review                               # build the review queue\n    python -m app.demo review --ai                          # ... and ask the model too\n")
op(D, '''    sub.add_parser("review", help="find payments with no order ID and suggest their orders")''',
   '''    review_p = sub.add_parser("review", help="find payments with no order ID and suggest "
                                             "their orders")
    review_p.add_argument("--ai", action="store_true",
                          help="also ask the model about the payments the rules are unsure of")
    review_p.add_argument("--all", action="store_true",
                          help="with --ai: ask about every payment, not only the unsure ones")
    review_p.add_argument("--limit", type=int, default=50, help="with --ai: how many payments")
    review_p.add_argument("--truth", type=Path, default=None,
                          help="with --ai: truth.csv of a generated data set, to score the picks")''')
op(D, '''            for grp in matching.queue(conn, limit=10):
                best = grp["suggestions"][0] if grp["suggestions"] else None''', '''            if args.ai:
                _review_with_ai(conn, args)
                return 0
            for grp in matching.queue(conn, limit=10):
                best = grp["suggestions"][0] if grp["suggestions"] else None''')
op(D, '''def main() -> int:''', '''def _review_with_ai(conn: Connection, args: argparse.Namespace) -> None:
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


def main() -> int:''')

# ======================= scripts/gen_data.py =======================
S = "scripts/gen_data.py"
op(S, '''M1_TYPES = {''', '''SKUS = ("BED-KING-GREY", "BED-KING-BLUE", "BED-QUEEN-WHITE", "BED-SINGLE-PINK", "TWL-BATH-WHT",
        "TWL-HAND-GRN", "PIL-COVER-SET2", "CUSH-COVER-SET4", "BLNK-DBL-BEIGE", "CURT-DOOR-TEAL",
        "QUILT-KING-RED", "MAT-YOGA-PRP")
CITY_BY_PREFIX = {
    "560": "Bengaluru", "561": "Bengaluru", "570": "Mysuru", "575": "Mangaluru",
    "577": "Shivamogga", "580": "Hubballi", "585": "Kalaburagi", "590": "Belagavi",
    "110": "Delhi", "400": "Mumbai", "600": "Chennai", "700": "Kolkata", "500": "Hyderabad",
    "411": "Pune", "380": "Ahmedabad", "302": "Jaipur", "226": "Lucknow", "282": "Agra",
    "462": "Bhopal", "751": "Bhubaneswar", "781": "Guwahati", "842": "Muzaffarpur",
    "834": "Ranchi", "160": "Chandigarh",
}

M1_TYPES = {''')
op(S, '''               "Weight (g)", "Length (cm)", "Width (cm)", "Height (cm)", "Courier", "AWB",
               "Delivery Status", "Delivered On", "Returned On"],
    "m1": ["Order ID", "Settlement Date", "Transaction Type", "Amount (INR)", "UTR"],
    "m2": ["order_id", "settled_on", "gross_sale", "commission", "fixed_fee", "shipping_fee",
           "tcs", "tds", "refund", "return_fee", "net_payout", "utr"],''', '''               "Weight (g)", "Length (cm)", "Width (cm)", "Height (cm)", "Courier", "AWB",
               "Delivery Status", "Delivered On", "Returned On", "SKU", "City"],
    "m1": ["Order ID", "Settlement Date", "Transaction Type", "Amount (INR)", "UTR", "SKU"],
    "m2": ["order_id", "settled_on", "gross_sale", "commission", "fixed_fee", "shipping_fee",
           "tcs", "tds", "refund", "return_fee", "net_payout", "utr", "city", "remarks"],''')
op(S, '''    "q1": ["Order No", "Payout Date", "Sale Value", "Platform Fee", "TCS", "TDS", "Net Payout"],''',
   '''    "q1": ["Order No", "Payout Date", "Sale Value", "Platform Fee", "TCS", "TDS", "Net Payout",
           "Store City"],''')
op(S, '''    "pg": ["Order ID", "Settled On", "Type", "Gross (INR)", "Fee (INR)", "Net (INR)"],''',
   '''    "pg": ["Order ID", "Settled On", "Type", "Gross (INR)", "Fee (INR)", "Net (INR)",
           "Narration"],''')
op(S, '''    due_on: date | None = None
    pending: bool = False

    @property
    def grams''', '''    due_on: date | None = None
    pending: bool = False
    sku: str = ""
    city: str = ""

    @property
    def grams''')
op(S, '''        self._lifecycle(plan)
        self._write_order(plan)''', '''        signals = random.Random(f"{self.seed}:{plan.ref}:signals")  # separate from the main stream
        plan.sku = signals.choice(SKUS)
        plan.city = CITY_BY_PREFIX[plan.pincode[:3]]
        self._lifecycle(plan)
        self._write_order(plan)''')
op(S, '''            p.delivered_on.isoformat() if p.delivered_on else "",
            p.returned_on.isoformat() if p.returned_on else "",
        ])''', '''            p.delivered_on.isoformat() if p.delivered_on else "",
            p.returned_on.isoformat() if p.returned_on else "", p.sku, p.city,
        ])''')
op(S, '''    def _write_settlement(self, p: Plan,''', '''    def _remark(self, p: Plan, blank_ref: bool) -> str:
        """Free text a partner puts next to a payment. When the order id column is blank the
        text often still holds a piece of the reference, sometimes mistyped; sometimes nothing
        useful. Deterministic per order, so a repeated row is a verbatim repeat."""
        r = random.Random(f"{self.seed}:{p.ref}:remark")
        canon = "".join(ch for ch in p.ref.upper() if ch.isalnum())
        if not blank_ref:
            return f"Settlement for order {p.ref}" if r.random() < 0.7 else ""
        roll = r.random()
        if roll < 0.45:  # the tail of the reference, 10% of the time with one character wrong
            tail = list(canon[-r.randint(5, 7):])
            if r.random() < 0.10:
                i = r.randrange(len(tail))
                tail[i] = "X" if tail[i] != "X" else "Y"
            return f"Pymt ref ...{''.join(tail)}"
        if roll < 0.65:  # the start of the reference
            return f"Order {p.ref[:max(6, len(p.ref) // 2)]}"
        return "Settlement" if roll < 0.85 else ""

    def _write_settlement(self, p: Plan,''')
op(S, '''                row = [cell, fdate(pay, "dmy"), M1_TYPES[t], a / 100, utr]''',
   '''                row = [cell, fdate(pay, "dmy"), M1_TYPES[t], a / 100, utr, p.sku]''')
op(S, '''                    sink.write([cell, fdate(pay, "dmy"), M1_TYPES[t], money(a), utr])''',
   '''                    sink.write([cell, fdate(pay, "dmy"), M1_TYPES[t], money(a), utr, p.sku])''')
op(S, '''                money(net), utr,
            ])''', '''                money(net), utr, p.city, self._remark(p, blank_ref),
            ])''')
op(S, '''                money(-by_type["tds"], "inr"), money(net, "inr"),
            ])''', '''                money(-by_type["tds"], "inr"), money(net, "inr"), p.city,
            ])''')
op(S, '''                self.sinks["pg"].write([cell, fdate(pay, "iso"), "settlement", money(p.value),
                                        money(fee), money(p.value - fee)])''',
   '''                self.sinks["pg"].write([cell, fdate(pay, "iso"), "settlement", money(p.value),
                                        money(fee), money(p.value - fee),
                                        self._remark(p, bool(missing_id))])''')
op(S, '''                self.sinks["pg"].write([p.ref, fdate(refund_on, "iso"), "refund",
                                        money(-p.value), money(-fee), money(-p.value + fee)])''',
   '''                self.sinks["pg"].write([p.ref, fdate(refund_on, "iso"), "refund",
                                        money(-p.value), money(-fee), money(-p.value + fee),
                                        f"Refund {p.ref}"])''')


root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
pending: dict[str, str] = {}
for path, old, new in OPS:
    target = root / path
    if not target.exists():
        print(f"CANNOT APPLY: {path} does not exist (run this from the backend folder)")
        sys.exit(1)
    text = pending.get(path) or target.read_text(encoding="utf-8")
    if text.count(old) != 1:
        print(f"CANNOT APPLY in {path}: expected to find this exactly once, found "
              f"{text.count(old)}:\n{old[:200]}")
        sys.exit(1)
    pending[path] = text.replace(old, new)
for path, text in pending.items():
    (root / path).write_text(text, encoding="utf-8")
print(f"patched {len(OPS)} edits in {len(pending)} files")