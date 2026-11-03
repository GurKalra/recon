"""Apply an approved mapping to a Table: validate every row, return clean rows plus rejects.

Nothing here touches the database. Every row either becomes clean typed data or is rejected
with a reason a human can act on; a bad row never fails the whole file.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import polars as pl

from app.ingest import normalize as nz
from app.ingest.mapping import LedgerFields, MappingSpec, OrderFields, ReceiptFields
from app.ingest.normalize import norm_header
from app.ingest.reader import Table

MAX_REJECTS_STORED = 1000
DELIVERY_STATUSES = ("placed", "shipped", "delivered", "returned", "cancelled")
SELLING_TYPES = ("website", "marketplace", "quick_commerce")
_NO_TEXT = pl.lit(None, dtype=pl.Utf8)


def _extras(extras: dict[str, str]) -> pl.Expr:
    """The optional extra columns of a row as one JSON object (text), or null. Empty cells are
    dropped by the database (jsonb_strip_nulls) when the row is stored."""
    if not extras:
        return _NO_TEXT
    fields = [nz.text(norm_header(col)).alias(key) for key, col in extras.items()]
    return pl.struct(fields).struct.json_encode()


@dataclass
class Transformed:
    good: pl.DataFrame
    rows_total: int
    rejects_total: int
    rejects: list[dict[str, object]]  # capped at MAX_REJECTS_STORED
    warnings: list[dict[str, object]] = field(default_factory=list)


def _first_failure(checks: list[tuple[pl.Expr, pl.Expr | str]]) -> pl.Expr:
    """A column holding the message of the FIRST failing check, or null when the row is fine."""
    reason = _NO_TEXT
    for condition, message in reversed(checks):
        then = message if isinstance(message, pl.Expr) else pl.lit(message)
        reason = pl.when(condition.fill_null(False)).then(then).otherwise(reason)
    return reason


def _finish(table: Table, derived: pl.DataFrame, checks: list[tuple[pl.Expr, pl.Expr | str]],
            warnings: list[dict[str, object]]) -> tuple[pl.DataFrame, Transformed]:
    """Split `derived` into good/bad rows using `checks`. Returns (good rows, partial result)."""
    flagged = derived.with_columns(_first_failure(checks).alias("_reject"))
    good = flagged.filter(pl.col("_reject").is_null()).drop("_reject")
    bad = flagged.filter(pl.col("_reject").is_not_null()).select("row_no", "_reject")
    shown = bad.head(MAX_REJECTS_STORED)
    raw_by_row = {
        r["row_no"]: {k: v for k, v in r.items() if k != "row_no"}
        for r in table.df.filter(pl.col("row_no").is_in(shown["row_no"].to_list())).to_dicts()
    }
    rejects = [
        {"row_no": row_no, "reason": reason, "raw": raw_by_row.get(row_no, {})}
        for row_no, reason in shown.iter_rows()
    ]
    if bad.height > MAX_REJECTS_STORED:
        warnings.append({"code": "rejects_capped", "stored": MAX_REJECTS_STORED,
                         "total": bad.height})
    result = Transformed(good=good, rows_total=table.df.height, rejects_total=bad.height,
                         rejects=rejects, warnings=warnings)
    return good, result


# ---- ledger (settlements, COD remittances, freight invoices) ---------------------------------


def _ledger(table: Table, f: LedgerFields) -> Transformed:
    c = norm_header
    cols: list[pl.Expr] = [pl.col("row_no")]
    checks: list[tuple[pl.Expr, pl.Expr | str]] = []

    ref_col = c(f.order_ref) if f.order_ref else None
    cols.append((nz.canonical_ref(ref_col) if ref_col else _NO_TEXT).alias("order_ref"))
    cols.append((nz.text(ref_col) if ref_col else _NO_TEXT).alias("order_ref_raw"))
    cols.append(_extras(f.extras).alias("extras"))
    if f.awb:
        awb, mangled = nz.awb(c(f.awb))
        cols += [awb.alias("awb"), mangled.alias("_awb_mangled")]
    else:
        cols += [_NO_TEXT.alias("awb"), pl.lit(False).alias("_awb_mangled")]
    cols.append((nz.text(c(f.reference)) if f.reference else _NO_TEXT).alias("reference"))

    occurred, _ = nz.date(c(f.date.column), f.date.format)
    cols.append(occurred.alias("occurred_on"))
    checks.append(
        (pl.col("occurred_on").is_null(), f"invalid or missing date in '{f.date.column}'"))

    for i, e in enumerate(f.emitters):
        amount, bad = nz.money(c(e.amount))
        cols += [amount.alias(f"_amt{i}"), bad.alias(f"_bad{i}")]
        checks.append((pl.col(f"_bad{i}"), f"invalid amount in '{e.amount}'"))
        if e.line_type is not None:
            line_type = pl.lit(e.line_type, dtype=pl.Utf8)
        else:
            key = pl.concat_str([nz.text(c(col)) for col in e.line_type_from], separator="|")
            line_type = key.replace_strict(e.line_type_map, default=None, return_dtype=pl.Utf8)
        cols.append(line_type.alias(f"_type{i}"))
        if e.line_type_from:
            checks.append((
                pl.col(f"_amt{i}").is_not_null() & pl.col(f"_type{i}").is_null(),
                f"unmapped line type in {list(e.line_type_from)}"))

    if f.net_check:
        net, net_bad = nz.money(c(f.net_check.column))
        cols += [net.alias("_net"), net_bad.alias("_net_bad")]
    derived = table.df.select(cols)

    if f.net_check:
        lines_sum = pl.sum_horizontal(
            [pl.col(f"_amt{i}").fill_null(0) * e.sign for i, e in enumerate(f.emitters)])
        expected = pl.col("_net") * f.net_check.sign
        checks.append((pl.col("_net_bad"), f"invalid amount in '{f.net_check.column}'"))
        off_by_more_than_tolerance = (lines_sum - expected).abs() > f.net_check.tolerance_paise
        checks.append((
            pl.col("_net").is_not_null() & off_by_more_than_tolerance,
            pl.format("lines add up to {} paise but '{}' says {}",
                      lines_sum, pl.lit(f.net_check.column), expected)))

    warnings: list[dict[str, object]] = []
    good, result = _finish(table, derived, checks, warnings)

    frames = []
    for i, e in enumerate(f.emitters):
        frame = good.select(
            pl.col("row_no"), pl.lit(i, dtype=pl.Int16).alias("emit_idx"),
            "order_ref", "order_ref_raw", "awb",
            pl.col(f"_type{i}").alias("line_type"),
            (pl.col(f"_amt{i}") * e.sign).alias("amount_paise"),
            "occurred_on", "reference", "extras",
        ).filter(pl.col("amount_paise").is_not_null() & pl.col("line_type").is_not_null())
        if e.skip_zero:
            frame = frame.filter(pl.col("amount_paise") != 0)
        frames.append(frame)
    result.good = pl.concat(frames).sort("row_no", "emit_idx")
    if (n := int(good["_awb_mangled"].sum())) and f.awb:
        result.warnings.append({"code": "awb_mangled_by_excel", "rows": n})
    if (n := int(good["order_ref"].is_null().sum())):
        result.warnings.append({"code": "rows_without_order_ref", "rows": n})
    return result


# ---- orders ----------------------------------------------------------------------------------


def _orders(table: Table, f: OrderFields, partner_types: dict[str, str]) -> Transformed:
    c = norm_header
    channels = [code for code, t in partner_types.items() if t in SELLING_TYPES]
    couriers = [code for code, t in partner_types.items() if t == "courier"]
    cols: list[pl.Expr] = [
        pl.col("row_no"),
        nz.canonical_ref(c(f.order_ref)).alias("order_ref"),
        nz.text(c(f.order_ref)).alias("order_ref_raw"),
        nz.text(c(f.channel)).str.to_uppercase().alias("channel_code"),
        nz.text(c(f.payment_mode)).str.to_lowercase().alias("payment_mode"),
        nz.text(c(f.delivery_status)).str.to_lowercase().alias("delivery_status"),
    ]
    cols.append((nz.text(c(f.courier)).str.to_uppercase() if f.courier else _NO_TEXT)
                .alias("courier_code"))
    if f.awb:
        awb, mangled = nz.awb(c(f.awb))
        cols += [awb.alias("awb"), mangled.alias("_awb_mangled")]
    else:
        cols += [_NO_TEXT.alias("awb"), pl.lit(False).alias("_awb_mangled")]
    value, value_bad = nz.money(c(f.value))
    cols += [value.alias("value_paise"), value_bad.alias("_value_bad")]
    for name, d in (("ordered_on", f.ordered_on), ("delivered_on", f.delivered_on),
                    ("returned_on", f.returned_on)):
        if d is None:
            cols += [pl.lit(None, dtype=pl.Date).alias(name), pl.lit(False).alias(f"_{name}_bad")]
        else:
            parsed, bad = nz.date(c(d.column), d.format)
            cols += [parsed.alias(name), bad.alias(f"_{name}_bad")]
    for name, column, lo, hi in (("weight_g", f.weight_g, 1, 1_000_000),
                                 ("length_cm", f.length_cm, 1, 32_767),
                                 ("width_cm", f.width_cm, 1, 32_767),
                                 ("height_cm", f.height_cm, 1, 32_767)):
        if column is None:
            cols += [pl.lit(None, dtype=pl.Int64).alias(name), pl.lit(False).alias(f"_{name}_bad")]
        else:
            parsed, bad = nz.whole_int(c(column), lo, hi)
            cols += [parsed.alias(name), bad.alias(f"_{name}_bad")]
    cols.append(_extras(f.extras).alias("extras"))
    pin = nz.text(c(f.pincode)) if f.pincode else _NO_TEXT
    cols += [pin.alias("pincode"),
             (pin.is_not_null() & ~pin.str.contains(r"^[0-9]{6}$").fill_null(False))
             .alias("_pincode_bad")]
    derived = table.df.select(cols)

    checks: list[tuple[pl.Expr, pl.Expr | str]] = [
        (pl.col("order_ref").is_null(), "missing order id"),
        (~pl.col("channel_code").is_in(channels), "unknown channel"),
        (pl.col("courier_code").is_not_null() & ~pl.col("courier_code").is_in(couriers),
         "unknown courier"),
        (pl.col("_ordered_on_bad") | pl.col("ordered_on").is_null(),
         "invalid or missing order date"),
        (pl.col("_delivered_on_bad"), "invalid delivered date"),
        (pl.col("_returned_on_bad"), "invalid returned date"),
        (~pl.col("payment_mode").is_in(["prepaid", "cod"]), "payment mode must be prepaid or cod"),
        (~pl.col("delivery_status").is_in(DELIVERY_STATUSES), "unknown delivery status"),
        (pl.col("_value_bad") | pl.col("value_paise").is_null(), "invalid or missing order value"),
        (pl.col("value_paise") < 0, "negative order value"),
        (pl.col("_weight_g_bad") | pl.col("_length_cm_bad") | pl.col("_width_cm_bad")
         | pl.col("_height_cm_bad"), "invalid weight or dimensions"),
        (pl.col("_pincode_bad"), "pincode must be 6 digits"),
        (pl.col("delivered_on") < pl.col("ordered_on"), "delivered before it was ordered"),
        (pl.col("returned_on") < pl.col("ordered_on"), "returned before it was ordered"),
    ]
    warnings: list[dict[str, object]] = []
    good, result = _finish(table, derived, checks, warnings)
    dupes = good.height - good.select("channel_code", "order_ref").unique().height
    if dupes:
        result.warnings.append({"code": "duplicate_orders_in_file", "rows": dupes,
                                "note": "the last occurrence wins"})
    if (n := int(good["_awb_mangled"].sum())) and f.awb:
        result.warnings.append({"code": "awb_mangled_by_excel", "rows": n})
    result.good = good.select(
        "row_no", "order_ref", "order_ref_raw", "channel_code", "courier_code", "awb",
        "ordered_on", "delivered_on", "returned_on", "payment_mode", "value_paise",
        "weight_g", "length_cm", "width_cm", "height_cm", "pincode", "delivery_status", "extras")
    return result


# ---- warehouse receipts ----------------------------------------------------------------------


def _receipts(table: Table, f: ReceiptFields) -> Transformed:
    c = norm_header
    received, bad = nz.date(c(f.received_on.column), f.received_on.format)
    derived = table.df.select(
        pl.col("row_no"),
        nz.canonical_ref(c(f.order_ref)).alias("order_ref"),
        received.alias("received_on"), bad.alias("_received_bad"),
        (nz.text(c(f.condition)) if f.condition else _NO_TEXT).alias("condition"),
    )
    checks: list[tuple[pl.Expr, pl.Expr | str]] = [
        (pl.col("order_ref").is_null(), "missing order id"),
        (pl.col("_received_bad") | pl.col("received_on").is_null(), "invalid or missing date"),
    ]
    good, result = _finish(table, derived, checks, [])
    result.good = good.select("row_no", "order_ref", "received_on", "condition")
    return result


def transform(table: Table, spec: MappingSpec, partner_types: dict[str, str]) -> Transformed:
    if spec.kind == "ledger":
        return _ledger(table, spec.ledger)  # type: ignore[arg-type]
    if spec.kind == "orders":
        return _orders(table, spec.orders, partner_types)  # type: ignore[arg-type]
    return _receipts(table, spec.receipts)  # type: ignore[arg-type]