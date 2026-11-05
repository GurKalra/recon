"""The evidence pack for a claim: a ZIP of CSVs a partner's finance team can check line by line.

    README.txt                         what is in the pack, how amounts were worked out, checksums
    1_summary.csv                      one row per order claimed: what we say is owed
    2_orders.csv                       the order facts (dates, value, courier, AWB)
    3_expected_vs_received.csv         for each order, line by line: expected under the rate card,
                                       received, and the gap
    4_payments_and_bills_received.csv  every payment / bill line we hold for these orders, with the
                                       file name and row number it came from
    5_rate_cards.csv                   the rate cards (and the version) used to work out "expected"

Money columns come in rupees and paise. The pack is deterministic: the same claim on the same
day gives the same bytes. Text cells that a spreadsheet could run as a formula are neutralised.
"""

from __future__ import annotations

import csv
import hashlib
import io
import zipfile
from datetime import date

from psycopg import Connection

from app.claims import ClaimError
from app.recon.engine import explain_orders

_FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")
_ZIP_TIME = (1980, 1, 1, 0, 0, 0)   # fixed, so the same pack always has the same bytes
_CHUNK = 2000


class _Amount(str):
    """A rupee amount we formatted ourselves ('-46.00'). It is a number, not text from a partner,
    so it is never given the formula-neutralising prefix."""


def safe(value: object) -> object:
    """A text cell that cannot be run as a formula when the CSV is opened in a spreadsheet."""
    if isinstance(value, str) and not isinstance(value, _Amount) \
            and value[:1] in _FORMULA_STARTS:
        return "'" + value
    return value


def inr(paise: int | None) -> str:
    if paise is None:
        return ""
    paise = int(paise)   # sums come back from Postgres as Decimal
    sign = "-" if paise < 0 else ""
    return _Amount(f"{sign}{abs(paise) // 100}.{abs(paise) % 100:02d}")


def _cell(value: object) -> object:
    return "" if value is None else value.isoformat() if isinstance(value, date) else value


