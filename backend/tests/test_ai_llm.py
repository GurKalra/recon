"""The LLM client and the masking rules. No network: the transport is faked."""

from __future__ import annotations

import json

import pytest

from app.ai import mask
from app.ai.llm import LLMBadOutput, LLMClient, LLMUnavailable, parse_json_object


def reply(content: str, status: int = 200) -> tuple[int, str]:
    return status, json.dumps({"model": "m", "choices": [{"message": {"content": content}}],
                               "usage": {"prompt_tokens": 5, "completion_tokens": 2}})


def client(*replies, key="k", sleeps=None):
    seq = iter(replies)
    calls = []

    def transport(url, headers, body, timeout):
        calls.append((url, headers, json.loads(body), timeout))
        return next(seq)

    c = LLMClient(key, "m", "https://example.test/v1/", 5.0, transport,
                  sleep=(sleeps.append if sleeps is not None else lambda s: None))
    return c, calls


def test_a_good_reply_is_parsed_and_the_request_is_strict():
    c, calls = client(reply('{"a": 1}'))
    result = c.complete_json("sys", "usr")
    assert result.data == {"a": 1} and result.usage["prompt_tokens"] == 5
    url, headers, body, timeout = calls[0]
    assert url == "https://example.test/v1/chat/completions"
    assert headers["Authorization"] == "Bearer k" and headers["User-Agent"] == "recon/0.1"
    assert body["temperature"] == 0 and body["response_format"] == {"type": "json_object"}
    assert "reasoning_format" not in body
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert timeout == 5.0


def test_without_a_key_nothing_is_sent():
    c, calls = client(key="")
    assert not c.available
    with pytest.raises(LLMUnavailable, match="GROQ_API_KEY"):
        c.complete_json("s", "u")
    assert calls == []


def test_rate_limits_are_retried_with_backoff_then_succeed():
    sleeps: list[float] = []
    c, calls = client((429, "slow down"), (503, "busy"), reply('{"ok": true}'), sleeps=sleeps)
    assert c.complete_json("s", "u").data == {"ok": True}
    assert len(calls) == 3 and sleeps == [1.5, 3.0]


def test_a_service_that_keeps_failing_means_unavailable():
    c, _ = client((500, "x"), (500, "x"), (500, "x"))
    with pytest.raises(LLMUnavailable, match="kept failing"):
        c.complete_json("s", "u")


@pytest.mark.parametrize("status", [401, 403])
def test_a_refused_key_is_not_retried(status):
    c, calls = client((status, "no"))
    with pytest.raises(LLMUnavailable, match="refused the key"):
        c.complete_json("s", "u")
    assert len(calls) == 1


def test_a_rejected_request_is_a_bad_output_not_an_outage():
    c, _ = client((400, "bad request"))
    with pytest.raises(LLMBadOutput):
        c.complete_json("s", "u")


def strict_json_refusal(failed: str) -> tuple[int, str]:
    return 400, json.dumps({"error": {"message": "Failed to generate JSON.",
                                      "code": "json_validate_failed",
                                      "failed_generation": failed}})


def test_json_is_salvaged_when_strict_mode_rejects_a_reply_that_contains_it():
    c, calls = client(strict_json_refusal('maybe I should think... {"a": 1} done'))
    assert c.complete_json("s", "u").data == {"a": 1}
    assert len(calls) == 1


def test_when_nothing_can_be_salvaged_the_call_is_repeated_without_strict_mode():
    c, calls = client(strict_json_refusal("maybe I should think about it"),
                      reply('{"ok": true}'))
    assert c.complete_json("s", "u").data == {"ok": True}
    assert "response_format" in calls[0][2] and "response_format" not in calls[1][2]


def test_if_both_attempts_fail_the_error_says_why():
    c, _ = client(strict_json_refusal("nothing useful"), reply("still no json"))
    with pytest.raises(LLMBadOutput, match="no JSON object"):
        c.complete_json("s", "u")


