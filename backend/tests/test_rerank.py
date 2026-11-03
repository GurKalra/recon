"""The AI re-ranker for the review queue: what it is shown, how its answer is checked and stored,
and that it can never link or reorder anything. A fake service stands in for the model."""

from __future__ import annotations

import csv
import json
from datetime import date
from pathlib import Path

import gen_data as g
import pytest

from app import demo
from app.ai import rerank as rr
from app.ai.llm import LLMClient
from app.recon import matching as m
from app.recon.engine import run_reconciliation

TODAY = date(2026, 11, 2)


# ---- pure pieces -------------------------------------------------------------------------------


@pytest.mark.parametrize("answer,n,expected", [
    ({"choice": 2, "confidence": "high", "reason": "x"}, 3, (2, "high", "x")),
    ({"choice": None, "confidence": "low", "reason": "unsure"}, 3, (None, "low", "unsure")),
    ({"choice": 2.0, "confidence": "HIGH", "reason": " a   b "}, 3, (2, "high", "a b")),
    ({"choice": 1, "confidence": "certain"}, 3, (1, "low", "")),
])
def test_a_valid_answer_is_accepted_and_tidied(answer, n, expected):
    assert rr.parse_choice(answer, n) == expected


@pytest.mark.parametrize("answer", [
    {}, {"choice": 0}, {"choice": 4}, {"choice": "2"}, {"choice": True}, {"choice": 1.5},
    {"confidence": "high"},
])
def test_an_answer_that_names_no_real_candidate_is_rejected(answer):
    with pytest.raises(ValueError):
        rr.parse_choice(answer, 3)


def test_a_very_long_reason_is_cut():
    assert len(rr.parse_choice({"choice": 1, "reason": "word " * 200}, 2)[2]) <= 240


# ---- against the database ----------------------------------------------------------------------


