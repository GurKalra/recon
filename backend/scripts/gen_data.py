#!/usr/bin/env python
"""Deterministic synthetic data for the reconciliation app, with planted and LABELLED leaks.

    python scripts/gen_data.py --orders 3000 --out ../sample_data/small
    python scripts/gen_data.py --orders 100000 --out ../sample_data/xl     # stress test

Writes 12 partner-style input files (different headers, date formats, money formats, wide and
long layouts, one .xlsx with title rows and a footer total, an Excel-mangled AWB column, a UTF-8
BOM...) plus:

    truth.csv        ground truth: the status every order SHOULD end up with, and why
    rate_cards.json  the rate cards the generator used (seed the database from this)
    manifest.json    row counts, leak counts, seed

The generator and the future engine share the same business rules, so truth.csv proves the engine
implements the rules correctly; it does not prove the rules match any real partner contract.
Money is integer paise throughout; files print rupees.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from pathlib import Path

try:
    import xlsxwriter
except ImportError:  # only needed for the .xlsx settlement file
    xlsxwriter = None

GENERATOR_VERSION = 1
TOL = 1000  # Rs 10 in paise. A gap strictly above this is a finding.
RECEIPT_WAIT_DAYS = 7  # a return must be scanned in within this many days of coming back
FREIGHT_BILL_LAG_DAYS = 3
STATUS_PRIORITY = ("return_not_received", "unpaid_past_due", "short_paid", "freight_overcharged")

# --------------------------------------------------------------------------------------------
# Business rules (assumptions are labelled in the TDD)
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Partner:
    code: str
    name: str
    type: str
    payout_sla_days: int
    claim_window_days: int


PARTNERS: dict[str, Partner] = {
    p.code: p
    for p in (
        Partner("WEB", "Brand Website (payment gateway)", "website", 2, 30),
        Partner("M1", "Marketplace One", "marketplace", 7, 90),
        Partner("M2", "Marketplace Two", "marketplace", 10, 45),
        Partner("M3", "Marketplace Three", "marketplace", 14, 60),
        Partner("Q1", "QuickMart", "quick_commerce", 7, 30),
        Partner("Q2", "Dash Express", "quick_commerce", 7, 30),
        Partner("CA", "Courier Aggregator A", "courier", 7, 15),
        Partner("CB", "Courier Aggregator B", "courier", 10, 15),
    )
}


@dataclass(frozen=True)
class ChannelCard:
    commission_bps: int
    fixed_paise: int
    ship_slabs: tuple[tuple[int, int], ...]  # (max chargeable grams, fee in paise)
    ship_extra_per_kg_paise: int  # per started 1000 g above the last slab
    tcs_bps: int = 50  # 0.5% GST TCS on the order value (assumption: value is the base)
    tds_bps: int = 10  # 0.1% income-tax TDS (194-O) on gross value


CHANNEL_CARDS: dict[str, ChannelCard] = {
    "M1": ChannelCard(1200, 2000, ((500, 4500), (1000, 7000), (2000, 11000)), 4000),
    "M2": ChannelCard(1000, 1500, ((500, 4000), (1000, 6500), (2000, 10000)), 3500),
    "M3": ChannelCard(1400, 2500, ((500, 5000), (1000, 7500), (2000, 12000)), 4500),
    "Q1": ChannelCard(1800, 0, (), 0),
    "Q2": ChannelCard(2000, 0, (), 0),
}
PG_FEE_BPS = 200  # website payment gateway fee, reversed on refund


@dataclass(frozen=True)
class CourierCard:
    base: dict[str, int]  # first 500 g, per zone
    addl: dict[str, int]  # each further started 500 g, per zone
    cod_flat: int
    cod_bps: int
    return_pct: int  # return freight as a percentage of forward freight


COURIER_CARDS: dict[str, CourierCard] = {
    "CA": CourierCard(
        {"A": 4000, "B": 4800, "C": 5600, "D": 7000},
        {"A": 3000, "B": 3800, "C": 4600, "D": 6000},
        3000, 200, 100,
    ),
    "CB": CourierCard(
        {"A": 3600, "B": 4500, "C": 5400, "D": 6800},
        {"A": 2800, "B": 3500, "C": 4300, "D": 5600},
        2800, 150, 80,
    ),
}

ZONE_RULES = {  # first matching rule wins; documented here and in rate_cards.json
    "A": {"pincode_prefixes": ["560"]},
    "B": {"pincode_prefixes": ["56", "57", "58", "59"]},
    "C": {"pincode_prefixes": ["11", "40", "41", "60", "70", "50", "38", "30"]},
    "D": {"pincode_prefixes": []},  # everything else
}


def pct(amount: int, bps: int) -> int:
    """amount * bps / 10000, rounded half up, in integer arithmetic."""
    return (amount * bps + 5000) // 10000


def volumetric_g(length_cm: int, width_cm: int, height_cm: int) -> int:
    return length_cm * width_cm * height_cm // 5  # L*W*H / 5000 kg, in grams


def chargeable_g(weight_g: int, length_cm: int, width_cm: int, height_cm: int) -> int:
    return max(weight_g, volumetric_g(length_cm, width_cm, height_cm))


def slabs_500(grams: int) -> int:
    return max(1, -(-grams // 500))


def zone_for(pincode: str) -> str:
    for zone in ("A", "B", "C"):
        if pincode.startswith(tuple(ZONE_RULES[zone]["pincode_prefixes"])):
            return zone
    return "D"


def courier_freight(card: CourierCard, zone: str, grams: int) -> int:
    return card.base[zone] + (slabs_500(grams) - 1) * card.addl[zone]


def courier_cod_fee(card: CourierCard, value: int) -> int:
    return max(card.cod_flat, pct(value, card.cod_bps))


def courier_return_freight(card: CourierCard, forward: int) -> int:
    return pct(forward, card.return_pct * 100)


def channel_ship_fee(card: ChannelCard, grams: int) -> int:
    if not card.ship_slabs:
        return 0
    for max_g, fee in card.ship_slabs:
        if grams <= max_g:
            return fee
    last_max, last_fee = card.ship_slabs[-1]
    return last_fee + -(-(grams - last_max) // 1000) * card.ship_extra_per_kg_paise


# --------------------------------------------------------------------------------------------
# Output formatting (every partner formats money, dates and ids differently on purpose)
# --------------------------------------------------------------------------------------------

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _indian(n: int) -> str:
    s = str(n)
    if len(s) <= 3:
        return s
    head, tail, groups = s[:-3], s[-3:], []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join([*groups, tail])


def money(paise: int, style: str = "plain") -> str:
    sign, rupees, cents = ("-" if paise < 0 else ""), abs(paise) // 100, abs(paise) % 100
    if style == "plain":
        return f"{sign}{rupees}.{cents:02d}"
    if style == "inr":  # "₹1,499.00"
        return f"{sign}₹{_indian(rupees)}.{cents:02d}"
    if style == "paren":  # accounting style: "(46.00)" for negatives, western grouping
        body = f"{rupees:,}.{cents:02d}"
        return f"({body})" if paise < 0 else body
    raise ValueError(style)


def fdate(d: date, style: str) -> str:
    if style == "iso":
        return d.isoformat()
    if style == "dmy":
        return f"{d.day:02d}/{d.month:02d}/{d.year}"
    if style == "dmony":
        return f"{d.day:02d}-{MONTHS[d.month - 1]}-{d.year}"
    if style == "iso_ts":
        return f"{d.isoformat()}T10:15:00+05:30"
    raise ValueError(style)


def excelify(digits: str) -> str:
    """What Excel does to a long numeric id: 1.23457E+14 (digits lost for good)."""
    return f"{float(digits):.5E}"


# --------------------------------------------------------------------------------------------
# Sinks
# --------------------------------------------------------------------------------------------


class CsvSink:
    def __init__(self, path: Path, header: list[str], bom: bool = False):
        self.rows = 0
        self._f = path.open("w", newline="", encoding="utf-8-sig" if bom else "utf-8")
        self._w = csv.writer(self._f)
        self._w.writerow(header)

    def write(self, row: list[object]) -> None:
        self._w.writerow(row)
        self.rows += 1

    def close(self) -> None:
        self._f.close()


class XlsxSink:
    """Looks like a real marketplace export: title rows, a blank row, headers, data, a total."""

    def __init__(self, path: Path, titles: list[str], header: list[str]):
        self.rows = 0
        self.total_paise = 0
        self._wb = xlsxwriter.Workbook(str(path), {"constant_memory": True})
        self._ws = self._wb.add_worksheet("Settlement")
        for i, title in enumerate(titles):
            self._ws.write(i, 0, title)
        self.header_row = len(titles) + 1  # 0-based; one blank row after the titles
        for c, name in enumerate(header):
            self._ws.write(self.header_row, c, name)
        self._next = self.header_row + 1

    def write_amount_row(self, cells: list[object], amount_paise: int, amount_col: int) -> None:
        for c, value in enumerate(cells):
            if c == amount_col:
                self._ws.write_number(self._next, c, amount_paise / 100)
            else:
                self._ws.write(self._next, c, value)
        self._next += 1
        self.rows += 1
        self.total_paise += amount_paise

    def close(self, amount_col: int) -> None:
        self._ws.write(self._next, 0, "Total")
        self._ws.write_number(self._next, amount_col, self.total_paise / 100)
        self._wb.close()


# --------------------------------------------------------------------------------------------
# The generator
# --------------------------------------------------------------------------------------------

CHANNELS = ("WEB", "M1", "M2", "M3", "Q1", "Q2")
CHANNEL_WEIGHTS = (0.40, 0.20, 0.12, 0.08, 0.12, 0.08)
WEIGHT_CHOICES = ((400, 0.25), (800, 0.30), (1200, 0.20), (2000, 0.15), (3500, 0.10))
ZONE_PINCODE_PREFIXES = {
    "A": ["560"],
    "B": ["561", "570", "575", "577", "580", "585", "590"],
    "C": ["110", "400", "600", "700", "500", "411", "380", "302"],
    "D": ["226", "282", "462", "751", "781", "842", "834", "160"],
}
ZONE_WEIGHTS = (("A", 0.10), ("B", 0.20), ("C", 0.40), ("D", 0.30))
TRANSIT_DAYS = {"A": (1, 2), "B": (2, 4), "C": (3, 6), "D": (4, 8)}
RETURN_RATE = {"M": 0.09, "Q": 0.04, "WEB_PREPAID": 0.06}
COD_RTO_RATE = 0.12

M1_TYPES = {
    "sale": "Item Price", "refund": "Refund", "commission": "Commission",
    "fixed_fee": "Fixed Closing Fee", "shipping_fee": "Shipping Fee",
    "return_fee": "Return Shipping Fee", "tcs": "TCS", "tds": "TDS",
}
M3_TYPES = {
    "sale": ("ItemPrice", "Principal"), "refund": ("Refund", "Principal"),
    "commission": ("ItemFees", "Commission"), "fixed_fee": ("ItemFees", "FixedClosingFee"),
    "shipping_fee": ("ItemFees", "ShippingChargeback"),
    "return_fee": ("ItemFees", "ReturnShippingFee"),
    "tcs": ("ItemWithheldTax", "TCS"), "tds": ("ItemWithheldTax", "TDS"),
}
CB_ZONE_LABEL = {"A": "Z1", "B": "Z2", "C": "Z3", "D": "Z4"}

HEADERS = {
    "orders": ["Order ID", "Channel", "Order Date", "Payment Mode", "Order Value", "Pincode",
               "Weight (g)", "Length (cm)", "Width (cm)", "Height (cm)", "Courier", "AWB",
               "Delivery Status", "Delivered On", "Returned On"],
    "m1": ["Order ID", "Settlement Date", "Transaction Type", "Amount (INR)", "UTR"],
    "m2": ["order_id", "settled_on", "gross_sale", "commission", "fixed_fee", "shipping_fee",
           "tcs", "tds", "refund", "return_fee", "net_payout", "utr"],
    "m3": ["settlement-id", "posted-date", "order-id", "amount-type", "amount-description",
           "amount"],
    "q1": ["Order No", "Payout Date", "Sale Value", "Platform Fee", "TCS", "TDS", "Net Payout"],
    "q2": ["Ref", "Date", "Order Amount", "Commission", "TCS", "TDS", "Settled Amount"],
    "pg": ["Order ID", "Settled On", "Type", "Gross (INR)", "Fee (INR)", "Net (INR)"],
    "cod_ca": ["AWB", "Order Ref", "Delivered On", "Remitted On", "COD Collected",
               "Freight Charged", "COD Fee", "Billed Weight (g)", "Net Remitted", "UTR"],
    "cod_cb": ["Waybill", "Client Order No", "Delivery Date", "Payout Date", "Cod Amount",
               "Shipping", "Cod Charges", "Charged Weight (kg)", "Remitted", "Ref No"],
    "freight_ca": ["Invoice No", "Invoice Date", "AWB", "Order Ref", "Zone", "Billed Weight (g)",
                   "Freight", "Return Freight", "Total"],
    "freight_cb": ["Bill No", "Bill Date", "Waybill", "Client Order No", "Dest Zone",
                   "Charged Weight (kg)", "Forward Charge", "RTO/Return Charge", "Amount"],
    "receipts": ["Return Order ID", "Received On", "Condition", "Scanned By"],
    "truth": ["order_ref", "channel", "state", "expected_status", "findings", "flags", "leak",
              "delta_paise", "due_on", "payment_mode", "value_paise"],
}
FILES = {
    "orders": "orders.csv", "m1": "settlement_m1.xlsx", "m2": "settlement_m2.csv",
    "m3": "settlement_m3.csv", "q1": "settlement_q1.csv", "q2": "settlement_q2.csv",
    "pg": "settlement_web_pg.csv", "cod_ca": "cod_remittance_ca.csv",
    "cod_cb": "cod_remittance_cb.csv", "freight_ca": "freight_invoice_ca.csv",
    "freight_cb": "freight_invoice_cb.csv", "receipts": "warehouse_receipts.csv",
    "truth": "truth.csv",
}


@dataclass
class Plan:
    ref: str
    channel: str
    ordered_on: date
    mode: str
    value: int
    pincode: str
    zone: str
    weight_g: int
    dims: tuple[int, int, int]
    courier: str | None
    awb: str | None
    state: str = "in_transit"  # in_transit | delivered | returned | rto
    status: str = "shipped"
    delivered_on: date | None = None
    returned_on: date | None = None
    findings: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    leak: str = "none"
    delta: int = 0  # money effect on the brand vs expected (negative = we lost money)
    due_on: date | None = None
    pending: bool = False

    @property
    def grams(self) -> int:
        return chargeable_g(self.weight_g, *self.dims)


def pick(u: float, table: tuple[tuple[str, float], ...]) -> str:
    acc = 0.0
    for name, probability in table:
        acc += probability
        if u < acc:
            return name
    return "none"


class Generator:
    def __init__(self, out: Path, n_orders: int, seed: int, today: date, use_xlsx: bool):
        self.rng = random.Random(seed)
        self.out, self.n_orders, self.seed, self.today = out, n_orders, seed, today
        self.use_xlsx = use_xlsx
        self.window = 104
        self.start = today - timedelta(days=self.window + 1)
        self.counters: Counter[str] = Counter()
        self.stats: Counter[str] = Counter()
        self.sinks: dict[str, CsvSink | XlsxSink] = {}
        out.mkdir(parents=True, exist_ok=True)
        for key, header in HEADERS.items():
            if key == "m1" and use_xlsx:
                self.sinks[key] = XlsxSink(
                    out / FILES["m1"],
                    ["Marketplace One: Seller Settlement Report",
                     "Seller: Demo Home Textiles Pvt Ltd",
                     f"Generated on {fdate(today, 'dmony')}"],
                    header,
                )
            else:
                name = "settlement_m1.csv" if key == "m1" else FILES[key]
                self.sinks[key] = CsvSink(out / name, header, bom=key in ("q1", "cod_cb"))

    # ---- helpers -------------------------------------------------------------------------

    def _dates(self, base: date, sla: int, max_early: int = 2) -> tuple[date, date]:
        """(due_on, pay_on). Partners pay on or up to `max_early` days before the SLA date."""
        due = base + timedelta(days=sla)
        return due, due - timedelta(days=self.rng.randint(0, min(max_early, sla)))

    def _make_ref(self, channel: str, n: int) -> str:
        if channel == "WEB":
            return f"ORD-W-{200000 + n}"
        if channel == "M1":
            return f"ORD-M-{80000 + n}"
        if channel == "M2":
            return f"OD{4100000000000 + n * 13}"
        if channel == "M3":
            return f"40{n % 10}-{1000000 + n:07d}-{self.rng.randint(0, 9999999):07d}"
        if channel == "Q1":
            return f"QC1-{50000 + n}"
        return f"Q2{700000 + n}"

    def _pincode(self) -> tuple[str, str]:
        zone = self.rng.choices([z for z, _ in ZONE_WEIGHTS], [w for _, w in ZONE_WEIGHTS])[0]
        prefix = self.rng.choice(ZONE_PINCODE_PREFIXES[zone])
        return f"{prefix}{self.rng.randint(1, 999):03d}", zone

    # ---- one order -----------------------------------------------------------------------

    def make_order(self) -> Plan:
        rng = self.rng
        channel = rng.choices(CHANNELS, CHANNEL_WEIGHTS)[0]
        self.counters[channel] += 1
        n = self.counters[channel]
        mode = "cod" if channel == "WEB" and rng.random() < 0.55 else "prepaid"
        value = max(299, int(round(rng.lognormvariate(math.log(1000), 0.55) / 100)) * 100 - 1)
        pincode, zone = self._pincode()
        weight = rng.choices([w for w, _ in WEIGHT_CHOICES], [p for _, p in WEIGHT_CHOICES])[0]
        dims = (rng.randint(20, 50), rng.randint(15, 40), rng.randint(4, 25))
        courier = awb = None
        if channel == "WEB":
            courier = rng.choices(["CA", "CB"], [0.6, 0.4])[0]
            # injective scrambles (multipliers coprime to the modulus): unique, well-spread digits
            awb = (str(10**11 + n * 1000003 % (9 * 10**11)) if courier == "CA"
                   else str(10**14 + n * 7919023757 % (9 * 10**14)))
        plan = Plan(
            ref=self._make_ref(channel, n), channel=channel,
            ordered_on=self.start + timedelta(days=rng.randrange(self.window)),
            mode=mode, value=value * 100, pincode=pincode, zone=zone, weight_g=weight,
            dims=dims, courier=courier, awb=awb,
        )
        self._lifecycle(plan)
        self._write_order(plan)
        if channel == "WEB":
            self._finance_web(plan)
        else:
            self._finance_channel(plan)
        self._receipt(plan)
        self._write_truth(plan)
        return plan

    def _lifecycle(self, p: Plan) -> None:
        rng, today = self.rng, self.today
        lo, hi = TRANSIT_DAYS[p.zone]
        arrive = p.ordered_on + timedelta(days=1 + rng.randint(lo, hi))
        if p.channel == "WEB" and p.mode == "cod":
            return_rate = COD_RTO_RATE
        elif p.channel == "WEB":
            return_rate = RETURN_RATE["WEB_PREPAID"]
        else:
            return_rate = RETURN_RATE[p.channel[0]]
        will_return = rng.random() < return_rate
        is_cod = p.channel == "WEB" and p.mode == "cod"
        back_days = rng.randint(3, 7) if is_cod else rng.randint(2, 8)
        if is_cod and will_return:  # refused at the door: return to origin (RTO)
            back = arrive + timedelta(days=back_days)
            if back <= today:
                p.state, p.status, p.returned_on = "rto", "returned", back
            return
        if arrive > today:
            return
        p.delivered_on, p.state, p.status = arrive, "delivered", "delivered"
        if will_return and arrive + timedelta(days=back_days) <= today:
            p.state, p.status = "returned", "returned"
            p.returned_on = arrive + timedelta(days=back_days)

    def _write_order(self, p: Plan) -> None:
        self.sinks["orders"].write([
            p.ref, p.channel, p.ordered_on.isoformat(), p.mode, money(p.value), p.pincode,
            p.weight_g, *p.dims, p.courier or "", p.awb or "", p.status,
            p.delivered_on.isoformat() if p.delivered_on else "",
            p.returned_on.isoformat() if p.returned_on else "",
        ])

    # ---- marketplaces and quick-commerce -------------------------------------------------

    def _finance_channel(self, p: Plan) -> None:
        rng = self.rng
        card, partner = CHANNEL_CARDS[p.channel], PARTNERS[p.channel]
        if p.state == "in_transit":
            p.pending = True
            return
        if p.state == "returned" and p.channel[0] == "Q":
            return  # quick-commerce returns net to zero: no settlement rows (assumption)
        comm = pct(p.value, card.commission_bps)
        ship = channel_ship_fee(card, p.grams)
        if p.state == "delivered":
            base, leaks = p.delivered_on, (
                ("unpaid", .015), ("short_paid", .025), ("overpaid", .003),
                ("boundary_ok", .004), ("boundary_flag", .004), ("noise", .06))
        else:
            base, leaks = p.returned_on, (("short_paid", .02), ("noise", .04))

        def build_lines(delta: int) -> list[tuple[str, int]]:
            if p.state == "delivered":
                out = [("sale", p.value), ("commission", -(comm + delta))]
                if card.fixed_paise:
                    out.append(("fixed_fee", -card.fixed_paise))
                if ship:
                    out.append(("shipping_fee", -ship))
                return [*out, ("tcs", -pct(p.value, card.tcs_bps)),
                        ("tds", -pct(p.value, card.tds_bps))]
            return [("sale", p.value), ("refund", -p.value), ("shipping_fee", -ship),
                    ("return_fee", -(ship + delta))]

        due, pay = self._dates(base, partner.payout_sla_days)
        p.due_on = due
        leak = pick(rng.random(), leaks)
        delta = 0
        if leak == "unpaid" and (due > self.today or sum(a for _, a in build_lines(0)) <= 0):
            leak = "none"  # not due yet, or fees exceed the sale so nothing is owed to us
        elif leak == "short_paid":
            delta = rng.randint(1100, 20000)
        elif leak == "overpaid":
            delta = -rng.randint(1100, min(5000, comm)) if comm >= 1100 else 0
            leak = leak if delta else "none"
        elif leak == "boundary_ok":
            delta = TOL
        elif leak == "boundary_flag":
            delta = TOL + 1
        elif leak == "noise":
            delta = rng.choice((-1, 1)) * rng.randint(1, TOL - 1)

        if leak == "unpaid":
            p.leak = leak
            p.findings.append("unpaid_past_due")
            p.delta = -sum(a for _, a in build_lines(0))
            return
        if pay > self.today:
            p.pending = True  # nothing has arrived yet, so there is nothing to be wrong about
            return

        p.leak, p.delta = leak, -delta
        if delta > TOL:
            p.findings.append("short_paid")
        elif delta < -TOL:
            p.flags.append("overpaid")
        elif delta:
            p.flags.append("tolerance_boundary" if abs(delta) == TOL else "rounding_noise")

        lines = build_lines(delta)
        missing_id = rng.random() < 0.007
        duplicate = rng.random() < 0.003
        if missing_id:
            p.flags.append("missing_order_id")
        if duplicate:
            p.flags.append("duplicate_line")
        utr = f"{p.channel}UTR{pay:%y%m%d}{rng.randint(100, 999)}"  # one UTR: a repeat is verbatim
        for _ in range(2 if duplicate else 1):
            self._write_settlement(p, lines, pay, blank_ref=missing_id, utr=utr)

    def _write_settlement(self, p: Plan, lines: list[tuple[str, int]], pay: date,
                          blank_ref: bool, utr: str) -> None:
        ch, net = p.channel, sum(a for _, a in lines)
        by_type = {t: a for t, a in lines}
        if ch == "M1":
            cell = "" if blank_ref else p.ref
            sink = self.sinks["m1"]
            for t, a in lines:
                row = [cell, fdate(pay, "dmy"), M1_TYPES[t], a / 100, utr]
                if isinstance(sink, XlsxSink):
                    sink.write_amount_row(row, a, 3)
                else:
                    sink.write([cell, fdate(pay, "dmy"), M1_TYPES[t], money(a), utr])
        elif ch == "M2":
            cell = "" if blank_ref else p.ref.lower() + " "  # lower-case, trailing space

            def neg(t: str) -> int:  # deductions are printed unsigned in this file
                return -by_type.get(t, 0)

            self.sinks["m2"].write([
                cell, fdate(pay, "dmony"), money(by_type["sale"]), money(neg("commission")),
                money(neg("fixed_fee")), money(neg("shipping_fee")), money(neg("tcs")),
                money(neg("tds")), money(neg("refund")), money(neg("return_fee")),
                money(net), utr,
            ])
        elif ch == "M3":
            cell = "" if blank_ref else p.ref
            for t, a in lines:
                kind, desc = M3_TYPES[t]
                self.sinks["m3"].write([f"S{pay:%Y%m%d}", fdate(pay, "iso_ts"), cell, kind, desc,
                                        money(a)])
        elif ch == "Q1":
            cell = "" if blank_ref else p.ref
            self.sinks["q1"].write([
                cell, fdate(pay, "dmy"), money(by_type["sale"], "inr"),
                money(-by_type["commission"], "inr"), money(-by_type["tcs"], "inr"),
                money(-by_type["tds"], "inr"), money(net, "inr"),
            ])
        else:  # Q2: deductions shown as negatives in brackets
            cell = "" if blank_ref else p.ref
            self.sinks["q2"].write([
                cell, fdate(pay, "iso"), money(by_type["sale"], "paren"),
                money(by_type["commission"], "paren"), money(by_type["tcs"], "paren"),
                money(by_type["tds"], "paren"), money(net, "paren"),
            ])

    # ---- own website: payment gateway + courier aggregators -----------------------------

    def _finance_web(self, p: Plan) -> None:
        card = COURIER_CARDS[p.courier]
        fwd = courier_freight(card, p.zone, p.grams)
        cod_fee = courier_cod_fee(card, p.value)
        ret = courier_return_freight(card, fwd)
        if p.mode == "prepaid":
            self._web_pg(p)
            self._web_prepaid_freight(p, fwd, ret)
        elif p.state == "delivered":
            self._web_cod_remit(p, fwd, cod_fee)
        elif p.state == "rto":
            self._web_rto_freight(p, fwd, ret)
        else:
            p.pending = True  # COD still in transit

    def _web_pg(self, p: Plan) -> None:
        rng, sla = self.rng, PARTNERS["WEB"].payout_sla_days
        fee = pct(p.value, PG_FEE_BPS)
        due, pay = self._dates(p.ordered_on, sla, max_early=1)
        p.due_on = due
        unpaid = (p.state == "delivered" and rng.random() < 0.005 and due <= self.today)
        if unpaid:
            p.leak = "unpaid"
            p.findings.append("unpaid_past_due")
            p.delta = -(p.value - fee)
        elif pay > self.today:
            p.pending = True
        else:
            missing_id = rng.random() < 0.007
            duplicate = rng.random() < 0.003
            if missing_id:
                p.flags.append("missing_order_id")
            if duplicate:
                p.flags.append("duplicate_line")
            cell = "" if missing_id else p.ref
            for _ in range(2 if duplicate else 1):
                self.sinks["pg"].write([cell, fdate(pay, "iso"), "settlement", money(p.value),
                                        money(fee), money(p.value - fee)])
        if p.state == "returned":
            _, refund_on = self._dates(p.returned_on, sla, max_early=1)
            if refund_on <= self.today:
                self.sinks["pg"].write([p.ref, fdate(refund_on, "iso"), "refund",
                                        money(-p.value), money(-fee), money(-p.value + fee)])
            else:
                p.pending = True
        elif p.state == "in_transit":
            p.pending = True

    def _freight_leak(self, p: Plan, has_return: bool, table) -> tuple[int, int, int, int]:
        """-> (extra_forward, extra_return, extra_billed_g, delta). Records the finding."""
        rng, card_addl = self.rng, COURIER_CARDS[p.courier].addl[p.zone]
        if p.findings:  # invariant: at most one financial leak per order
            return 0, 0, 0, 0
        leak = pick(rng.random(), table)
        extra_fwd = extra_ret = extra_g = 0
        if leak == "freight_overcharge":
            if has_return and rng.random() < 0.3:
                extra_ret = rng.randint(1500, 4000)
            else:
                extra_fwd, extra_g = card_addl, 500  # billed one weight slab too high
            p.findings.append("freight_overcharged")
        elif leak == "cod_fee_overcharge":
            extra_fwd = rng.randint(1500, 4000)
            p.findings.append("freight_overcharged")
        elif leak == "noise":
            extra_fwd = rng.randint(1, TOL - 1)
            p.flags.append("rounding_noise")
        p.leak, p.delta = leak, -(extra_fwd + extra_ret)
        return extra_fwd, extra_ret, extra_g, extra_fwd + extra_ret

    def _web_cod_remit(self, p: Plan, fwd: int, cod_fee: int) -> None:
        rng = self.rng
        due, pay = self._dates(p.delivered_on, PARTNERS[p.courier].payout_sla_days)
        p.due_on = due
        table = (("unremitted", .016), ("freight_overcharge", .020),
                 ("cod_fee_overcharge", .008), ("noise", .04))
        owed = p.value - fwd - cod_fee  # what the courier should remit; <= 0 means nothing owed
        if pick(rng.random(), table[:1]) == "unremitted" and due <= self.today and owed > 0:
            p.leak, p.delta = "unremitted", -owed
            p.findings.append("unpaid_past_due")
            return
        if pay > self.today:
            p.pending = True
            return
        extra_fwd, _, extra_g, _ = self._freight_leak(p, False, table[1:])
        billed_fwd, billed_cod = fwd, cod_fee
        if p.leak == "cod_fee_overcharge":
            billed_cod += extra_fwd
        else:
            billed_fwd += extra_fwd
        net = p.value - billed_fwd - billed_cod
        utr = f"{p.courier}UTR{pay:%y%m%d}{rng.randint(100, 999)}"
        billed_g = slabs_500(p.grams) * 500 + extra_g
        if p.courier == "CA":
            self.sinks["cod_ca"].write([
                p.awb, p.ref, fdate(p.delivered_on, "iso"), fdate(pay, "iso"), money(p.value),
                money(billed_fwd), money(billed_cod), billed_g, money(net), utr])
        else:
            self.sinks["cod_cb"].write([
                excelify(p.awb), p.ref, fdate(p.delivered_on, "dmy"), fdate(pay, "dmy"),
                money(p.value), money(billed_fwd), money(billed_cod), f"{billed_g / 1000:.1f}",
                money(net), utr])

    def _write_freight_row(self, p: Plan, billed_on: date, billed_g: int, fwd: int,
                           ret: int) -> None:
        if p.courier == "CA":
            invoice = f"CAINV{billed_on:%y%m}{billed_on.isocalendar()[1]:02d}"
            self.sinks["freight_ca"].write([
                invoice, fdate(billed_on, "iso"), p.awb, p.ref, p.zone, billed_g,
                money(fwd), money(ret), money(fwd + ret)])
        else:
            bill = f"CB/{billed_on:%Y}/{billed_on.isocalendar()[1]:02d}"
            self.sinks["freight_cb"].write([
                bill, fdate(billed_on, "dmy"), excelify(p.awb), p.ref, CB_ZONE_LABEL[p.zone],
                f"{billed_g / 1000:.1f}", money(fwd), money(ret), money(fwd + ret)])

    def _web_prepaid_freight(self, p: Plan, fwd: int, ret: int) -> None:
        if p.state not in ("delivered", "returned"):
            return
        lag = timedelta(days=FREIGHT_BILL_LAG_DAYS)
        fwd_on = p.delivered_on + lag
        returned = p.state == "returned"
        if returned and p.returned_on + lag <= self.today:
            billed_on, has_return = p.returned_on + lag, True
        elif fwd_on <= self.today:
            billed_on, has_return = fwd_on, False
        else:
            return
        table = (("freight_overcharge", .02), ("noise", .04))
        extra_fwd, extra_ret, extra_g, _ = self._freight_leak(p, has_return, table)
        self._write_freight_row(
            p, billed_on, slabs_500(p.grams) * 500 + extra_g, fwd + extra_fwd,
            (ret if has_return else 0) + extra_ret)

    def _web_rto_freight(self, p: Plan, fwd: int, ret: int) -> None:
        billed_on = p.returned_on + timedelta(days=FREIGHT_BILL_LAG_DAYS)
        if billed_on > self.today:
            return
        table = (("freight_overcharge", .03), ("noise", .04))
        extra_fwd, extra_ret, extra_g, _ = self._freight_leak(p, True, table)
        self._write_freight_row(
            p, billed_on, slabs_500(p.grams) * 500 + extra_g, fwd + extra_fwd, ret + extra_ret)

    # ---- returns, truth ------------------------------------------------------------------

    def _receipt(self, p: Plan) -> None:
        if p.returned_on is None:
            return
        rng = self.rng
        wait_over = p.returned_on + timedelta(days=RECEIPT_WAIT_DAYS) <= self.today
        scan_on = p.returned_on + timedelta(days=rng.randint(1, 6))
        never_scanned = rng.random() < 0.10
        if never_scanned and wait_over:
            p.findings.append("return_not_received")
        elif scan_on <= self.today:
            self.sinks["receipts"].write([
                p.ref, fdate(scan_on, "iso"),
                "Damaged" if rng.random() < 0.15 else "Good", f"WH-{rng.randint(1, 4)}"])
        else:
            p.pending = True

    def _write_truth(self, p: Plan) -> None:
        if p.state == "in_transit":
            p.pending = True
        status = next((f for f in STATUS_PRIORITY if f in p.findings), None)
        status = status or ("pending" if p.pending else "settled")
        self.stats[f"status:{status}"] += 1
        self.stats[f"state:{p.state}"] += 1
        if p.leak != "none":
            self.stats[f"leak:{p.leak}"] += 1
        for flag in p.flags:
            self.stats[f"flag:{flag}"] += 1
        if "return_not_received" in p.findings:
            self.stats["leak:return_not_received"] += 1
        self.sinks["truth"].write([
            p.ref, p.channel, p.state, status, "|".join(p.findings), "|".join(p.flags), p.leak,
            p.delta, p.due_on.isoformat() if p.due_on else "", p.mode, p.value])

    # ---- run -----------------------------------------------------------------------------

    def run(self) -> dict:
        started = time.perf_counter()
        for _ in range(self.n_orders):
            self.make_order()
        rows = {}
        for key, sink in self.sinks.items():
            if isinstance(sink, XlsxSink):
                sink.close(amount_col=3)
            else:
                sink.close()
            rows[FILES[key] if key != "m1" or self.use_xlsx else "settlement_m1.csv"] = sink.rows
        rate_cards = {
            "generator_version": GENERATOR_VERSION, "tolerance_paise": TOL,
            "receipt_wait_days": RECEIPT_WAIT_DAYS, "pg_fee_bps": PG_FEE_BPS,
            "partners": [asdict(p) for p in PARTNERS.values()],
            "channel_cards": {k: asdict(v) for k, v in CHANNEL_CARDS.items()},
            "courier_cards": {k: asdict(v) for k, v in COURIER_CARDS.items()},
            "zone_rules": ZONE_RULES,
            "volumetric_divisor": 5000,
        }
        (self.out / "rate_cards.json").write_text(json.dumps(rate_cards, indent=2), "utf-8")
        manifest = {
            "generator_version": GENERATOR_VERSION, "seed": self.seed, "orders": self.n_orders,
            "today": self.today.isoformat(), "rows": rows,
            "stats": dict(sorted(self.stats.items())),
            "seconds": round(time.perf_counter() - started, 2),
        }
        (self.out / "manifest.json").write_text(json.dumps(manifest, indent=2), "utf-8")
        return manifest


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--orders", type=int, default=3000)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", type=Path, default=Path("../sample_data/small"))
    ap.add_argument("--today", type=date.fromisoformat, default=date(2026, 11, 2))
    ap.add_argument("--no-xlsx", action="store_true", help="write the M1 file as CSV instead")
    args = ap.parse_args()
    if not args.no_xlsx and xlsxwriter is None:
        ap.error("xlsxwriter is not installed (pip install xlsxwriter) or pass --no-xlsx")
    manifest = Generator(args.out, args.orders, args.seed, args.today, not args.no_xlsx).run()
    print(f"Generated {args.orders} orders in {manifest['seconds']}s -> {args.out}")
    for name, count in manifest["rows"].items():
        print(f"  {name:<28}{count:>9} rows")
    print("Ground truth:")
    for key, count in manifest["stats"].items():
        print(f"  {key:<34}{count:>7}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())