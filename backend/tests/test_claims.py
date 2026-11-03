"""Claims: what can be claimed, the lifecycle, how a later payment closes a claim, and the
evidence pack."""

from __future__ import annotations

import csv
import hashlib
import io
import zipfile
from datetime import date, timedelta
from pathlib import Path

import gen_data as g
import pytest

from app import claim_evidence as ev
from app import claims as cl
from app import demo
from app.recon.engine import explain_orders, run_reconciliation

TODAY = date(2026, 11, 2)


# ---- pure pieces -------------------------------------------------------------------------------


@pytest.mark.parametrize("days,expected", [(-1, "expired"), (0, "critical"), (7, "critical"),
                                           (8, "soon"), (14, "soon"), (15, "ok")])
def test_urgency_follows_the_days_left(days, expected):
    assert cl.urgency(days) == expected


@pytest.mark.parametrize("text", ["=cmd|' /C calc'!A0", "+1+1", "-2+3", "@SUM(A1)", "\tx", "\rx"])
def test_text_that_a_spreadsheet_would_run_as_a_formula_is_neutralised(text):
    assert ev.safe(text) == "'" + text


@pytest.mark.parametrize("text", ["OD123", "ORD-W-1", "hello = world", "", 5, None])
def test_ordinary_text_and_numbers_are_left_alone(text):
    assert ev.safe(text) == text


def test_negative_amounts_are_not_mistaken_for_formulas():
    assert ev.safe(ev.inr(-4600)) == "-46.00"      # our own number, kept exactly
    assert ev.safe("-46.00") == "'-46.00"          # the same characters from a partner are not


@pytest.mark.parametrize("paise,expected", [(0, "0.00"), (5, "0.05"), (149900, "1499.00"),
                                            (-4600, "-46.00"), (-5, "-0.05"), (None, "")])
def test_rupees_are_printed_exactly(paise, expected):
    assert ev.inr(paise) == expected


# ---- against the database ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def data(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("claims")
    g.Generator(path, 3000, seed=5, today=TODAY, use_xlsx=True).run()
    return path


@pytest.fixture
def world(dict_conn, data: Path):
    demo.load_dataset(dict_conn, data, echo=False)
    run_reconciliation(dict_conn, TODAY)
    return dict_conn


def partner_with_findings(conn, n=3, types=None):
    """(partner_id, [finding ids]) for a partner that has at least n open findings."""
    rows = conn.execute(
        "SELECT partner_id, array_agg(id ORDER BY id) AS ids FROM findings "
        "WHERE status = 'open' AND (%s::text[] IS NULL OR type = ANY(%s)) "
        "GROUP BY partner_id HAVING count(*) >= %s ORDER BY count(*) DESC",
        (types, types, n)).fetchall()
    assert rows, "the data set has no partner with enough findings"
    return rows[0]["partner_id"], rows[0]["ids"]


def status_of(conn, finding_id):
    return conn.execute("SELECT status, recovered_paise FROM findings WHERE id = %s",
                        (finding_id,)).fetchone()


# ---- what to claim -----------------------------------------------------------------------------


def test_the_most_urgent_findings_come_first_and_carry_their_days_left(world):
    rows = cl.candidates(world, TODAY, limit=500)
    assert rows and all(r["status"] == "open" for r in rows)
    deadlines = [r["claim_deadline"] for r in rows]
    assert deadlines == sorted(deadlines)
    for r in rows[:50]:
        assert r["days_left"] == (r["claim_deadline"] - TODAY).days
        assert r["urgency"] == cl.urgency(r["days_left"])


def test_findings_can_be_filtered_by_partner_type_and_urgency(world):
    pid, _ = partner_with_findings(world, 1)
    only = cl.candidates(world, TODAY, partner_id=pid, limit=500)
    assert only and {r["partner_id"] for r in only} == {pid}
    unpaid = cl.candidates(world, TODAY, types=["unpaid_past_due"], limit=500)
    assert unpaid and {r["type"] for r in unpaid} == {"unpaid_past_due"}
    for bucket in ("expired", "critical", "soon", "ok"):
        rows = cl.candidates(world, TODAY, bucket=bucket, limit=500)
        assert all(r["urgency"] == bucket for r in rows)


