"""The rules engine against the generator's ground truth, plus its lifecycle behaviour."""

from __future__ import annotations

import csv
import hashlib
from datetime import date
from pathlib import Path

import gen_data as g
import pytest

from app import demo
from app.recon.engine import explain_orders, run_reconciliation

TODAY = date(2026, 11, 2)
FINANCIAL = {"unpaid_past_due", "short_paid", "freight_overcharged"}


@pytest.fixture(scope="module")
def data(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("recon")
    g.Generator(path, 2000, seed=11, today=TODAY, use_xlsx=True).run()
    return path


@pytest.fixture(scope="module")
def truth(data: Path) -> dict[str, dict[str, str]]:
    with (data / "truth.csv").open(encoding="utf-8", newline="") as f:
        return {r["order_ref"]: r for r in csv.DictReader(f)}


@pytest.fixture
def reconciled(dict_conn, data: Path):
    demo.load_dataset(dict_conn, data, echo=False)
    return dict_conn, run_reconciliation(dict_conn, TODAY)


def scalar(conn, sql: str, *params):
    return next(iter(conn.execute(sql, params).fetchone().values()))


def blank_id(truth, ref: str) -> bool:
    return "missing_order_id" in truth[ref]["flags"]


def verdicts(conn) -> dict[str, dict]:
    rows = conn.execute("""SELECT o.order_ref_raw AS ref, s.status, s.expected_paise,
                           s.received_paise, s.variance_paise, s.flags
                           FROM order_status s JOIN orders o ON o.id = s.order_id""").fetchall()
    return {r["ref"]: r for r in rows}


# ---- the engine agrees with the ground truth --------------------------------------------------


def test_every_status_and_every_rupee_of_variance_matches_the_ground_truth(reconciled, truth):
    conn, _ = reconciled
    checked = 0
    for ref, v in verdicts(conn).items():
        if blank_id(truth, ref):
            continue
        t = truth[ref]
        assert v["status"] == t["expected_status"], (ref, t["leak"], t["state"])
        assert v["variance_paise"] == int(t["delta_paise"]), (ref, t["leak"])
        checked += 1
    assert checked > 1900


def test_the_findings_table_is_exactly_the_planted_findings(reconciled, truth):
    conn, _ = reconciled
    engine = {(r["ref"], r["type"]): r["amount_paise"] for r in conn.execute(
        """SELECT o.order_ref_raw AS ref, f.type, f.amount_paise FROM findings f
           JOIN orders o ON o.id = f.order_id WHERE f.status = 'open'""").fetchall()
        if not blank_id(truth, r["ref"])}
    planted = {(ref, f) for ref, t in truth.items() if not blank_id(truth, ref)
               for f in t["findings"].split("|") if f}
    assert set(engine) == planted
    for (ref, kind), amount in engine.items():
        if kind in FINANCIAL:
            assert amount == -int(truth[ref]["delta_paise"]), (ref, kind)
        else:  # a return that never arrived is worth the order value
            assert amount == int(truth[ref]["value_paise"])


def test_the_claim_clock_is_the_partners_claim_window(reconciled):
    conn, _ = reconciled
    bad = scalar(conn, """SELECT count(*) FROM findings f JOIN partners p ON p.id = f.partner_id
                          WHERE f.claim_deadline <> f.since_on + p.claim_window_days""")
    assert bad == 0


def test_duplicates_and_overpayments_are_flagged_not_counted(reconciled, truth):
    conn, _ = reconciled
    got = verdicts(conn)
    for ref, t in truth.items():
        if blank_id(truth, ref):
            continue
        for flag in ("duplicate_line", "overpaid"):
            assert (flag in got[ref]["flags"]) == (flag in t["flags"]), (ref, flag)


def test_payments_with_no_order_id_are_not_silently_treated_as_settled(reconciled, truth):
    conn, _ = reconciled
    got = verdicts(conn)
    blank = [ref for ref in truth if blank_id(truth, ref)]
    assert blank
    for ref in blank:  # until a human links the payment, the order looks unpaid or pending
        assert got[ref]["status"] in ("unpaid_past_due", "pending", "return_not_received"), ref


# ---- lifecycle ----------------------------------------------------------------------------------


def test_running_again_changes_nothing(reconciled):
    conn, first = reconciled
    before = verdicts(conn)
    ids = {r["order_id"]: r["first_seen_run"] for r in conn.execute(
        "SELECT order_id, first_seen_run FROM findings").fetchall()}
    second = run_reconciliation(conn, TODAY)
    assert verdicts(conn) == before
    assert second.findings_new == 0 and second.findings_resolved == 0
    assert second.findings_open == first.findings_open
    assert {r["order_id"]: r["first_seen_run"] for r in conn.execute(
        "SELECT order_id, first_seen_run FROM findings").fetchall()} == ids
    assert scalar(conn, "SELECT count(*) FROM findings") == first.findings_open


def _insert_expected_payment(conn, order_id: int) -> list[int]:
    """Pay an order exactly what the rate card says, as a later settlement file would."""
    import_id = scalar(conn, "SELECT id FROM imports ORDER BY id LIMIT 1")
    ids = []
    for row in explain_orders(conn, [order_id], TODAY):
        if not row["is_expected"] or row["grp"] != "channel":
            continue
        digest = hashlib.sha256(f"manual|{order_id}|{row['line_type']}".encode()).hexdigest()
        ids.append(conn.execute(
            """INSERT INTO ledger_lines (import_id, partner_id, order_id, order_ref, line_type,
                                         amount_paise, occurred_on, line_hash)
               VALUES (%s, %s, %s, 'MANUAL', %s, %s, %s, %s) RETURNING id""",
            (import_id, row["partner_id"], order_id, row["line_type"], row["expected_paise"],
             TODAY, digest)).fetchone()["id"])
    conn.commit()
    return ids


def test_a_finding_resolves_when_the_money_arrives_and_reopens_if_it_goes(reconciled, truth):
    conn, _ = reconciled
    target = conn.execute(
        """SELECT f.id, f.order_id FROM findings f JOIN orders o ON o.id = f.order_id
           JOIN partners p ON p.id = o.channel_partner_id
           WHERE f.type = 'unpaid_past_due' AND p.type = 'marketplace' AND f.status = 'open'
           ORDER BY f.id""").fetchall()
    target = next(t for t in target if not blank_id(truth, conn.execute(
        "SELECT order_ref_raw FROM orders WHERE id = %s", (t["order_id"],)).fetchone()[
            "order_ref_raw"]))
    line_ids = _insert_expected_payment(conn, target["order_id"])
    assert line_ids

    result = run_reconciliation(conn, TODAY)
    row = conn.execute("SELECT status, resolved_at FROM findings WHERE id = %s",
                       (target["id"],)).fetchone()
    assert row["status"] == "resolved" and row["resolved_at"] is not None
    assert result.findings_resolved >= 1
    assert scalar(conn, "SELECT status FROM order_status WHERE order_id = %s",
                  target["order_id"]) == "settled"

    conn.execute("DELETE FROM ledger_lines WHERE id = ANY(%s)", (line_ids,))
    conn.commit()
    run_reconciliation(conn, TODAY)
    row = conn.execute("SELECT status, resolved_at FROM findings WHERE id = %s",
                       (target["id"],)).fetchone()
    assert row["status"] == "open" and row["resolved_at"] is None


def test_whether_something_is_overdue_depends_on_the_date_you_ask_about(reconciled):
    conn, now = reconciled
    earlier = run_reconciliation(conn, date(2026, 9, 15))
    assert earlier.by_status.get("unpaid_past_due", 0) < now.by_status["unpaid_past_due"]
    assert earlier.by_status.get("pending", 0) > now.by_status["pending"]


def test_a_new_rate_card_only_applies_to_orders_placed_after_it_takes_effect(reconciled):
    conn, _ = reconciled
    before = verdicts(conn)
    m2 = conn.execute("SELECT id FROM partners WHERE code = 'M2'").fetchone()["id"]
    old = conn.execute("SELECT * FROM channel_cards WHERE partner_id = %s", (m2,)).fetchone()
    new_id = conn.execute(
        """INSERT INTO channel_cards (partner_id, effective_from, commission_bps, fixed_paise,
                                      ship_extra_per_kg_paise, tcs_bps, tds_bps, note)
           VALUES (%s, '2026-10-01', %s, %s, %s, %s, %s, 'cheaper commission')
           RETURNING id""",
        (m2, old["commission_bps"] - 500, old["fixed_paise"], old["ship_extra_per_kg_paise"],
         old["tcs_bps"], old["tds_bps"])).fetchone()["id"]
    conn.execute("INSERT INTO channel_ship_slabs (card_id, max_g, fee_paise) "
                 "SELECT %s, max_g, fee_paise FROM channel_ship_slabs WHERE card_id = %s",
                 (new_id, old["id"]))
    conn.commit()
    run_reconciliation(conn, TODAY)
    after = verdicts(conn)
    placed = {r["ref"]: r["ordered_on"] for r in conn.execute(
        """SELECT o.order_ref_raw AS ref, o.ordered_on FROM orders o
           WHERE o.channel_partner_id = %s""", (m2,)).fetchall()}
    older = [ref for ref, d in placed.items() if d < date(2026, 10, 1)]
    newer = [ref for ref, d in placed.items() if d >= date(2026, 10, 1)]
    assert older and newer
    assert all(after[r]["status"] == before[r]["status"] for r in older)  # untouched
    flipped = [r for r in newer
               if before[r]["status"] != "short_paid" and after[r]["status"] == "short_paid"]
    assert flipped, "orders placed under the new card are now judged against its lower commission"


def test_explain_agrees_with_the_stored_verdict_and_writes_nothing(reconciled):
    conn, _ = reconciled
    ids = [r["id"] for r in conn.execute("SELECT id FROM orders ORDER BY id LIMIT 40").fetchall()]
    runs_before = scalar(conn, "SELECT count(*) FROM recon_runs")
    rows = explain_orders(conn, ids, TODAY)
    received: dict[int, int] = {}
    for r in rows:
        received[r["order_id"]] = received.get(r["order_id"], 0) + r["received_paise"]
    for order_id in ids:
        assert received.get(order_id, 0) == scalar(
            conn, "SELECT received_paise FROM order_status WHERE order_id = %s", order_id)
    assert scalar(conn, "SELECT count(*) FROM recon_runs") == runs_before


def test_a_failed_run_is_recorded_and_leaves_the_previous_verdicts_in_place(reconciled,
                                                                           monkeypatch):
    conn, _ = reconciled
    before = verdicts(conn)
    from app.recon import engine

    monkeypatch.setattr(engine, "_FIND", "SELECT this is not valid sql")
    with pytest.raises(Exception, match="syntax"):
        run_reconciliation(conn, TODAY)
    assert verdicts(conn) == before
    row = conn.execute("SELECT status, error FROM recon_runs ORDER BY id DESC LIMIT 1").fetchone()
    assert row["status"] == "failed" and "syntax" in row["error"]