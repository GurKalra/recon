"""The numbers behind the screens must add up to the tables they come from."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import gen_data as g
import pytest

from app import claims as cl
from app import dashboard as dash
from app import demo
from app.recon.engine import run_reconciliation

TODAY = date(2026, 11, 2)


@pytest.fixture(scope="module")
def data(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("dash")
    g.Generator(path, 3000, seed=5, today=TODAY, use_xlsx=True).run()
    return path


@pytest.fixture
def world(dict_conn, data: Path):
    demo.load_dataset(dict_conn, data, echo=False)
    run_reconciliation(dict_conn, TODAY)
    return dict_conn


@pytest.mark.parametrize("days,bucket", [(0, "0-7"), (7, "0-7"), (8, "8-15"), (15, "8-15"),
                                         (16, "16-30"), (30, "16-30"), (31, "31-60"),
                                         (60, "31-60"), (61, "60+")])
def test_age_buckets_have_the_agreed_edges(days, bucket):
    assert dash.age_bucket(days) == bucket


def test_the_headline_matches_the_findings(world):
    h = dash.headline(world, TODAY)
    risk = world.execute("SELECT sum(amount_paise) AS s, count(*) AS n FROM findings "
                         "WHERE status IN ('open', 'claimed')").fetchone()
    assert h["money_at_risk_paise"] == risk["s"] and h["findings_at_risk"] == risk["n"]
    assert h["open_paise"] + h["claimed_paise"] == h["money_at_risk_paise"]
    assert h["orders"] == 3000 and h["last_run"]["as_of"] == TODAY
    assert 0 < h["auto_match_pct"] < 100 and h["lines_to_review"] > 0
    assert h["expiring_7d_paise"] == sum(
        r["amount_paise"] for r in cl.candidates(world, TODAY, bucket="critical", limit=5000)
        if r["days_left"] >= 0)


def test_the_risk_matrix_adds_up_both_ways(world):
    m = dash.risk_matrix(world, TODAY)
    assert m["amount_paise"] == dash.headline(world, TODAY)["money_at_risk_paise"]
    for p in m["partners"]:
        assert sum(b["amount_paise"] for b in p["buckets"].values()) == p["amount_paise"]
    for b in dash.AGE_BUCKETS:
        assert m["totals"][b]["amount_paise"] == sum(
            p["buckets"][b]["amount_paise"] for p in m["partners"])
    sizes = [p["amount_paise"] for p in m["partners"]]
    assert sizes == sorted(sizes, reverse=True)


def test_money_in_a_claim_is_still_at_risk_until_it_is_paid(world):
    before = dash.headline(world, TODAY)["money_at_risk_paise"]
    row = cl.candidates(world, TODAY, limit=1)[0]
    cid = cl.create_claim(world, row["partner_id"], [row["id"]])
    cl.submit_claim(world, cid, TODAY)
    assert dash.headline(world, TODAY)["money_at_risk_paise"] == before
    item = cl.claim_detail(world, cid, TODAY)["items"][0]
    cl.record_outcome(world, item["id"], "recovered")
    after = dash.headline(world, TODAY)
    assert after["money_at_risk_paise"] == before - row["amount_paise"]
    assert after["recovered_paise"] == row["amount_paise"]


def test_the_types_add_up_to_the_money_at_risk(world):
    assert sum(t["amount_paise"] for t in dash.by_type(world)) == \
        dash.headline(world, TODAY)["money_at_risk_paise"]
    assert sum(dash.status_counts(world).values()) == 3000


def test_tax_deducted_is_reported_as_positive_credit(world):
    rows = dash.tax_summary(world)
    assert rows and all(r["tcs_paise"] >= 0 and r["tds_paise"] >= 0 for r in rows)
    total = world.execute("SELECT -sum(amount_paise) AS s FROM ledger_lines "
                          "WHERE line_type = 'tcs'").fetchone()["s"]
    assert sum(r["tcs_paise"] for r in rows) == total


def test_the_order_list_filters_sorts_and_pages(world):
    page = dash.orders_list(world, limit=10)
    assert page["total"] == 3000 and len(page["rows"]) == 10
    variances = [r["variance_paise"] for r in page["rows"]]
    assert variances == sorted(variances)                        # biggest shortfall first
    problems = dash.orders_list(world, problems_only=True, limit=500)
    assert problems["total"] == 156 or problems["total"] > 0
    assert {r["status"] for r in problems["rows"]} <= {
        "short_paid", "unpaid_past_due", "freight_overcharged", "return_not_received"}
    only = dash.orders_list(world, status="short_paid", limit=500)
    assert only["total"] == len(only["rows"]) and {r["status"] for r in only["rows"]} == {
        "short_paid"}
    p1 = dash.orders_list(world, sort="newest", limit=5, offset=0)["rows"]
    p2 = dash.orders_list(world, sort="newest", limit=5, offset=5)["rows"]
    assert not {r["id"] for r in p1} & {r["id"] for r in p2}
    assert dash.orders_list(world, limit=10**6)["limit"] == 500


def test_orders_can_be_found_by_reference_or_awb(world):
    row = world.execute("SELECT order_ref_raw, awb FROM orders WHERE awb IS NOT NULL "
                        "LIMIT 1").fetchone()
    hit = dash.orders_list(world, search=row["order_ref_raw"].lower())
    assert any(r["order_ref"] == row["order_ref_raw"] for r in hit["rows"])
    assert dash.orders_list(world, search=row["awb"])["total"] >= 1
    assert dash.orders_list(world, search="NOSUCHORDER999")["total"] == 0
    with pytest.raises(ValueError):
        dash.orders_list(world, sort="drop table")


def test_dates_and_payment_mode_filter_the_list(world):
    cod = dash.orders_list(world, payment_mode="cod", limit=500)
    assert cod["rows"] and {r["payment_mode"] for r in cod["rows"]} == {"cod"}
    since = TODAY - timedelta(days=30)
    recent = dash.orders_list(world, date_from=since, limit=500)
    assert recent["rows"] and all(r["ordered_on"] >= since for r in recent["rows"])


def test_the_order_drawer_explains_a_problem_order(world):
    row = world.execute("SELECT order_id FROM findings WHERE type = 'short_paid' "
                        "LIMIT 1").fetchone()
    d = dash.order_detail(world, row["order_id"], TODAY)
    assert d["order"]["status"] == "short_paid" and d["waterfall"] and d["lines"]
    assert d["findings"][0]["type"] == "short_paid"
    assert all(line["filename"] and line["source_row"] for line in d["lines"])
    short = sum(int(w["expected_paise"]) - int(w["received_paise"]) for w in d["waterfall"]
                if w["is_expected"])
    assert short > 0
    assert dash.order_detail(world, 10**9, TODAY) is None

def test_a_matrix_cell_opens_exactly_the_orders_behind_it(world):
    matrix = dash.risk_matrix(world, TODAY)
    row = max(matrix["partners"], key=lambda p: p["amount_paise"])
    bucket = max(row["buckets"], key=lambda b: row["buckets"][b]["amount_paise"])
    partner_id = world.execute("SELECT id FROM partners WHERE code = %s",
                               (row["partner_code"],)).fetchone()["id"]
    page = dash.orders_list(world, risk_partner_id=partner_id, age=bucket, as_of=TODAY, limit=500)
    wanted = world.execute(
        "SELECT count(DISTINCT order_id) AS n FROM findings f WHERE f.partner_id = %s "
        "AND f.status = ANY(ARRAY['open','claimed']) AND "
        "CASE WHEN %s - f.since_on <= 7 THEN '0-7' WHEN %s - f.since_on <= 15 THEN '8-15' "
        "WHEN %s - f.since_on <= 30 THEN '16-30' WHEN %s - f.since_on <= 60 THEN '31-60' "
        "ELSE '60+' END = %s", (partner_id, TODAY, TODAY, TODAY, TODAY, bucket)).fetchone()["n"]
    assert page["total"] == wanted > 0
    with pytest.raises(ValueError):
        dash.orders_list(world, age="1-2")


def test_the_returns_tracker_agrees_with_the_findings(world):
    out = dash.returns_tracker(world, TODAY, limit=500)
    missing = dash.returns_tracker(world, TODAY, state="missing", limit=500)
    findings = world.execute(
        "SELECT count(*) AS n FROM findings WHERE type = 'return_not_received' "
        "AND status = ANY(ARRAY['open','claimed'])").fetchone()["n"]
    assert out["wait_days"] > 0 and missing["total"] == out["counts"]["missing"]
    assert missing["total"] == findings
    assert all(r["state"] == "missing" and r["received_on"] is None for r in missing["rows"])
    assert sum(out["counts"].values()) == out["total"]
    with pytest.raises(ValueError):
        dash.returns_tracker(world, TODAY, state="lost")