def test_the_deadline_summary_adds_up_to_all_the_open_money(world):
    summary = cl.deadline_summary(world, TODAY)
    total = world.execute("SELECT sum(amount_paise) AS s, count(*) AS n FROM findings "
                          "WHERE status = 'open'").fetchone()
    assert sum(r["amount_paise"] for r in summary) == total["s"]
    assert sum(r["findings"] for r in summary) == total["n"]
    assert {r["urgency"] for r in summary} <= {"expired", "critical", "soon", "ok"}


def test_a_later_as_of_date_means_less_time_left(world):
    first = {r["id"]: r["days_left"] for r in cl.candidates(world, TODAY, limit=50)}
    later = {r["id"]: r["days_left"] for r in
             cl.candidates(world, TODAY + timedelta(days=10), limit=5000) if r["id"] in first}
    assert later and all(later[i] == first[i] - 10 for i in later)


# ---- creating and sending ----------------------------------------------------------------------


def test_a_claim_is_made_of_open_findings_of_one_partner(world):
    pid, ids = partner_with_findings(world, 3)
    claim_id = cl.create_claim(world, pid, ids[:3], user_id=None, note="Q3 shortfalls")
    detail = cl.claim_detail(world, claim_id, TODAY)
    assert detail["status"] == "draft" and len(detail["items"]) == 3
    amounts = {i["finding_id"]: i["claimed_paise"] for i in detail["items"]}
    for fid, amount in amounts.items():
        assert world.execute("SELECT amount_paise FROM findings WHERE id = %s",
                             (fid,)).fetchone()["amount_paise"] == amount
        assert status_of(world, fid)["status"] == "open"        # a draft changes nothing yet


def test_a_claim_cannot_mix_partners_or_reuse_a_finding(world):
    pid, ids = partner_with_findings(world, 3)
    other = world.execute("SELECT id FROM findings WHERE status = 'open' AND partner_id <> %s "
                          "LIMIT 1", (pid,)).fetchone()["id"]
    with pytest.raises(cl.ClaimError, match="one partner"):
        cl.create_claim(world, pid, [ids[0], other])
    cl.create_claim(world, pid, [ids[0]])
    with pytest.raises(cl.ClaimError, match="already in claim"):
        cl.create_claim(world, pid, [ids[0], ids[1]])
    with pytest.raises(cl.ClaimError, match="at least one"):
        cl.create_claim(world, pid, [])
    with pytest.raises(cl.ClaimError, match="do not exist"):
        cl.create_claim(world, pid, [10**9])


def test_a_draft_can_be_changed_and_discarded_but_a_sent_claim_cannot(world):
    pid, ids = partner_with_findings(world, 3)
    claim_id = cl.create_claim(world, pid, ids[:2])
    cl.add_findings(world, claim_id, [ids[2]])
    cl.remove_finding(world, claim_id, ids[0])
    assert {i["finding_id"] for i in cl.claim_detail(world, claim_id, TODAY)["items"]} == \
        {ids[1], ids[2]}
    cl.submit_claim(world, claim_id, TODAY, user_id=None, partner_ref="CASE-1")
    with pytest.raises(cl.ClaimError):
        cl.add_findings(world, claim_id, [ids[0]])
    with pytest.raises(cl.ClaimError):
        cl.discard_draft(world, claim_id)
    draft = cl.create_claim(world, pid, [ids[0]])
    cl.discard_draft(world, draft)
    assert world.execute("SELECT count(*) AS n FROM claims WHERE id = %s",
                         (draft,)).fetchone()["n"] == 0


