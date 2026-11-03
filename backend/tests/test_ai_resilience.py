"""Rate limits, and measuring the model on its own. No network."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import gen_data as g
import pytest

from app.ai.llm import LLMClient, LLMUnavailable, retry_after_seconds
from app.ai.rules import Hint
from app.ai.suggest import suggest_mapping
from app.ingest.catalog import SOURCES


@pytest.mark.parametrize("text,expected", [
    ("Rate limit reached. Please try again in 7.34s. Need more tokens?", 7.34),
    ("Please try again in 1m3.5s.", 63.5),
    ("Please try again in 250ms", 0.25),
    ("Please try again in 2m", 120.0),
    ("no hint here", None),
])
def test_the_wait_is_read_from_the_providers_message(text, expected):
    assert retry_after_seconds(text) == pytest.approx(expected) if expected else \
        retry_after_seconds(text) is None


def ok_reply():
    return 200, json.dumps({"choices": [{"message": {"content": '{"a": 1}'}}]})


def limited(wait: str):
    message = f"Rate limit reached. Please try again in {wait}"
    return 429, json.dumps({"error": {"message": message}})


def make(*replies):
    seq, sleeps = iter(replies), []

    def transport(url, headers, body, timeout):
        return next(seq)

    return LLMClient("k", "m", "https://x.test", transport=transport, max_retries=4,
                     sleep=sleeps.append), sleeps


def test_a_rate_limit_waits_exactly_as_long_as_the_provider_asks():
    c, sleeps = make(limited("7.34s"), limited("2s"), ok_reply())
    assert c.complete_json("s", "u").data == {"a": 1}
    assert sleeps == [pytest.approx(7.84), pytest.approx(2.5)]


def test_the_wait_is_announced_so_a_person_is_not_left_staring_at_a_silent_screen():
    c, _ = make(limited("3s"), ok_reply())
    told = []
    c.on_wait = lambda secs, why: told.append((round(secs, 1), why))
    c.complete_json("s", "u")
    assert told == [(3.5, "HTTP 429")]


def test_a_limit_that_lasts_longer_than_a_minute_is_not_waited_out():
    c, sleeps = make(limited("5m"))
    with pytest.raises(LLMUnavailable, match="rate limited for 300s"):
        c.complete_json("s", "u")
    assert sleeps == []


def test_the_error_text_survives_when_the_service_keeps_failing():
    c, _ = make(*[(503, "overloaded")] * 5)
    with pytest.raises(LLMUnavailable, match="overloaded"):
        c.complete_json("s", "u")


# ---- the model's own answer is reported even when the rules' answer is kept --------------------


@pytest.fixture(scope="module")
def data(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("res")
    g.Generator(path, 600, seed=3, today=date(2026, 11, 2), use_xlsx=True).run()
    return path


def test_the_models_answer_is_kept_for_measurement_when_the_rules_win(data):
    src = next(s for s in SOURCES if s.filename == "settlement_q1.csv")

    def transport(url, headers, body, timeout):
        reply = {"spec": src.mapping}
        return 200, json.dumps({"choices": [{"message": {"content": json.dumps(reply)}}]})

    llm = LLMClient("k", "m", "https://x.test", transport=transport)
    s = suggest_mapping(data / src.filename, Hint(source_kind="settlement",
                                                   partner_type="quick_commerce"),
                        llm=llm, force_llm=True)
    assert s.source == "rules"                      # a tie goes to the rules
    assert s.model_candidate is not None and s.model_candidate.spec == src.mapping


def test_evaluate_can_be_limited_to_some_files_and_reports_each_as_it_finishes(data):
    from app.ai import evaluate as ev
    seen = []
    rows = ev.evaluate(data, only=["q1", "m3"], on_row=seen.append)
    assert [r.file for r in rows] == ["settlement_m3.csv", "settlement_q1.csv"] == \
        [r.file for r in seen]
    assert all(r.identical for r in rows)