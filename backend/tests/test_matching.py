"""The review queue: grouping, scoring, suggestions and what a reviewer can do."""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

import gen_data as g
import pytest

from app import demo
from app.recon import matching as m
from app.recon.engine import run_reconciliation

TODAY = date(2026, 11, 2)


# ---- scoring (pure) -----------------------------------------------------------------------------


def test_the_date_matters_less_the_closer_it_is_and_not_at_all_inside_three_days():
    assert m.date_factor(0) == m.date_factor(-3) == m.date_factor(3) == 1.0
    assert m.date_factor(4) < 1.0
    assert m.date_factor(10) < m.date_factor(5)
    assert m.date_factor(m.DATE_WINDOW_DAYS) == 0.0


def test_a_perfect_candidate_scores_one_and_a_stale_one_scores_less():
    assert m.score(0, 4, 4) == 1.0
    assert m.score(0, 0, 4) == 0.8      # fees all disagree
    assert m.score(0, 0, 0) == 0.9      # nothing to check: half marks
    assert m.score(21, 4, 4) == 0.7     # right amount, but weeks off


def test_confidence_is_high_only_when_clear_and_uncontested():
    assert m.confidence(1.0, 0.7, contested=False) == "high"
    assert m.confidence(1.0, 0.95, contested=False) == "medium"   # a close runner-up
    assert m.confidence(1.0, None, contested=True) == "medium"    # another payment wants it too
    assert m.confidence(0.6, None, contested=False) == "low"


def test_reasons_read_like_a_person_wrote_them():
    why = m.reasons_for(149900, -2, 3, 3)
    assert why[0] == "Payment of ₹1,499.00 equals the order value exactly"
    assert "2 days before" in why[1]
    assert "3 of 3 fee lines" in why[2]
    assert m.reasons_for(100, 1, 0, 0)[1] == "Paid 1 day after the expected payout date"


def _line(row: int, kind: str, import_id: int = 1) -> dict:
    return {"partner_id": 1, "import_id": import_id, "source_row": row, "line_type": kind,
            "reference": "U", "occurred_on": date(2026, 1, 1)}


def test_consecutive_rows_of_one_payment_stay_together():
    lines = [_line(10, "sale"), _line(11, "commission"), _line(12, "tcs")]
    groups = m._split_into_payments(lines)
    assert [(x.first_row, x.last_row, len(x.lines)) for x in groups] == [(10, 12, 3)]


def test_two_blank_payments_back_to_back_are_told_apart_by_their_sale_line():
    lines = [_line(10, "sale"), _line(11, "commission"), _line(12, "sale"), _line(13, "tds")]
    groups = m._split_into_payments(lines)
    assert [(x.first_row, x.last_row) for x in groups] == [(10, 11), (12, 13)]


def test_a_gap_in_the_row_numbers_starts_a_new_payment():
    groups = m._split_into_payments([_line(10, "sale"), _line(14, "commission")])
    assert len(groups) == 2


def test_a_wide_row_is_one_payment_whatever_order_its_lines_come_in():
    row = [_line(5, "commission"), _line(5, "sale"), _line(5, "tcs")]
    assert len(m._split_into_payments(row)) == 1


# ---- against the generator's ground truth -------------------------------------------------------


