"""The generator is the oracle for every later test, so it gets tested first."""

from __future__ import annotations

import csv
import re
import time
from datetime import date, datetime
from pathlib import Path

import fastexcel
import gen_data as g
import pytest

TODAY = date(2026, 11, 2)
N = 6000


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def paise(text: str) -> int:
    """'1499.00' / '-46.00' -> integer paise, without floats."""
    sign = -1 if text.startswith("-") else 1
    rupees, cents = text.lstrip("-").split(".")
    return sign * (int(rupees) * 100 + int(cents))


@pytest.fixture(scope="module")
def out(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("gen")
    g.Generator(path, N, seed=7, today=TODAY, use_xlsx=True).run()
    return path


@pytest.fixture(scope="module")
def orders(out: Path) -> dict[str, dict[str, str]]:
    return {r["Order ID"]: r for r in read_csv(out / "orders.csv")}


@pytest.fixture(scope="module")
def truth(out: Path) -> dict[str, dict[str, str]]:
    return {r["order_ref"]: r for r in read_csv(out / "truth.csv")}


# ---- pure rules ---------------------------------------------------------------------------


def test_pct_rounds_half_up_in_integers():
    assert g.pct(100_00, 50) == 50  # 0.5% of Rs 100
    assert g.pct(1, 5000) == 1  # 0.5 rounds up
    assert g.pct(149_900, 10) == 150


def test_indian_grouping_and_money_styles():
    assert g.money(10_800_000, "inr") == "₹1,08,000.00"
    assert g.money(-4_600, "plain") == "-46.00"
    assert g.money(-4_600, "paren") == "(46.00)"
    assert g.money(149_900, "paren") == "1,499.00"
    assert g._indian(999) == "999" and g._indian(1_000) == "1,000"


def test_chargeable_weight_is_max_of_actual_and_volumetric():
    assert g.volumetric_g(35, 25, 15) == 2625
    assert g.chargeable_g(800, 35, 25, 15) == 2625
    assert g.chargeable_g(3500, 20, 15, 4) == 3500
    assert g.slabs_500(500) == 1 and g.slabs_500(501) == 2 and g.slabs_500(2625) == 6


def test_zone_rules():
    assert g.zone_for("560001") == "A"
    assert g.zone_for("575001") == "B"
    assert g.zone_for("110001") == "C"
    assert g.zone_for("226001") == "D"


def test_freight_cod_and_marketplace_fees():
    ca = g.COURIER_CARDS["CA"]
    assert g.courier_freight(ca, "A", 400) == 4000
    assert g.courier_freight(ca, "A", 1200) == 4000 + 2 * 3000
    assert g.courier_cod_fee(ca, 100_000) == 3000  # flat wins on small orders
    assert g.courier_cod_fee(ca, 300_000) == 6000  # 2% wins on large ones
    m1 = g.CHANNEL_CARDS["M1"]
    assert g.channel_ship_fee(m1, 2000) == 11000
    assert g.channel_ship_fee(m1, 2001) == 11000 + 4000  # one started extra kg
    assert g.channel_ship_fee(g.CHANNEL_CARDS["Q1"], 5000) == 0


def test_excel_mangles_long_ids():
    assert re.fullmatch(r"\d\.\d{5}E\+\d{2}", g.excelify("123456789012345"))


# ---- determinism and structure ---------------------------------------------------------------


def test_same_seed_gives_identical_csv_files(tmp_path: Path, out: Path):
    other = tmp_path / "again"
    g.Generator(other, N, seed=7, today=TODAY, use_xlsx=True).run()
    for name in g.FILES.values():
        if name.endswith(".csv"):
            assert (out / name).read_bytes() == (other / name).read_bytes(), name


def test_different_seed_gives_different_data(tmp_path: Path, out: Path):
    g.Generator(tmp_path, 500, seed=8, today=TODAY, use_xlsx=False).run()
    assert (out / "orders.csv").read_bytes() != (tmp_path / "orders.csv").read_bytes()


def test_every_file_has_its_documented_header(out: Path):
    for key, header in g.HEADERS.items():
        if key == "m1":
            continue  # xlsx, checked separately
        with (out / g.FILES[key]).open(encoding="utf-8-sig") as f:
            assert next(csv.reader(f)) == header, key


def test_order_refs_are_unique_and_well_formed(orders):
    patterns = {
        "WEB": r"ORD-W-\d{6}", "M1": r"ORD-M-\d{5,6}", "M2": r"OD\d{13}",
        "M3": r"40\d-\d{7}-\d{7}", "Q1": r"QC1-\d{5,6}", "Q2": r"Q2\d{6}",
    }
    assert len(orders) == N
    for ref, row in orders.items():
        assert re.fullmatch(patterns[row["Channel"]], ref), ref


def test_no_date_in_any_file_is_in_the_future(out: Path):
    iso = re.compile(r"^(\d{4}-\d{2}-\d{2})")
    dmy = re.compile(r"^(\d{2})/(\d{2})/(\d{4})$")
    dmony = re.compile(r"^(\d{2})-([A-Z][a-z]{2})-(\d{4})$")
    for name in g.FILES.values():
        if not name.endswith(".csv") or name == "truth.csv":  # truth holds future DUE dates
            continue
        for row in read_csv(out / name):
            for cell in row.values():
                if m := iso.match(cell):
                    d = date.fromisoformat(m.group(1))
                elif m := dmy.match(cell):
                    d = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
                elif m := dmony.match(cell):
                    d = datetime.strptime(cell, "%d-%b-%Y").date()
                else:
                    continue
                assert d <= TODAY, (name, cell)


def test_xlsx_has_title_rows_header_data_and_a_footer_total(out: Path):
    sheet = fastexcel.read_excel(str(out / "settlement_m1.xlsx")).load_sheet(0, header_row=None)
    rows = sheet.to_polars().rows()
    assert rows[0][0].startswith("Marketplace One")
    assert rows[3][0] is None  # blank separator row
    assert list(rows[4]) == g.HEADERS["m1"]
    assert rows[-1][0] == "Total"
    body = rows[5:-1]
    assert abs(sum(float(r[3]) for r in body) - float(rows[-1][3])) < 0.01


def test_file_quirks_are_present(out: Path):
    assert (out / "settlement_q1.csv").read_bytes().startswith(b"\xef\xbb\xbf")  # BOM
    assert (out / "cod_remittance_cb.csv").read_bytes().startswith(b"\xef\xbb\xbf")
    cb = read_csv(out / "cod_remittance_cb.csv")
    assert all(re.fullmatch(r"\d\.\d{5}E\+\d{2}", r["Waybill"]) for r in cb)
    assert len({r["Waybill"] for r in cb}) > 0.9 * len(cb)  # mangled but still varied
    m2 = read_csv(out / "settlement_m2.csv")
    assert all(r["order_id"] == r["order_id"].lower() for r in m2 if r["order_id"])
    assert any(r["order_id"].endswith(" ") for r in m2)
    assert any(r["Commission"].startswith("(") for r in read_csv(out / "settlement_q2.csv"))
    assert any(r["Sale Value"].startswith("₹") for r in read_csv(out / "settlement_q1.csv"))


# ---- ground truth ------------------------------------------------------------------------------


def test_truth_status_follows_the_priority_rule(truth):
    for row in truth.values():
        findings = [f for f in row["findings"].split("|") if f]
        expected = next((f for f in g.STATUS_PRIORITY if f in findings), None)
        if expected:
            assert row["expected_status"] == expected
        else:
            assert row["expected_status"] in ("pending", "settled")


def test_at_most_one_financial_leak_per_order(truth):
    financial = {"unpaid_past_due", "short_paid", "freight_overcharged"}
    for row in truth.values():
        assert len(financial & set(row["findings"].split("|"))) <= 1, row


def test_tolerance_boundary_is_exactly_ten_rupees(truth):
    ok = [r for r in truth.values() if r["leak"] == "boundary_ok"]
    flagged = [r for r in truth.values() if r["leak"] == "boundary_flag"]
    assert ok and flagged
    assert all(r["expected_status"] != "short_paid" and r["delta_paise"] == "-1000" for r in ok)
    assert all(r["expected_status"] == "short_paid" and r["delta_paise"] == "-1001"
               for r in flagged)


def test_unpaid_orders_are_really_past_due(truth):
    unpaid = [r for r in truth.values() if "unpaid_past_due" in r["findings"]]
    assert unpaid
    assert all(date.fromisoformat(r["due_on"]) <= TODAY for r in unpaid)


def test_every_leak_kind_is_planted_at_least_once(out: Path):
    import json
    stats = json.loads((out / "manifest.json").read_text())["stats"]
    for key in ("unpaid", "unremitted", "short_paid", "freight_overcharge", "cod_fee_overcharge",
                "overpaid", "boundary_ok", "boundary_flag", "noise", "return_not_received"):
        assert stats.get(f"leak:{key}", 0) > 0, key
    for flag in ("duplicate_line", "missing_order_id"):
        assert stats.get(f"flag:{flag}", 0) > 0, flag


def _flagged(truth, ref: str) -> bool:
    flags = truth[ref]["flags"]
    return "missing_order_id" in flags or "duplicate_line" in flags


def test_marketplace_m2_arithmetic_and_labelled_delta(out: Path, orders, truth):
    """Recompute the expected payout from the rules and compare with what the file paid."""
    card, checked = g.CHANNEL_CARDS["M2"], 0
    for row in read_csv(out / "settlement_m2.csv"):
        deductions = sum(paise(row[c]) for c in (
            "commission", "fixed_fee", "shipping_fee", "tcs", "tds", "refund", "return_fee"))
        assert paise(row["net_payout"]) == paise(row["gross_sale"]) - deductions
        if not row["order_id"]:
            continue
        ref = next(r for r in orders if r.lower() == row["order_id"].strip())
        if _flagged(truth, ref):
            continue
        o = orders[ref]
        value = paise(o["Order Value"])
        grams = g.chargeable_g(int(o["Weight (g)"]), int(o["Length (cm)"]),
                               int(o["Width (cm)"]), int(o["Height (cm)"]))
        ship = g.channel_ship_fee(card, grams)
        if o["Delivery Status"] == "delivered":
            expected = (value - g.pct(value, card.commission_bps) - card.fixed_paise - ship
                        - g.pct(value, card.tcs_bps) - g.pct(value, card.tds_bps))
        else:
            expected = -2 * ship
        assert paise(row["net_payout"]) - expected == int(truth[ref]["delta_paise"]), ref
        checked += 1
    assert checked > 100


def test_cod_remittance_ca_arithmetic_and_labelled_delta(out: Path, orders, truth):
    card, checked = g.COURIER_CARDS["CA"], 0
    for row in read_csv(out / "cod_remittance_ca.csv"):
        o = orders[row["Order Ref"]]
        value = paise(o["Order Value"])
        assert paise(row["COD Collected"]) == value
        assert paise(row["Net Remitted"]) == (
            value - paise(row["Freight Charged"]) - paise(row["COD Fee"]))
        grams = g.chargeable_g(int(o["Weight (g)"]), int(o["Length (cm)"]),
                               int(o["Width (cm)"]), int(o["Height (cm)"]))
        expected = (value - g.courier_freight(card, g.zone_for(o["Pincode"]), grams)
                    - g.courier_cod_fee(card, value))
        assert paise(row["Net Remitted"]) - expected == int(truth[row["Order Ref"]]["delta_paise"])
        checked += 1
    assert checked > 50


def test_payment_gateway_rows_are_internally_consistent(out: Path):
    rows = read_csv(out / "settlement_web_pg.csv")
    assert rows
    for r in rows:
        assert paise(r["Net (INR)"]) == paise(r["Gross (INR)"]) - paise(r["Fee (INR)"])
        assert (r["Type"] == "refund") == r["Gross (INR)"].startswith("-")


def test_returns_without_a_scan_are_exactly_the_not_received_findings(out: Path, orders, truth):
    scanned = {r["Return Order ID"] for r in read_csv(out / "warehouse_receipts.csv")}
    for ref, row in truth.items():
        returned_on = orders[ref]["Returned On"]
        if not returned_on:
            assert ref not in scanned
            continue
        wait_over = (TODAY - date.fromisoformat(returned_on)).days >= g.RECEIPT_WAIT_DAYS
        if "return_not_received" in row["findings"]:
            assert ref not in scanned and wait_over
        elif ref not in scanned:
            assert not wait_over  # missing scan, but still inside the grace period


def test_generation_is_fast_enough_for_the_stress_dataset(tmp_path: Path):
    start = time.perf_counter()
    g.Generator(tmp_path, 20_000, seed=1, today=TODAY, use_xlsx=True).run()
    assert time.perf_counter() - start < 30