def test_sending_a_claim_marks_its_findings_claimed_and_keeps_the_history(world):
    pid, ids = partner_with_findings(world, 2)
    claim_id = cl.create_claim(world, pid, ids[:2])
    cl.submit_claim(world, claim_id, TODAY, user_id=None, partner_ref="CASE-7")
    claim = cl.claim_detail(world, claim_id, TODAY)
    assert claim["status"] == "submitted" and claim["partner_ref"] == "CASE-7"
    assert claim["submitted_on"] == TODAY
    for fid in ids[:2]:
        assert status_of(world, fid)["status"] == "claimed"
        assert [e["kind"] for e in cl.history(world, fid)] == ["claimed"]
    with pytest.raises(cl.ClaimError, match="already been sent"):
        cl.submit_claim(world, claim_id, TODAY)


def test_sending_after_the_window_closed_warns_but_is_allowed(world):
    pid, ids = partner_with_findings(world, 1)
    claim_id = cl.create_claim(world, pid, ids[:1])
    late = world.execute("SELECT claim_deadline FROM findings WHERE id = %s",
                         (ids[0],)).fetchone()["claim_deadline"] + timedelta(days=1)
    warnings = cl.submit_claim(world, claim_id, late)
    assert len(warnings) == 1 and "window closed" in warnings[0]


def test_an_empty_claim_cannot_be_sent(world):
    pid, ids = partner_with_findings(world, 1)
    claim_id = cl.create_claim(world, pid, ids[:1])
    cl.remove_finding(world, claim_id, ids[0])
    with pytest.raises(cl.ClaimError, match="empty"):
        cl.submit_claim(world, claim_id, TODAY)


# ---- outcomes ----------------------------------------------------------------------------------


@pytest.fixture
def sent(world):
    pid, ids = partner_with_findings(world, 4)
    claim_id = cl.create_claim(world, pid, ids[:4])
    cl.submit_claim(world, claim_id, TODAY)
    items = cl.claim_detail(world, claim_id, TODAY)["items"]
    return world, claim_id, items


