"""Staying under the provider's tokens-per-minute limit, and keeping each call small."""

from __future__ import annotations

import json

from app.ai import rerank as rr
from app.ai.llm import LLMClient
from app.recon import matching as m


class Clock:
    """A clock the fake 'sleep' moves forward, so tests take no real time."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def make(tpm, used_tokens=None, extra=None):
    clock, sent, naps = Clock(), [], []

    def transport(url, headers, body, timeout):
        sent.append(json.loads(body))
        payload = {"choices": [{"message": {"content": '{"ok": 1}'}}]}
        if used_tokens is not None:
            payload["usage"] = {"total_tokens": used_tokens}
        return 200, json.dumps(payload)

    def sleep(seconds):
        naps.append(seconds)
        clock.sleep(seconds)

    llm = LLMClient("k", "m", "https://x.test", transport=transport, sleep=sleep, tpm_limit=tpm,
                    clock=clock, extra=extra)
    return llm, clock, sent, naps


def test_calls_that_fit_the_budget_are_not_delayed():
    llm, clock, _, naps = make(1000)
    llm.complete_json("s", "u", max_tokens=300)
    llm.complete_json("s", "u", max_tokens=300)
    assert naps == []


def test_a_call_that_would_exceed_the_budget_waits_for_the_minute_to_pass():
    llm, clock, _, naps = make(1000)
    start = clock.now
    for _ in range(3):                      # each reserves about 400; the third does not fit
        llm.complete_json("s", "u", max_tokens=400)
    assert len(naps) == 1 and 59 <= naps[0] <= 61
    assert clock.now - start >= 59


def test_what_a_call_really_used_replaces_the_estimate():
    llm, clock, _, naps = make(1000, used_tokens=50)   # replies are tiny: 50 tokens each
    for _ in range(6):
        llm.complete_json("s", "u", max_tokens=400)
    assert naps == []                       # 6 x 50 fits, though 6 x 400 would not


def test_pacing_is_announced():
    llm, _, _, _ = make(500)
    told = []
    llm.on_wait = lambda secs, why: told.append(why)
    llm.complete_json("s", "u", max_tokens=300)
    llm.complete_json("s", "u", max_tokens=300)
    assert told and "pacing" in told[0]


def test_a_budget_of_zero_means_no_pacing():
    llm, _, _, naps = make(0)
    for _ in range(20):
        llm.complete_json("s", "u", max_tokens=4000)
    assert naps == []


def test_a_single_call_bigger_than_the_whole_budget_is_still_sent():
    llm, _, sent, naps = make(100)
    llm.complete_json("s", "u", max_tokens=4000)
    assert len(sent) == 1 and naps == []


def test_one_call_can_ask_for_a_short_answer_and_no_thinking_without_changing_the_client():
    llm, _, sent, _ = make(0, extra={"reasoning_format": "hidden", "reasoning_effort": "low"})
    llm.complete_json("s", "u", max_tokens=400, extra={"reasoning_effort": "none"})
    llm.complete_json("s", "u")
    assert sent[0]["max_tokens"] == 400 and sent[0]["reasoning_effort"] == "none"
    assert sent[0]["reasoning_format"] == "hidden"
    assert sent[1]["max_tokens"] == 3000 and sent[1]["reasoning_effort"] == "low"


# ---- the review-queue questions are small ------------------------------------------------------


def test_the_reranker_asks_for_a_short_answer_with_no_thinking(dict_conn, tmp_path):
    from datetime import date

    import gen_data as g

    from app import demo
    from app.recon.engine import run_reconciliation

    g.Generator(tmp_path, 3000, seed=5, today=date(2026, 11, 2), use_xlsx=True).run()
    demo.load_dataset(dict_conn, tmp_path, echo=False)
    run_reconciliation(dict_conn, date(2026, 11, 2))
    m.refresh(dict_conn)
    llm, _, sent, _ = make(0, extra={"reasoning_format": "hidden", "reasoning_effort": "low"})
    stats = rr.rerank(dict_conn, llm, everything=True, limit=3)
    assert stats.asked == len(sent) == 3
    for body in sent:
        assert body["max_tokens"] == rr.ANSWER_TOKENS
        assert body["reasoning_effort"] == "none"
        assert len(body["messages"][1]["content"]) < 3500   # a few hundred tokens, not thousands
    plain, _, sent2, _ = make(0)                            # a model that does not reason
    rr.rerank(dict_conn, plain, everything=True, limit=1)
    assert "reasoning_effort" not in sent2[0]