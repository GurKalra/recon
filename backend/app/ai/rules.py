"""Rules-only mapping suggestions: read what columns look like and what they are called.

This is the baseline the AI is measured against, and the whole answer when no API key is
configured. It uses three kinds of evidence, strongest first:

  1. the arithmetic of the file (a partner's net column must equal the signed sum of the
     fee columns, which fixes signs and finds the net column without trusting any name)
  2. the values (dates in a known format, numbers, a few repeating labels such as a transaction
     type, order references that already exist in our orders)
  3. the column names (keyword tables)

It never needs to know the partner by name.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from itertools import combinations, product

import polars as pl

from app.ai.profile import ColumnProfile, FileProfile
from app.ingest import normalize as nz
from app.ingest.mapping import MappingSpec
from app.ingest.normalize import canonical
from app.ingest.transform import transform

DEDUCTIONS = {"commission", "fixed_fee", "shipping_fee", "return_fee", "pg_fee", "freight",
              "return_freight", "cod_fee", "tcs", "tds", "refund"}


@dataclass
class Hint:
    """What the uploader already knows. All optional."""

    source_kind: str | None = None        # settlement|cod_remittance|freight_invoice|orders|...
    partner_type: str | None = None       # website|marketplace|quick_commerce|courier
    known_refs: frozenset[str] | None = None  # canonical order refs already in our orders table


@dataclass
class Note:
    field: str
    value: str
    confidence: str  # high | medium | low
    reason: str


@dataclass
class Draft:
    spec: dict | None
    notes: list[Note] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unmapped: list[str] = field(default_factory=list)  # columns the spec does not use


# ---- keyword tables -----------------------------------------------------------------------------

_NOT_AN_AMOUNT = re.compile(r"weight|\bkg\b|\(g\)|\bzone\b|qty|quantity|pincode|pin code|slab")
_LINE_PATTERNS: list[tuple[str, str]] = [
    ("return_freight", r"return.*freight|rto|return charge|freight.*return"),
    ("return_fee", r"return.*(ship|fee|charge)"),
    ("cod_fee", r"cod.*(fee|charge)|cash on delivery.*(fee|charge)"),
    ("fixed_fee", r"fixed|closing"),
    ("tcs", r"\btcs\b"),
    ("tds", r"\btds\b"),
    ("refund", r"refund"),
    ("commission", r"commission|platform fee|marketplace fee|referral"),
    ("freight", r"freight|forward charge"),
    ("shipping_fee", r"shipping"),
    ("pg_fee", r"gateway|\bmdr\b|\bfee\b"),
    ("adjustment", r"adjust"),
    ("sale", r"gross|sale|order amount|item ?price|principal|cod (collected|amount)|collected|"
             r"order value|settlement"),
]
_VALUE_PATTERNS = [(t, p) for t, p in _LINE_PATTERNS if t != "adjustment"]
_REFERENCE = re.compile(r"utr|settlement[- ]?id|reference|ref no|invoice no|bill no|txn|"
                        r"transaction id|payout id")
_AWB = re.compile(r"awb|waybill|tracking")
_DATE_PREFERENCE = [r"settle", r"payout", r"remit", r"post", r"paid", r"invoice", r"bill",
                    r"^date$", r"date"]
_DATE_AVOID = re.compile(r"deliver|order|return|dispatch|ship")


def classify_header(name: str, partner_type: str | None = None) -> str | None:
    """The line type a column header names, or None."""
    for line_type, pattern in _LINE_PATTERNS:
        if re.search(pattern, name):
            if partner_type == "courier" and line_type == "shipping_fee":
                return "freight"
            if line_type == "pg_fee" and partner_type not in (None, "website"):
                return "commission"
            return line_type
    return None


def classify_value(parts: tuple[str, ...], partner_type: str | None = None) -> str | None:
    """The line type a transaction-type label (or pair of labels) names."""
    text = " ".join(parts).lower()
    squashed = text.replace(" ", "")
    for line_type, pattern in _VALUE_PATTERNS:
        if re.search(pattern, text) or re.search(pattern, squashed):
            if partner_type == "courier" and line_type == "shipping_fee":
                return "freight"
            return line_type
    return None


# ---- small helpers ------------------------------------------------------------------------------


def _date_format(col: ColumnProfile) -> str:
    return {"date_iso": "iso", "date_dmy": "dmy", "date_dmony": "dmony"}[col.kind]


def _first_match(profile: FileProfile, patterns: list[str], kinds: tuple[str, ...],
                 skip: set[str]) -> ColumnProfile | None:
    for pattern in patterns:
        for c in profile.columns:
            if c.kind in kinds and c.name not in skip and re.search(pattern, c.name):
                return c
    return None


def _money_paise(name: str) -> pl.Expr:
    return nz.money(name)[0]


# ---- ledger -------------------------------------------------------------------------------------


def _pick_order_ref(profile: FileProfile, hint: Hint, notes: list[Note],
                    skip: set[str]) -> str | None:
    best, best_score, why = None, 0.0, ""
    body = profile.table.df.head(500)
    for c in profile.columns:
        if c.kind not in ("text", "id") or c.name in skip:
            continue
        s, reasons = 0.0, []
        if re.search(r"order", c.name):
            s += 3
            reasons.append("name mentions 'order'")
        elif c.name == "ref":
            s += 2
            reasons.append("name is 'ref'")
        if _REFERENCE.search(c.name) and "order" not in c.name:
            s -= 5
        if _AWB.search(c.name):
            s -= 5
        if hint.known_refs:
            vals = body[c.name].drop_nulls()
            if vals.len():
                canon = body.select(
                    canonical(pl.col(c.name).str.strip_chars()))[c.name].drop_nulls()
                hit = sum(1 for v in canon.to_list() if v in hint.known_refs) / max(canon.len(), 1)
                if hit > 0:
                    s += 6 * hit
                    reasons.append(f"{hit:.0%} of its values are orders we already have")
        if s > best_score:
            best, best_score, why = c.name, s, "; ".join(reasons)
    if best is None or best_score < 2:
        return None
    notes.append(Note("order_ref", best, "high" if best_score >= 5 else "medium", why))
    return best


def _long_layout(profile: FileProfile, partner_type: str | None, claimed: set[str]
                 ) -> tuple[list[str], dict[str, str]] | None:
    """(label columns, label -> line type) when one amount column is split into line types by
    one or two label columns, else None. Uses the fewest label columns that never give one
    label two meanings (in 'Refund|Principal' and 'ItemPrice|Principal', 'Principal' alone
    would be ambiguous, so both columns are needed)."""
    enums = [c.name for c in profile.columns if c.kind == "enum" and c.name not in claimed
             and not re.search(r"zone|status|mode|channel|condition", c.name)]
    if not enums:
        return None
    df = profile.table.df.head(2000)
    full = df.select([nz.text(c) for c in enums]).unique().drop_nulls().rows()
    typed = [(combo, classify_value(tuple(combo), partner_type)) for combo in full]
    if not typed or sum(t is not None for _, t in typed) / len(typed) < 0.7:
        return None
    for size in (1, 2):
        for idx in combinations(range(len(enums)), size):
            groups: dict[str, set[str]] = {}
            for combo, line_type in typed:
                if line_type:
                    groups.setdefault("|".join(combo[i] for i in idx), set()).add(line_type)
            if groups and all(len(v) == 1 for v in groups.values()) \
                    and len({next(iter(v)) for v in groups.values()}) >= 2:
                return [enums[i] for i in idx], {k: next(iter(v)) for k, v in groups.items()}
    return None


def _signs_and_net(profile: FileProfile, emitters: list[dict], candidates: list[ColumnProfile]
                   ) -> tuple[list[dict], dict | None]:
    """Try the printed signs and their flips; return the emitters whose lines add up to one of
    the candidate columns, and that column as a net_check."""
    df = profile.table.df.head(2000)
    deduct_idx = [i for i, e in enumerate(emitters)
                  if e.get("line_type") in DEDUCTIONS and e["amount"]]
    tried = []
    for flips in product((False, True), repeat=min(len(deduct_idx), 1)):
        trial = [dict(e) for e in emitters]
        if flips and flips[0]:  # flip every deduction at once (they share one convention)
            for i in deduct_idx:
                trial[i]["sign"] = -trial[i]["sign"]
        tried.append(trial)
    for trial in tried:
        lines_sum = pl.sum_horizontal(
            [_money_paise(e["amount"]).fill_null(0) * e["sign"] for e in trial])
        for cand in candidates:
            net = _money_paise(cand.name)
            for sign in (1, -1):
                ok = df.select(
                    ((lines_sum - net * sign).abs() <= 100).fill_null(False).mean().alias("m"),
                    net.is_not_null().mean().alias("n"))
                if ok["n"][0] > 0.9 and ok["m"][0] >= 0.98:
                    return trial, {"column": cand.name, "sign": sign}
    return emitters, None


def _suggest_ledger(profile: FileProfile, hint: Hint) -> Draft:
    notes: list[Note] = []
    warnings: list[str] = []
    pt = hint.partner_type
    claimed: set[str] = set()

    order_ref = _pick_order_ref(profile, hint, notes, claimed)
    if order_ref:
        claimed.add(order_ref)
    awb = _first_match(profile, [r"awb|waybill|tracking"], ("text", "id", "number"), claimed)
    if awb:
        claimed.add(awb.name)
        notes.append(Note("awb", awb.name, "high", "name says AWB / waybill"))
    ref_col = next((c for c in profile.columns
                    if c.kind in ("text", "id") and c.name not in claimed
                    and _REFERENCE.search(c.name)), None)
    if ref_col:
        claimed.add(ref_col.name)
        notes.append(Note("reference", ref_col.name, "high", "name looks like a UTR / reference"))

    dates = [c for c in profile.columns if c.kind.startswith("date_")]
    good = [c for c in dates if not _DATE_AVOID.search(c.name)] or dates
    date_col = _first_match(profile, _DATE_PREFERENCE, tuple(c.kind for c in good), claimed)
    date_col = date_col if date_col in good else (good[0] if good else None)
    if date_col is None:
        return Draft(None, notes, ["no date column was found"])
    claimed.add(date_col.name)
    notes.append(Note("date", date_col.name, "high" if len(good) == 1 else "medium",
                      f"{_date_format(date_col)} dates; the payment/billing date among "
                      f"{len(dates)} date column(s)"))

    numbers = [c for c in profile.columns if c.kind == "number" and c.name not in claimed
               and not _NOT_AN_AMOUNT.search(c.name)]
    emitters: list[dict] = []
    long_part = _long_layout(profile, pt, claimed | {c.name for c in numbers})
    long_amount: ColumnProfile | None = None
    if long_part:
        label_cols, mapping = long_part
        # the column that carries the amounts: not a net/total column, preferably called
        # 'amount', else the one whose name says sale/gross, else any unclassified one
        netish = re.compile(r"\bnet\b|total|remitted")
        pool = [c for c in numbers if not netish.search(c.name)]
        by_amount = [c for c in pool if re.search(r"amount", c.name)
                     and classify_header(c.name, pt) in (None, "sale")]
        sale_like = [c for c in pool if classify_header(c.name, pt) == "sale"]
        unclassified = [c for c in pool if classify_header(c.name, pt) is None]
        long_amount = (by_amount or sale_like or unclassified or [None])[0]
        if long_amount is None:
            long_part = None
        else:
            claimed.update(label_cols)
            claimed.add(long_amount.name)
            emitters.append({"amount": long_amount.name, "sign": 1, "skip_zero": False,
                             "line_type_from": label_cols, "line_type_map": mapping})
            notes.append(Note("layout", "long", "high",
                              f"one amount column split by {label_cols}"))
    leftovers: list[ColumnProfile] = []
    for c in numbers:
        if c is long_amount:
            continue
        line_type = classify_header(c.name, pt)
        if line_type is None:
            leftovers.append(c)
            continue
        printed_negative = c.negative_share > 0.5
        sign = 1 if (line_type not in DEDUCTIONS or printed_negative) else -1
        emitters.append({"amount": c.name, "sign": sign, "line_type": line_type,
                         "skip_zero": line_type != "sale" and (c.zero_share > 0 or c.filled < 1)})
        notes.append(Note(f"emit:{c.name}", line_type, "medium",
                          f"name suggests {line_type}; printed "
                          f"{'negative' if printed_negative else 'positive'}"))
    if not emitters:
        return Draft(None, notes, ["no amount columns were recognised"],
                     [c.name for c in leftovers])

    net_check = None
    if leftovers:
        emitters, net_check = _signs_and_net(profile, emitters, leftovers)
        if net_check:
            claimed.add(net_check["column"])
            leftovers = [c for c in leftovers if c.name != net_check["column"]]
            for e in emitters:
                for n in notes:
                    if n.field == f"emit:{e['amount']}":
                        n.confidence, n.reason = "high", n.reason + "; sign confirmed by net"
            notes.append(Note("net_check", net_check["column"], "high",
                              "the signed fee columns add up to this column on "
                              ">=98% of rows"))
        elif not long_part:
            warnings.append("no column matched the sum of the fee columns, so signs could "
                            "not be confirmed")
    spec = {
        "kind": "ledger", "format": _format_dict(profile),
        "ledger": {"order_ref": order_ref, "awb": awb.name if awb else None,
                   "reference": ref_col.name if ref_col else None,
                   "date": {"column": date_col.name, "format": _date_format(date_col)},
                   "emitters": emitters, "net_check": net_check},
    }
    spec["ledger"] = {k: v for k, v in spec["ledger"].items() if v is not None}
    extras = _extras_for(profile, _used(spec))
    if extras:
        spec["ledger"]["extras"] = extras
        for key, col in extras.items():
            notes.append(Note(f"extra:{key}", col, "medium", "kept as an extra column"))
    unmapped = [c.name for c in profile.columns if c.name not in _used(spec)]
    if not order_ref:
        warnings.append("no order reference column was found: every row will go to the "
                        "review queue")
    return Draft(spec, notes, warnings, unmapped)


_EXTRA_NAME = re.compile(r"sku|item|product|title|city|state|region|store|remark|narration|"
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


def _used(spec: dict) -> set[str]:
    return set(MappingSpec.model_validate(spec).referenced_columns())


def _format_dict(profile: FileProfile) -> dict:
    f = profile.format
    out = {"kind": f.kind, "header_row": profile.header_row}
    if f.footer_markers:
        out["footer_markers"] = list(f.footer_markers)
    if f.kind == "xlsx":
        out["sheet"] = f.sheet
    return out


# ---- orders and receipts ------------------------------------------------------------------------

_ORDER_ALIASES: dict[str, list[str]] = {
    "order_ref": [r"order (id|no|number|ref)", r"^order$", r"^ref"],
    "channel": [r"channel", r"marketplace", r"platform", r"source"],
    "payment_mode": [r"payment (mode|method|type)", r"^mode$", r"\bcod\b"],
    "value": [r"order (value|amount|total)", r"^value", r"total amount", r"invoice value"],
    "pincode": [r"pin ?code", r"postal", r"zip"],
    "weight_g": [r"weight"],
    "length_cm": [r"length"], "width_cm": [r"width"], "height_cm": [r"height"],
    "courier": [r"courier", r"carrier", r"logistics", r"shipping partner"],
    "awb": [r"awb|waybill|tracking"],
    "delivery_status": [r"delivery status", r"order status", r"^status$"],
}
_ORDER_DATES = {
    "ordered_on": [r"order(ed)? (date|on)", r"created", r"^date$"],
    "delivered_on": [r"deliver\w* (on|date)"],
    "returned_on": [r"return\w* (on|date)"],
}


def _alias_pick(profile: FileProfile, patterns: list[str], kinds: tuple[str, ...] | None,
                used: set[str]) -> ColumnProfile | None:
    for pattern in patterns:
        for c in profile.columns:
            if c.name in used or (kinds and c.kind not in kinds):
                continue
            if re.search(pattern, c.name):
                return c
    return None


def _suggest_orders(profile: FileProfile, hint: Hint) -> Draft:
    used: set[str] = set()
    fields: dict[str, object] = {}
    notes: list[Note] = []
    for fld, patterns in _ORDER_DATES.items():
        col = _alias_pick(profile, patterns, ("date_iso", "date_dmy", "date_dmony"), used)
        if col:
            used.add(col.name)
            fields[fld] = {"column": col.name, "format": _date_format(col)}
            notes.append(Note(fld, col.name, "high", "name and date format match"))
    for fld, patterns in _ORDER_ALIASES.items():
        col = _alias_pick(profile, patterns, None, used)
        if col:
            used.add(col.name)
            fields[fld] = col.name
            notes.append(Note(fld, col.name, "high" if fld != "payment_mode" else "medium",
                              "name matches"))
    required = ["order_ref", "channel", "ordered_on", "payment_mode", "value", "delivery_status"]
    missing = [r for r in required if r not in fields]
    warnings = []
    if "weight_g" in fields and re.search(r"\bkg\b", str(fields["weight_g"])):
        warnings.append("weights look like kilograms; the importer expects grams")
    if missing:
        return Draft(None, notes, [f"could not find: {', '.join(missing)}"],
                     [c.name for c in profile.columns if c.name not in used])
    extras = _extras_for(profile, used)
    if extras:
        fields["extras"] = extras
        for key, col in extras.items():
            notes.append(Note(f"extra:{key}", col, "medium", "kept as an extra column"))
    spec = {"kind": "orders", "format": _format_dict(profile), "orders": fields}
    return Draft(spec, notes, warnings,
                 [c.name for c in profile.columns if c.name not in _used(spec)])


def _suggest_receipts(profile: FileProfile, hint: Hint) -> Draft:
    used: set[str] = set()
    notes: list[Note] = []
    ref = _alias_pick(profile, [r"order (id|no|ref)", r"return order", r"^ref"], None, used)
    if ref:
        used.add(ref.name)
    got = _alias_pick(profile, [r"receiv\w* (on|date)", r"scan", r"^date"],
                      ("date_iso", "date_dmy", "date_dmony"), used)
    if not ref or not got:
        return Draft(None, notes, ["could not find the order reference and received date"])
    used.add(got.name)
    cond = _alias_pick(profile, [r"condition", r"grade", r"^status$"], None, used)
    fields: dict[str, object] = {"order_ref": ref.name,
                                 "received_on": {"column": got.name, "format": _date_format(got)}}
    notes += [Note("order_ref", ref.name, "high", "name matches"),
              Note("received_on", got.name, "high", "name and date format match")]
    if cond:
        fields["condition"] = cond.name
    spec = {"kind": "receipts", "format": _format_dict(profile), "receipts": fields}
    return Draft(spec, notes, [], [c.name for c in profile.columns if c.name not in _used(spec)])


# ---- entry point --------------------------------------------------------------------------------


def infer_kind(profile: FileProfile, hint: Hint) -> str:
    sk = hint.source_kind
    if sk == "orders":
        return "orders"
    if sk == "warehouse_receipt":
        return "receipts"
    if sk in ("settlement", "cod_remittance", "freight_invoice"):
        return "ledger"
    names = " ".join(profile.names())
    if re.search(r"delivery status|pincode|pin code", names):
        return "orders"
    if re.search(r"received on|condition|scanned", names) and not re.search(r"amount|fee", names):
        return "receipts"
    return "ledger"


def suggest_with_rules(profile: FileProfile, hint: Hint | None = None) -> Draft:
    hint = hint or Hint()
    kind = infer_kind(profile, hint)
    return {"orders": _suggest_orders, "receipts": _suggest_receipts,
            "ledger": _suggest_ledger}[kind](profile, hint)


@dataclass
class Check:
    ok: bool
    errors: list[str]
    rows: int = 0
    rejects: int = 0
    reject_reasons: list[str] = field(default_factory=list)

    @property
    def reject_rate(self) -> float:
        return self.rejects / self.rows if self.rows else 1.0


def verify(profile: FileProfile, spec_dict: dict | None, sample_rows: int = 2000,
           partner_types: dict[str, str] | None = None) -> Check:
    """Does this mapping actually work on this file? Validates the spec, checks every column it
    names exists, and runs the real transform on the first rows."""
    if spec_dict is None:
        return Check(False, ["no mapping was produced"])
    try:
        spec = MappingSpec.model_validate(spec_dict)
    except Exception as exc:
        return Check(False, [f"the mapping is not valid: {exc}"])
    missing = [c for c in spec.referenced_columns() if c not in set(profile.names())]
    if missing:
        return Check(False, [f"columns not in the file: {missing}. The file has "
                             f"{profile.names()}"])
    table = dataclasses.replace(profile.table, df=profile.table.df.head(sample_rows))
    try:
        result = transform(table, spec, partner_types or {})
    except Exception as exc:  # a structurally wrong mapping can fail inside the transform
        return Check(False, [f"the mapping failed on the data: {exc}"])
    reasons = list(dict.fromkeys(r["reason"] for r in result.rejects))[:3]
    rate = result.rejects_total / max(result.rows_total, 1)
    errors = [f"{result.rejects_total} of {result.rows_total} rows would be rejected, e.g. "
              f"{reasons}"] if rate > 0.02 else []
    return Check(not errors, errors, result.rows_total, result.rejects_total, reasons)