def test_each_order_gets_its_own_outcome_and_the_claim_closes_when_all_are_decided(sent):
    world, claim_id, items = sent
    a, b, c, d = items
    cl.record_outcome(world, a["id"], "recovered", user_id=None)
    cl.record_outcome(world, b["id"], "partial", recovered_paise=b["claimed_paise"] // 2)
    cl.record_outcome(world, c["id"], "rejected", note="not our fault")
    assert world.execute("SELECT status FROM claims WHERE id = %s",
                         (claim_id,)).fetchone()["status"] == "submitted"
    cl.record_outcome(world, d["id"], "written_off")
    assert world.execute("SELECT status, closed_at FROM claims WHERE id = %s",
                         (claim_id,)).fetchone()["status"] == "closed"
    assert status_of(world, a["finding_id"]) == {"status": "recovered",
                                                  "recovered_paise": a["claimed_paise"]}
    assert status_of(world, b["finding_id"])["recovered_paise"] == b["claimed_paise"] // 2
    assert status_of(world, c["finding_id"])["status"] == "rejected"
    assert status_of(world, d["finding_id"])["status"] == "written_off"


def test_outcomes_that_make_no_sense_are_refused(sent):
    world, _, items = sent
    one = items[0]
    with pytest.raises(cl.ClaimError, match="whole claimed amount"):
        cl.record_outcome(world, one["id"], "recovered", recovered_paise=1)
    for bad in (0, one["claimed_paise"], one["claimed_paise"] + 1, None):
        with pytest.raises(cl.ClaimError, match="part payment"):
            cl.record_outcome(world, one["id"], "partial", recovered_paise=bad)
    with pytest.raises(cl.ClaimError, match="Unknown outcome"):
        cl.record_outcome(world, one["id"], "maybe")
    cl.record_outcome(world, one["id"], "rejected")
    with pytest.raises(cl.ClaimError, match="cannot be recorded"):
        cl.record_outcome(world, one["id"], "recovered")      # already decided


def test_an_outcome_cannot_be_recorded_on_a_draft(world):
    pid, ids = partner_with_findings(world, 1)
    claim_id = cl.create_claim(world, pid, ids[:1])
    item = cl.claim_detail(world, claim_id, TODAY)["items"][0]
    with pytest.raises(cl.ClaimError, match="cannot be recorded"):
        cl.record_outcome(world, item["id"], "recovered")


def test_a_wrongly_recorded_outcome_can_be_undone(sent):
    world, claim_id, items = sent
    for it in items:
        cl.record_outcome(world, it["id"], "rejected")
    assert cl.claim_detail(world, claim_id, TODAY)["status"] == "closed"
    cl.reopen_item(world, items[0]["id"], user_id=None)
    assert cl.claim_detail(world, claim_id, TODAY)["status"] == "submitted"
    assert status_of(world, items[0]["finding_id"]) == {"status": "claimed", "recovered_paise": 0}
    kinds = [e["kind"] for e in cl.history(world, items[0]["finding_id"])]
    assert kinds == ["claimed", "rejected", "outcome_undone"]
    with pytest.raises(cl.ClaimError, match="no recorded outcome"):
        cl.reopen_item(world, items[0]["id"])                 # nothing left to undo


def test_a_finding_can_be_written_off_without_a_claim_but_not_while_claimed(world):
    pid, ids = partner_with_findings(world, 2)
    cl.create_claim(world, pid, [ids[0]])
    assert cl.write_off(world, [ids[0], ids[1]], user_id=None, note="too small") == 1
    assert status_of(world, ids[1])["status"] == "written_off"
    assert status_of(world, ids[0])["status"] == "open"       # it is in a live draft


def test_a_claim_can_be_raised_again_after_the_partner_rejected_it(sent):
    world, _, items = sent
    cl.record_outcome(world, items[0]["id"], "rejected")
    world.execute("UPDATE findings SET status = 'open' WHERE id = %s", (items[0]["finding_id"],))
    world.commit()
    pid = world.execute("SELECT partner_id FROM findings WHERE id = %s",
                        (items[0]["finding_id"],)).fetchone()["partner_id"]
    assert cl.create_claim(world, pid, [items[0]["finding_id"]])


# ---- later payments and re-runs ----------------------------------------------------------------


def pay_in_full(conn, finding_id):
    """Insert the lines a partner's settlement would carry, as a later file would."""
    f = conn.execute("SELECT order_id FROM findings WHERE id = %s", (finding_id,)).fetchone()
    import_id = conn.execute("SELECT id FROM imports ORDER BY id LIMIT 1").fetchone()["id"]
    for row in explain_orders(conn, [f["order_id"]], TODAY):
        if not row["is_expected"] or row["grp"] not in ("channel", "courier_cod"):
            continue
        digest = hashlib.sha256(f"late|{finding_id}|{row['line_type']}".encode()).hexdigest()
        conn.execute(
            """INSERT INTO ledger_lines (import_id, partner_id, order_id, order_ref, line_type,
                   amount_paise, occurred_on, line_hash)
               VALUES (%s, %s, %s, 'LATE', %s, %s, %s, %s)""",
            (import_id, row["partner_id"], f["order_id"], row["line_type"],
             row["expected_paise"], TODAY, digest))
    conn.commit()


def test_when_the_money_arrives_later_the_claim_notices_and_closes(world):
    pid, ids = partner_with_findings(world, 1, types=["unpaid_past_due"])
    claim_id = cl.create_claim(world, pid, ids[:1])
    cl.submit_claim(world, claim_id, TODAY)
    pay_in_full(world, ids[0])
    run_reconciliation(world, TODAY)
    assert status_of(world, ids[0])["status"] == "resolved"
    assert cl.sync_after_run(world) == {"paid": 1, "withdrawn": 0}
    item = cl.claim_detail(world, claim_id, TODAY)["items"][0]
    assert item["outcome"] == "recovered" and item["recovered_paise"] == item["claimed_paise"]
    assert cl.claim_detail(world, claim_id, TODAY)["status"] == "closed"
    assert status_of(world, ids[0])["recovered_paise"] == item["claimed_paise"]
    assert "paid_by_partner" in [e["kind"] for e in cl.history(world, ids[0])]
    assert cl.sync_after_run(world) == {"paid": 0, "withdrawn": 0}          # nothing twice


def test_a_draft_item_is_withdrawn_when_the_money_arrives_before_it_was_sent(world):
    pid, ids = partner_with_findings(world, 1, types=["unpaid_past_due"])
    claim_id = cl.create_claim(world, pid, ids[:1])
    pay_in_full(world, ids[0])
    run_reconciliation(world, TODAY)
    assert cl.sync_after_run(world) == {"paid": 0, "withdrawn": 1}
    assert cl.claim_detail(world, claim_id, TODAY)["items"][0]["outcome"] == "withdrawn"


def test_running_the_reconciliation_again_never_undoes_a_decision(sent):
    world, _, items = sent
    cl.record_outcome(world, items[0]["id"], "recovered")
    cl.record_outcome(world, items[1]["id"], "rejected")
    cl.record_outcome(world, items[2]["id"], "written_off")
    run_reconciliation(world, TODAY)
    run_reconciliation(world, TODAY)
    assert [status_of(world, it["finding_id"])["status"] for it in items[:3]] == \
        ["recovered", "rejected", "written_off"]
    assert status_of(world, items[3]["finding_id"])["status"] == "claimed"


def test_the_claims_list_shows_totals_and_the_nearest_deadline(sent):
    world, claim_id, items = sent
    cl.record_outcome(world, items[0]["id"], "recovered")
    row = next(r for r in cl.list_claims(world, TODAY) if r["id"] == claim_id)
    assert row["items"] == 4 and row["status"] == "submitted"
    assert row["claimed_paise"] == sum(i["claimed_paise"] for i in items)
    assert row["recovered_paise"] == items[0]["claimed_paise"]
    assert row["pending_paise"] == sum(i["claimed_paise"] for i in items[1:])
    assert row["days_left"] == min(i["days_left"] for i in items[1:])
    assert cl.list_claims(world, TODAY, status="closed") == []


# ---- the evidence pack -------------------------------------------------------------------------


def read_pack(blob: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        return {n: z.read(n) for n in z.namelist()}


def rows_of(raw: bytes) -> list[dict]:
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig"))))


@pytest.fixture
def pack(world):
    pid, ids = partner_with_findings(world, 3)
    claim_id = cl.create_claim(world, pid, ids[:3])
    cl.submit_claim(world, claim_id, TODAY, partner_ref="CASE-99")
    name, blob = ev.evidence_zip(world, claim_id, TODAY)
    return world, claim_id, ids[:3], name, blob


def test_the_pack_has_every_file_and_a_sensible_name(pack):
    _, claim_id, _, name, blob = pack
    assert name.startswith(f"claim-{claim_id}-") and name.endswith(f"-{TODAY.isoformat()}.zip")
    assert set(read_pack(blob)) == {
        "README.txt", "1_summary.csv", "2_orders.csv", "3_expected_vs_received.csv",
        "4_payments_and_bills_received.csv", "5_rate_cards.csv"}


def test_the_summary_matches_the_claim_to_the_paisa(pack):
    world, claim_id, ids, _, blob = pack
    rows = rows_of(read_pack(blob)["1_summary.csv"])
    claimed = {r["order"]: int(r["claimed_paise"]) for r in rows}
    items = cl.claim_detail(world, claim_id, TODAY)["items"]
    assert sorted(claimed.values()) == sorted(i["claimed_paise"] for i in items)
    for r in rows:
        assert r["claimed_inr"] == ev.inr(int(r["claimed_paise"]))
        assert r["partner_reference"] == "CASE-99"
        assert int(r["days_left_on_" + TODAY.isoformat()]) == \
            (date.fromisoformat(r["claim_deadline"]) - TODAY).days


def test_every_line_we_hold_for_these_orders_is_listed_with_its_file_and_row(pack):
    world, _, ids, _, blob = pack
    listed = rows_of(read_pack(blob)["4_payments_and_bills_received.csv"])
    order_ids = [r["order_id"] for r in world.execute(
        "SELECT order_id FROM findings WHERE id = ANY(%s)", (ids,)).fetchall()]
    expected = world.execute("SELECT count(*) AS n FROM ledger_lines WHERE order_id = ANY(%s)",
                             (order_ids,)).fetchone()["n"]
    assert len(listed) == expected
    for r in listed:
        assert r["source_file"] and int(r["source_row"]) > 0
        assert r["amount_inr"] == ev.inr(int(r["amount_paise"]))


def test_expected_versus_received_agrees_with_the_engine(pack):
    world, _, _, _, blob = pack
    rows = rows_of(read_pack(blob)["3_expected_vs_received.csv"])
    assert rows
    for r in rows:
        assert int(r["expected_paise"]) - int(r["received_paise"]) == \
            -int(round(float(r["gap_inr"]) * 100))


def test_the_rate_cards_used_are_included_with_their_version(pack):
    _, _, _, _, blob = pack
    cards = rows_of(read_pack(blob)["5_rate_cards.csv"])
    kinds = {c["kind"] for c in cards}
    assert kinds & {"channel", "courier"}
    for c in cards:
        if c["kind"] in ("channel", "courier"):
            assert c["effective_from"] and c["card_id"]


def test_the_readme_lists_checksums_that_match_the_files(pack):
    _, _, _, _, blob = pack
    files = read_pack(blob)
    readme = files["README.txt"].decode()
    assert "CASE-99" in readme and "tolerance" in readme
    for name, raw in files.items():
        if name != "README.txt":
            assert f"{hashlib.sha256(raw).hexdigest()}  {name}" in readme


def test_the_same_claim_on_the_same_day_gives_the_same_bytes(pack):
    world, claim_id, _, _, blob = pack
    assert ev.evidence_zip(world, claim_id, TODAY)[1] == blob


def test_text_from_a_partner_cannot_turn_into_a_formula(world):
    pid, ids = partner_with_findings(world, 1)
    claim_id = cl.create_claim(world, pid, ids[:1])
    order_id = world.execute("SELECT order_id FROM findings WHERE id = %s",
                             (ids[0],)).fetchone()["order_id"]
    world.execute("UPDATE orders SET order_ref_raw = %s WHERE id = %s",
                  ("=HYPERLINK(\"http://evil\",\"x\")", order_id))
    world.execute("UPDATE ledger_lines SET reference = '@SUM(1+1)' WHERE order_id = %s",
                  (order_id,))
    world.commit()
    files = read_pack(ev.evidence_zip(world, claim_id, TODAY)[1])
    for name in ("1_summary.csv", "2_orders.csv", "4_payments_and_bills_received.csv"):
        for row in rows_of(files[name]):
            for value in row.values():
                assert not value.startswith(("=", "@")), (name, value)
    assert "'=HYPERLINK" in files["1_summary.csv"].decode("utf-8-sig")


def test_an_empty_or_missing_claim_has_no_pack(world):
    with pytest.raises(cl.ClaimError, match="No such claim"):
        ev.evidence_zip(world, 10**9, TODAY)
    pid, ids = partner_with_findings(world, 1)
    claim_id = cl.create_claim(world, pid, ids[:1])
    cl.remove_finding(world, claim_id, ids[0])
    with pytest.raises(cl.ClaimError, match="empty"):
        ev.evidence_zip(world, claim_id, TODAY)