@pytest.fixture(scope="module")
def data(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("rerank")
    g.Generator(path, 3000, seed=5, today=TODAY, use_xlsx=True).run()
    return path


@pytest.fixture(scope="module")
def flagged(data: Path) -> set[str]:
    with (data / "truth.csv").open(encoding="utf-8", newline="") as f:
        return {r["order_ref"] for r in csv.DictReader(f) if "missing_order_id" in r["flags"]}


@pytest.fixture
def queued(dict_conn, data: Path):
    demo.load_dataset(dict_conn, data, echo=False)
    run_reconciliation(dict_conn, TODAY)
    m.refresh(dict_conn)
    return dict_conn


def fake(answer_for, record=None):
    """A model whose answer is computed from the prompt it receives."""

    def transport(url, headers, body, timeout):
        payload = json.loads(body)
        user = payload["messages"][1]["content"]
        if record is not None:
            record.append(user)
        answer = answer_for(user)
        content = answer if isinstance(answer, str) else json.dumps(answer)
        return 200, json.dumps({"model": "fake", "choices": [{"message": {"content": content}}],
                                "usage": {"total_tokens": 50}})

    return LLMClient("k", "fake", "https://x.test", transport=transport, sleep=lambda s: None)


def unsure_groups(conn):
    return [x for x in m.queue(conn, limit=1000)
            if x["suggestions"] and x["suggestions"][0]["confidence"] != "high"]


def test_only_the_payments_the_rules_are_unsure_of_are_asked(queued):
    asked = []
    llm = fake(lambda u: {"choice": 1, "confidence": "medium", "reason": "ok"}, asked)
    stats = rr.rerank(queued, llm)
    assert stats.asked == len(asked) == len(unsure_groups(queued)) > 0
    assert stats.agreed == stats.asked and stats.errors == 0 and stats.tokens == 50 * stats.asked


def test_everything_can_be_asked_when_wanted(queued):
    stats = rr.rerank(queued, fake(lambda u: {"choice": None, "reason": "no idea"}),
                      everything=True)
    assert stats.asked == len(m.queue(queued, limit=1000)) and stats.none == stats.asked


def test_the_answer_is_stored_beside_the_rules_ranking_and_nothing_else_changes(queued):
    before = {x["id"]: [(s["id"], s["rank"], float(s["score"]), s["confidence"])
                        for s in x["suggestions"]] for x in m.queue(queued, limit=1000)}
    llm = fake(lambda u: {"choice": 2, "confidence": "high", "reason": "the remark matches"})
    rr.rerank(queued, llm)
    after = m.queue(queued, limit=1000)
    for grp in after:
        assert [(s["id"], s["rank"], float(s["score"]), s["confidence"])
                for s in grp["suggestions"]] == before[grp["id"]]       # ranking untouched
        assert grp["status"] == "open"                                   # nothing linked
        if grp["ai_verdict"] == "picked":
            picks = [s for s in grp["suggestions"] if s["ai_pick"]]
            assert len(picks) == 1 and picks[0]["rank"] == 2
            assert picks[0]["ai_reason"] == "the remark matches" and grp["ai_note"]
    assert queued.execute("SELECT count(*) AS n FROM ledger_lines WHERE order_id IS NULL "
                          "AND order_ref IS NULL").fetchone()["n"] > 0


def test_a_none_answer_is_recorded_as_unsure(queued):
    rr.rerank(queued, fake(lambda u: {"choice": None, "confidence": "low", "reason": "two fit"}))
    states = {x["ai_verdict"] for x in m.queue(queued, limit=1000) if x["ai_verdict"]}
    assert states == {"none"}
    assert not any(s["ai_pick"] for x in m.queue(queued, limit=1000) for s in x["suggestions"])


def test_a_made_up_candidate_is_an_error_and_changes_no_pick(queued):
    stats = rr.rerank(queued, fake(lambda u: {"choice": 99, "reason": "trust me"}))
    assert stats.errors == stats.asked > 0 and stats.agreed == 0
    assert all(not s["ai_pick"] for x in m.queue(queued, limit=1000) for s in x["suggestions"])
    assert {x["ai_verdict"] for x in m.queue(queued, limit=1000) if x["ai_verdict"]} == {"error"}


def test_unusable_text_is_an_error_and_the_run_carries_on(queued):
    answers = iter(["not json at all"] + [{"choice": 1, "confidence": "low", "reason": "r"}] * 50)
    stats = rr.rerank(queued, fake(lambda u: next(answers)))
    assert stats.errors == 1 and stats.agreed == stats.asked - 1


def test_if_the_service_goes_down_the_run_stops_and_the_queue_is_as_the_rules_left_it(queued):
    def down(url, headers, body, timeout):
        return 503, "unavailable"

    llm = LLMClient("k", "m", "https://x.test", transport=down, max_retries=0,
                    sleep=lambda s: None)
    stats = rr.rerank(queued, llm)
    assert stats.asked == 1 and stats.errors == 1 and stats.notes
    first = next(x for x in m.queue(queued, limit=1000) if x["ai_verdict"] == "error")
    assert "AI unavailable" in first["ai_note"]


def test_without_a_key_nobody_is_asked(queued):
    stats = rr.rerank(queued, LLMClient("", "m", "https://x.test"))
    assert stats.asked == 0 and "no API key" in stats.notes[0]


def test_payments_already_answered_are_not_asked_again(queued):
    llm = fake(lambda u: {"choice": 1, "confidence": "low", "reason": "r"})
    first = rr.rerank(queued, llm)
    assert first.asked > 0
    assert rr.rerank(queued, llm).asked == 0


def test_the_model_sees_the_evidence_but_not_personal_data(queued):
    queued.execute("""UPDATE ledger_lines SET extras = coalesce(extras, '{}'::jsonb) ||
                      '{"remarks": "pay to asha@example.com ph 98765 43210"}'::jsonb
                      WHERE order_id IS NULL AND order_ref IS NULL""")
    queued.commit()
    m.refresh(queued)
    seen: list[str] = []
    rr.rerank(queued, fake(lambda u: {"choice": 1, "confidence": "low", "reason": "r"}, seen),
              everything=True)
    prompt = seen[0]
    assert "<email>" in prompt and "<phone>" in prompt
    assert "asha@example.com" not in prompt and "98765" not in prompt
    for needed in ("PAYMENT", "CANDIDATES", "EARLIER REVIEWS", "evidence",
                   "days_from_expected_payout"):
        assert needed in prompt


def test_what_reviewers_decided_before_is_part_of_the_prompt(queued):
    by_partner: dict[int, list[dict]] = {}
    for grp in unsure_groups(queued):
        by_partner.setdefault(grp["partner_id"], []).append(grp)
    pair = next((v for v in by_partner.values() if len(v) >= 2), None)
    if pair is None:
        pytest.skip("no partner has two unsure payments in this data set")
    first, second = pair[0], pair[1]
    m.accept(queued, first["suggestions"][0]["id"], None)
    seen: list[str] = []
    rr.rerank(queued, fake(lambda u: {"choice": 1, "confidence": "low", "reason": "r"}, seen),
              ids=[second["id"]], everything=True)
    assert "1 were accepted" in seen[0]