@pytest.fixture(scope="module")
def data(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("match")
    g.Generator(path, 3000, seed=5, today=TODAY, use_xlsx=True).run()
    return path


@pytest.fixture(scope="module")
def truth(data: Path) -> dict[str, dict[str, str]]:
    with (data / "truth.csv").open(encoding="utf-8", newline="") as f:
        return {r["order_ref"]: r for r in csv.DictReader(f)}


@pytest.fixture
def queued(dict_conn, data: Path):
    demo.load_dataset(dict_conn, data, echo=False)
    run_reconciliation(dict_conn, TODAY)
    return dict_conn, m.refresh(dict_conn)


def blank_orders(truth) -> set[str]:
    return {r for r, t in truth.items() if "missing_order_id" in t["flags"]}


def test_there_is_one_payment_in_the_queue_for_every_payment_that_lost_its_order_id(
        queued, truth):
    conn, stats = queued
    assert stats["groups_open"] == len(blank_orders(truth)) > 5
    assert stats["groups_with_suggestions"] == stats["groups_open"]


def test_every_high_confidence_suggestion_is_right_and_none_repeat(queued, truth):
    conn, _ = queued
    flagged = blank_orders(truth)
    high = [g_["suggestions"][0]["order_ref_raw"] for g_ in m.queue(conn, limit=500)
            if g_["suggestions"] and g_["suggestions"][0]["confidence"] == "high"]
    assert len(high) >= len(flagged) // 2
    assert set(high) <= flagged
    assert len(high) == len(set(high))


def test_the_right_order_is_nearly_always_among_the_top_three(queued, truth):
    conn, _ = queued
    flagged = blank_orders(truth)
    hits = sum(any(s["order_ref_raw"] in flagged for s in g_["suggestions"])
               for g_ in m.queue(conn, limit=500))
    assert hits >= len(flagged) - 1


def test_accepting_the_high_confidence_suggestions_makes_the_reconciliation_match_the_truth(
        queued, truth):
    conn, _ = queued
    accepted = {}
    for grp in m.queue(conn, limit=500):
        top = grp["suggestions"][0]
        if top["confidence"] == "high":
            assert m.accept(conn, top["id"], None) == grp["n_lines"]
            accepted[top["order_ref_raw"]] = grp
    run_reconciliation(conn, TODAY)
    rows = {r["ref"]: r for r in conn.execute(
        """SELECT o.order_ref_raw AS ref, s.status, s.variance_paise FROM order_status s
           JOIN orders o ON o.id = s.order_id""").fetchall()}
    assert accepted
    for ref in accepted:
        assert rows[ref]["status"] == truth[ref]["expected_status"], ref
        assert rows[ref]["variance_paise"] == int(truth[ref]["delta_paise"]), ref


def test_refreshing_twice_changes_nothing(queued):
    conn, first = queued
    assert m.refresh(conn) == first


def test_nothing_is_ever_linked_without_a_person(queued):
    conn, _ = queued
    unlinked = conn.execute("""SELECT count(*) AS n FROM ledger_lines
                               WHERE order_id IS NULL AND order_ref IS NULL""").fetchone()["n"]
    assert unlinked > 0
    assert conn.execute("SELECT count(*) AS n FROM match_groups WHERE status <> 'open'"
                        ).fetchone()["n"] == 0


def test_a_rejected_suggestion_does_not_come_back(queued):
    conn, _ = queued
    grp = next(x for x in m.queue(conn, limit=500) if len(x["suggestions"]) > 1)
    first = grp["suggestions"][0]
    m.reject(conn, first["id"], None)
    m.refresh(conn)
    again = next(x for x in m.queue(conn, limit=500) if x["id"] == grp["id"])
    assert first["order_id"] not in [s["order_id"] for s in again["suggestions"]]


def test_decisions_that_make_no_sense_are_refused(queued):
    conn, _ = queued
    grp = m.queue(conn, limit=500)[0]
    sug = grp["suggestions"][0]
    m.accept(conn, sug["id"], None)
    with pytest.raises(m.ReviewError):
        m.accept(conn, sug["id"], None)                        # already accepted
    with pytest.raises(m.ReviewError):
        m.link_manual(conn, grp["id"], sug["order_id"], None)  # already decided
    other = next(x for x in m.queue(conn, limit=500) if x["partner_id"] != grp["partner_id"])
    with pytest.raises(m.ReviewError):                         # wrong partner
        m.link_manual(conn, other["id"], sug["order_id"], None)


def test_an_order_that_already_has_a_payment_cannot_take_another(queued):
    conn, _ = queued
    grp = m.queue(conn, limit=500)[0]
    paid = conn.execute(
        """SELECT o.id FROM orders o JOIN ledger_lines l ON l.order_id = o.id
           WHERE o.channel_partner_id = %s AND l.line_type = 'sale' AND l.partner_id = %s
           LIMIT 1""", (grp["partner_id"], grp["partner_id"])).fetchone()
    with pytest.raises(m.ReviewError, match="already has a payment"):
        m.link_manual(conn, grp["id"], paid["id"], None)


def test_a_link_can_be_undone_and_the_payment_returns_to_the_queue(queued):
    conn, _ = queued
    grp = m.queue(conn, limit=500)[0]
    sug = grp["suggestions"][0]
    before = run_reconciliation(conn, TODAY)
    m.accept(conn, sug["id"], None)
    run_reconciliation(conn, TODAY)
    assert m.undo(conn, grp["id"], None) == grp["n_lines"]
    after = run_reconciliation(conn, TODAY)
    assert after.by_status == before.by_status
    back = next(x for x in m.queue(conn, limit=500) if x["id"] == grp["id"])
    assert back["suggestions"] and back["suggestions"][0]["status"] == "pending"


def test_a_reviewer_can_link_by_hand_and_dismiss(queued):
    conn, _ = queued
    groups = m.queue(conn, limit=500)
    by_hand, dismissed = groups[0], groups[1]
    target = by_hand["suggestions"][0]["order_id"]
    m.link_manual(conn, by_hand["id"], target, None, note="checked with the account manager")
    m.dismiss(conn, dismissed["id"], None, note="not ours")
    rows = {r["id"]: r for r in conn.execute("SELECT * FROM match_groups").fetchall()}
    assert rows[by_hand["id"]]["status"] == "linked"
    assert rows[by_hand["id"]]["link_method"] == "manual"
    assert rows[dismissed["id"]]["status"] == "dismissed"


def test_verbatim_duplicate_copies_travel_with_their_payment(queued):
    conn, _ = queued
    grp = m.queue(conn, limit=500)[0]
    originals = conn.execute("SELECT * FROM ledger_lines WHERE id = ANY(%s)",
                             (conn.execute("SELECT line_ids FROM match_groups WHERE id = %s",
                                           (grp["id"],)).fetchone()["line_ids"],)).fetchall()
    # the partner's file repeated this payment verbatim: the copies are stored with dup_seq 1
    for row in originals:
        conn.execute(
            """INSERT INTO ledger_lines (import_id, partner_id, order_id, order_ref, line_type,
                   amount_paise, occurred_on, reference, line_hash, dup_seq, source_row)
               VALUES (%s, %s, NULL, NULL, %s, %s, %s, %s, %s, 1, %s)""",
            (row["import_id"], row["partner_id"], row["line_type"], row["amount_paise"],
             row["occurred_on"], row["reference"], row["line_hash"], row["source_row"] + 100000))
    conn.commit()
    m.refresh(conn)
    ids = conn.execute("SELECT line_ids FROM match_groups WHERE id = %s",
                       (grp["id"],)).fetchone()["line_ids"]
    assert len(ids) == 2 * grp["n_lines"]
    top = next(x for x in m.queue(conn, limit=500) if x["id"] == grp["id"])["suggestions"][0]
    assert m.accept(conn, top["id"], None) == 2 * grp["n_lines"]
    left = conn.execute("SELECT count(*) AS n FROM ledger_lines WHERE id = ANY(%s) "
                        "AND order_id IS NULL", (ids,)).fetchone()["n"]
    assert left == 0