@pytest.mark.parametrize("text,expected", [
    ('{"a": 1}', {"a": 1}),
    ('```json\n{"a": 1}\n```', {"a": 1}),
    ('Sure! Here you go: {"a": {"b": 2}} Hope that helps.', {"a": {"b": 2}}),
])
def test_json_is_found_even_when_the_model_adds_chatter(text, expected):
    assert parse_json_object(text) == expected


@pytest.mark.parametrize("text", ["no json here", "[1, 2]", '{"a": ', ""])
def test_replies_that_are_not_an_object_are_rejected(text):
    with pytest.raises(LLMBadOutput):
        parse_json_object(text)


def test_an_odd_response_envelope_is_a_bad_output():
    c, _ = client((200, '{"unexpected": true}'))
    with pytest.raises(LLMBadOutput, match="response shape"):
        c.complete_json("s", "u")


# ---- masking ------------------------------------------------------------------------------------


def test_personal_columns_are_sent_as_shapes_only():
    assert mask.is_sensitive_header("Customer Name") and mask.is_sensitive_header("Mobile No")
    assert not mask.is_sensitive_header("Order Value")
    assert mask.safe_sample("Customer Name", "Asha Rao") == "Aaaa Aaa"
    assert mask.safe_sample("Order Value", "1499.00") == "1499.00"


def test_personal_data_is_masked_wherever_it_hides():
    text = "pay to asha@example.com ph 98765 43210 pan ABCDE1234F aadhaar 1234 5678 9012"
    out = mask.mask_text(text)
    for leaked in ("asha@example.com", "98765", "ABCDE1234F", "5678"):
        assert leaked not in out
    assert "<email>" in out and "<phone>" in out and "<pan>" in out and "<id-number>" in out


def test_order_numbers_and_amounts_survive_masking():
    for value in ("OD4100000000936", "ORD-W-201171", "405-1000085-2197654", "₹1,499.00"):
        assert mask.mask_text(value) == value


# ---- reasoning models ---------------------------------------------------------------------------


def test_reasoning_settings_are_sent_and_the_thinking_is_stripped():
    seq = iter([reply('<think>maybe {"x": 1}?</think>\n{"a": 2}')])
    sent = []

    def transport(url, headers, body, timeout):
        sent.append(json.loads(body))
        return next(seq)

    c = LLMClient("k", "qwen/qwen3.8-27b", "https://x.test/v1", transport=transport,
                  temperature=0.3, max_tokens=8000,
                  extra={"reasoning_format": "hidden", "reasoning_effort": "low"})
    assert c.complete_json("s", "u").data == {"a": 2}
    body = sent[0]
    assert body["temperature"] == 0.3 and body["max_tokens"] == 8000
    assert body["reasoning_format"] == "hidden" and body["reasoning_effort"] == "low"
    assert body["model"] == "qwen/qwen3.8-27b"


def test_a_reply_cut_off_by_the_token_limit_is_reported_as_such():
    cut = (200, json.dumps({"choices": [{"message": {"content": '{"a": '},
                                         "finish_reason": "length"}]}))
    c, _ = client(cut)
    with pytest.raises(LLMBadOutput, match="cut off"):
        c.complete_json("s", "u")


def test_settings_build_the_client_for_the_configured_model(monkeypatch):
    from app.config import get_settings
    for name, value in {"GROQ_API_KEY": "abc", "GROQ_MODEL": "qwen/qwen3.8-27b",
                        "GROQ_REASONING_FORMAT": "hidden", "GROQ_REASONING_EFFORT": "low",
                        "GROQ_TEMPERATURE": "0.3", "GROQ_MAX_TOKENS": "4000"}.items():
        monkeypatch.setenv(name, value)  # so a developer's own .env cannot change the test
    get_settings.cache_clear()
    try:
        c = LLMClient.from_settings()
        assert c.model == "qwen/qwen3.8-27b" and c.available
        assert c.extra == {"reasoning_format": "hidden", "reasoning_effort": "low"}
        assert c.temperature == 0.3 and c.max_tokens == 4000
        monkeypatch.setenv("GROQ_REASONING_FORMAT", "")
        monkeypatch.setenv("GROQ_REASONING_EFFORT", "")
        get_settings.cache_clear()
        assert LLMClient.from_settings().extra == {}
    finally:
        get_settings.cache_clear()