def csv_bytes(header: list[str], rows: list[list[object]]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")
    writer.writerow(header)
    writer.writerows([[safe(_cell(c)) for c in row] for row in rows])
    return buf.getvalue().encode("utf-8-sig")


# ---- gathering ---------------------------------------------------------------------------------


def _items(conn: Connection, claim_id: int) -> list[dict]:
    return conn.execute(
        """
        SELECT ci.id AS item_id, f.id AS finding_id, f.type, ci.claimed_paise, ci.outcome,
               f.since_on, f.claim_deadline, o.id AS order_id, o.order_ref_raw, o.ordered_on,
               o.delivered_on, o.returned_on, o.value_paise, o.payment_mode, o.awb, o.pincode,
               ch.code AS channel, co.code AS courier, s.expected_paise, s.received_paise,
               s.variance_paise, s.status AS order_status,
               (SELECT min(w.received_on) FROM warehouse_receipts w
                WHERE w.order_id = o.id) AS return_received_on
        FROM claim_items ci JOIN findings f ON f.id = ci.finding_id
        JOIN orders o ON o.id = f.order_id
        JOIN partners ch ON ch.id = o.channel_partner_id
        LEFT JOIN partners co ON co.id = o.courier_partner_id
        LEFT JOIN order_status s ON s.order_id = o.id
        WHERE ci.claim_id = %s ORDER BY f.claim_deadline, o.order_ref_raw, ci.id
        """, (claim_id,)).fetchall()


def _lines(conn: Connection, order_ids: list[int]) -> list[dict]:
    return conn.execute(
        """
        SELECT o.order_ref_raw, p.code AS partner, l.line_type, l.amount_paise, l.occurred_on,
               l.reference, i.filename, l.source_row, l.dup_seq
        FROM ledger_lines l JOIN orders o ON o.id = l.order_id
        JOIN partners p ON p.id = l.partner_id JOIN imports i ON i.id = l.import_id
        WHERE l.order_id = ANY(%s)
        ORDER BY o.order_ref_raw, l.occurred_on, p.code, l.line_type, l.id
        """, (order_ids,)).fetchall()


def _rate_card_rows(conn: Connection, order_ids: list[int]) -> list[list[object]]:
    rows: list[list[object]] = []
    channel = conn.execute(
        """
        SELECT DISTINCT c.id, p.code, c.effective_from, c.commission_bps, c.fixed_paise,
               c.ship_extra_per_kg_paise, c.tcs_bps, c.tds_bps, c.pg_fee_bps, c.note
        FROM orders o
        JOIN LATERAL (SELECT c.* FROM channel_cards c
                      WHERE c.partner_id = o.channel_partner_id AND c.effective_from <= o.ordered_on
                      ORDER BY c.effective_from DESC LIMIT 1) c ON true
        JOIN partners p ON p.id = c.partner_id WHERE o.id = ANY(%s) ORDER BY p.code, c.id
        """, (order_ids,)).fetchall()
    for c in channel:
        base = ["channel", c["code"], c["id"], c["effective_from"]]
        for name in ("commission_bps", "fixed_paise", "ship_extra_per_kg_paise", "tcs_bps",
                     "tds_bps", "pg_fee_bps"):
            rows.append([*base, name, c[name]])
        rows.append([*base, "note", c["note"]])
        for s in conn.execute("SELECT max_g, fee_paise FROM channel_ship_slabs "
                              "WHERE card_id = %s ORDER BY max_g", (c["id"],)).fetchall():
            rows.append([*base, f"shipping_fee_up_to_{s['max_g']}g_paise", s["fee_paise"]])
    courier = conn.execute(
        """
        SELECT DISTINCT c.id, p.code, c.effective_from, c.cod_flat_paise, c.cod_bps, c.return_pct,
               c.note
        FROM orders o
        JOIN LATERAL (SELECT c.* FROM courier_cards c
                      WHERE c.partner_id = o.courier_partner_id AND c.effective_from <= o.ordered_on
                      ORDER BY c.effective_from DESC LIMIT 1) c ON true
        JOIN partners p ON p.id = c.partner_id WHERE o.id = ANY(%s) ORDER BY p.code, c.id
        """, (order_ids,)).fetchall()
    for c in courier:
        base = ["courier", c["code"], c["id"], c["effective_from"]]
        for name in ("cod_flat_paise", "cod_bps", "return_pct"):
            rows.append([*base, name, c[name]])
        rows.append([*base, "note", c["note"]])
        for z in conn.execute("SELECT zone, base_paise, addl_paise FROM courier_zone_rates "
                              "WHERE card_id = %s ORDER BY zone", (c["id"],)).fetchall():
            rows.append([*base, f"zone_{z['zone']}_first_500g_paise", z["base_paise"]])
            rows.append([*base, f"zone_{z['zone']}_each_further_500g_paise", z["addl_paise"]])
    zones = conn.execute("SELECT prefix, zone FROM zone_prefixes ORDER BY zone, prefix").fetchall()
    for z in zones:
        rows.append(["zones", "", "", "", f"pincode_prefix_{z['prefix']}", z["zone"]])
    return rows


# ---- the pack -----------------------------------------------------------------------------------

_TYPE_TEXT = {
    "unpaid_past_due": "No payment received although it was due",
    "short_paid": "Paid less than the agreed rate card",
    "freight_overcharged": "Billed more than the agreed courier rates",
    "return_not_received": "Returned parcel never reached our warehouse",
}


def evidence_zip(conn: Connection, claim_id: int, as_of: date) -> tuple[str, bytes]:
    claim = conn.execute(
        "SELECT c.*, p.code AS partner_code, p.name AS partner_name, p.tolerance_paise, "
        "p.claim_window_days FROM claims c JOIN partners p ON p.id = c.partner_id "
        "WHERE c.id = %s", (claim_id,)).fetchone()
    if claim is None:
        raise ClaimError("No such claim.")
    items = _items(conn, claim_id)
    if not items:
        raise ClaimError("The claim is empty.")
    order_ids = sorted({i["order_id"] for i in items})

    summary = csv_bytes(
        ["claim_id", "partner", "partner_reference", "order", "what_is_wrong", "claimed_inr",
         "claimed_paise", "expected_inr", "received_inr", "gap_inr", "owed_since",
         "claim_deadline", "days_left_on_" + as_of.isoformat(), "claim_status"],
        [[claim_id, claim["partner_code"], claim["partner_ref"], i["order_ref_raw"],
          _TYPE_TEXT.get(i["type"], i["type"]), inr(i["claimed_paise"]), i["claimed_paise"],
          inr(i["expected_paise"]), inr(i["received_paise"]), inr(i["variance_paise"]),
          i["since_on"], i["claim_deadline"], (i["claim_deadline"] - as_of).days, i["outcome"]]
         for i in items])

    orders = csv_bytes(
        ["order", "channel", "payment_mode", "order_value_inr", "ordered_on", "delivered_on",
         "returned_on", "return_received_at_warehouse_on", "courier", "awb", "pincode"],
        [[i["order_ref_raw"], i["channel"], i["payment_mode"], inr(i["value_paise"]),
          i["ordered_on"], i["delivered_on"], i["returned_on"], i["return_received_on"],
          i["courier"], i["awb"], i["pincode"]] for i in items])

    by_ref = {i["order_id"]: i["order_ref_raw"] for i in items}
    expl: list[dict] = []
    for start in range(0, len(order_ids), _CHUNK):
        expl += explain_orders(conn, order_ids[start:start + _CHUNK], as_of)
    expl.sort(key=lambda r: (by_ref[r["order_id"]], r["grp"], r["phase"], r["line_type"]))
    expected = csv_bytes(
        ["order", "settlement_group", "phase", "line_type", "due_on", "expected_inr",
         "expected_paise", "received_inr", "received_paise", "gap_inr", "lines_received"],
        [[by_ref[r["order_id"]], r["grp"], r["phase"], r["line_type"], r["due_on"],
          inr(r["expected_paise"]), int(r["expected_paise"]), inr(r["received_paise"]),
          int(r["received_paise"]), inr(int(r["received_paise"]) - int(r["expected_paise"])),
          int(r["n_received"])]
         for r in expl])

    received = csv_bytes(
        ["order", "from_partner", "line_type", "amount_inr", "amount_paise", "date", "reference",
         "source_file", "source_row", "repeat_of_an_earlier_line"],
        [[r["order_ref_raw"], r["partner"], r["line_type"], inr(r["amount_paise"]),
          r["amount_paise"], r["occurred_on"], r["reference"], r["filename"], r["source_row"],
          "yes" if r["dup_seq"] else "no"] for r in _lines(conn, order_ids)])

    cards = csv_bytes(["kind", "partner", "card_id", "effective_from", "parameter", "value"],
                 _rate_card_rows(conn, order_ids))

    files = {"1_summary.csv": summary, "2_orders.csv": orders,
             "3_expected_vs_received.csv": expected,
             "4_payments_and_bills_received.csv": received, "5_rate_cards.csv": cards}
    total = sum(i["claimed_paise"] for i in items)
    readme = "\r\n".join([
        f"Claim {claim_id} against {claim['partner_name']} ({claim['partner_code']})",
        f"Prepared as of {as_of.isoformat()}"
        + (f"; partner reference {claim['partner_ref']}" if claim["partner_ref"] else ""),
        f"{len(items)} orders, total claimed INR {inr(total)}",
        "",
        "How the amounts were worked out",
        "  For every order we compute what the agreed rate card says should arrive or be",
        "  billed (commission, fixed fee, shipping, taxes, freight, COD fee) using the card in",
        "  force on the order date, and compare it with what we actually hold. A difference",
        f"  counts only if it is more than INR {inr(claim['tolerance_paise'])} (the tolerance).",
        f"  The claim window for this partner is {claim['claim_window_days']} days.",
        "  Money is shown in rupees and in paise (1 rupee = 100 paise). A negative gap means we",
        "  received less than expected.",
        "",
        "Files",
        "  1_summary.csv                      one row per order claimed",
        "  2_orders.csv                       the order facts",
        "  3_expected_vs_received.csv         expected vs received, line by line",
        "  4_payments_and_bills_received.csv  every line we hold, with source file and row",
        "  5_rate_cards.csv                   the rate cards used",
        "",
        "Checksums (SHA-256)",
        *[f"  {hashlib.sha256(b).hexdigest()}  {name}" for name, b in sorted(files.items())],
        ""]).encode("utf-8")
    files["README.txt"] = readme

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in ["README.txt", *sorted(k for k in files if k != "README.txt")]:
            info = zipfile.ZipInfo(name, date_time=_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, files[name])
    return f"claim-{claim_id}-{claim['partner_code']}-{as_of.isoformat()}.zip", buf.getvalue()