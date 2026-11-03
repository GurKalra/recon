"""A small, strict client for an OpenAI-compatible chat API (Groq by default).

What it guarantees to callers:
  * JSON-only replies, parsed into a dict (or an exception); reasoning text is stripped
  * a hard timeout, and retries on rate limits and server errors (a rate limit waits as long
    as the provider asks)
  * optional pacing: with a tokens-per-minute budget it waits BEFORE a call that would exceed it,
    counting the tokens the provider reserves for the answer, so limits are rarely hit
  * `LLMUnavailable` when there is no key or the service is down: callers fall back to rules
  * `LLMBadOutput` when the reply is not usable JSON: callers may repair or fall back
  * if the provider's strict JSON mode rejects a reply (reasoning models sometimes think out
    loud), the JSON is salvaged from the rejected text, or the call is repeated without strict
    mode: the reply is validated here either way

The HTTP call goes through an injectable `transport`, so tests never touch the network.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from app.config import get_settings


class LLMUnavailable(Exception):
    """No key, bad key, or the service cannot be reached. Use the non-AI path."""


class LLMBadOutput(Exception):
    """The model answered, but not with usable JSON."""


@dataclass
class LLMResult:
    data: dict
    model: str
    seconds: float
    usage: dict = field(default_factory=dict)  # prompt_tokens / completion_tokens when reported


# (url, headers, body, timeout) -> (http status, response text)
Transport = Callable[[str, dict, bytes, float], tuple[int, str]]


def _urllib_transport(url: str, headers: dict, body: bytes, timeout: float) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (https only)
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise LLMUnavailable(f"could not reach the AI service: {exc}") from exc


_RETRY_IN = re.compile(r"try again in\s+([0-9hms. ]+)", re.IGNORECASE)
_MAX_WAIT_S = 65.0  # longer than a per-minute limit window: give up instead of hanging


def retry_after_seconds(text: str) -> float | None:
    """'Please try again in 7.34s' / '1m3.5s' / '250ms' -> seconds, or None."""
    m = _RETRY_IN.search(text)
    if not m:
        return None
    units = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
    total = sum(float(v) * units[u] for v, u in re.findall(r"([\d.]+)\s*(ms|s|m|h)", m.group(1)))
    return total or None


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)


def parse_json_object(text: str) -> dict:
    """The model's reply as a dict. Tolerates code fences and chatter around a single object."""
    cleaned = _FENCE.sub("", _THINK.sub("", text).strip())  # reasoning models may leak <think>
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise LLMBadOutput("the reply contained no JSON object") from None
        try:
            value = json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError as exc:
            raise LLMBadOutput(f"the reply was not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise LLMBadOutput("the reply was JSON but not an object")
    return value


class LLMClient:
    def __init__(self, api_key: str, model: str, base_url: str, timeout: float = 30.0,
                 transport: Transport | None = None, max_retries: int = 2,
                 sleep: Callable[[float], None] = time.sleep, temperature: float = 0.0,
                 max_tokens: int = 3000, extra: dict | None = None, tpm_limit: int = 0,
                 clock: Callable[[], float] = time.monotonic):
        self.api_key, self.model = api_key, model
        self.temperature, self.max_tokens = temperature, max_tokens
        self.extra = dict(extra or {})  # provider-specific request fields, e.g. reasoning_*
        self.base_url = base_url.rstrip("/")
        self.timeout, self.max_retries, self._sleep = timeout, max_retries, sleep
        # optional hook: called as on_wait(seconds, reason) before waiting out a rate limit
        self.on_wait: Callable[[float, str], None] | None = None
        self.tpm_limit, self._clock = tpm_limit, clock
        self._spent: deque[list[float]] = deque()   # [time, tokens] of calls in the last minute
        self._transport = transport or _urllib_transport

    @classmethod
    def from_settings(cls, transport: Transport | None = None) -> LLMClient:
        s = get_settings()
        extra = {}
        if s.groq_reasoning_format:
            extra["reasoning_format"] = s.groq_reasoning_format
        if s.groq_reasoning_effort:
            extra["reasoning_effort"] = s.groq_reasoning_effort
        return cls(s.groq_api_key, s.groq_model, s.groq_base_url, s.llm_timeout_s, transport,
                   max_retries=4, temperature=s.groq_temperature, max_tokens=s.groq_max_tokens,
                   extra=extra, tpm_limit=s.groq_tpm_limit)

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _pace(self, estimate: int) -> list[float]:
        """Wait, if needed, until a call of about `estimate` tokens fits in the per-minute
        budget. Returns the usage record for this call (adjusted once the reply is known)."""
        now = self._clock()
        while self._spent and self._spent[0][0] <= now - 60:
            self._spent.popleft()
        if self.tpm_limit and estimate <= self.tpm_limit:
            used = sum(t for _, t in self._spent)
            if used + estimate > self.tpm_limit:
                excess, wait = used + estimate - self.tpm_limit, 0.0
                for when, tokens in self._spent:     # oldest first: when do enough expire?
                    excess -= tokens
                    wait = max(0.0, when + 60 - now)
                    if excess <= 0:
                        break
                if self.on_wait:
                    self.on_wait(wait + 0.2, "pacing to stay under the token limit")
                self._sleep(wait + 0.2)
                now = self._clock()
                while self._spent and self._spent[0][0] <= now - 60:
                    self._spent.popleft()
        record = [now, float(estimate)]
        self._spent.append(record)
        return record

    def _post(self, body: dict) -> tuple[int, str]:
        """One request with retries on rate limits and server errors."""
        payload = json.dumps(body).encode("utf-8")
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json",
                   "Accept": "application/json",
                   "User-Agent": "recon/0.1"}  # some gateways reject the default Python agent
        for attempt in range(self.max_retries + 1):
            status, text = self._transport(f"{self.base_url}/chat/completions", headers,
                                           payload, self.timeout)
            if status in (408, 409, 425, 429) or status >= 500:
                if attempt < self.max_retries:
                    wait = retry_after_seconds(text) if status == 429 else None
                    if wait is not None and wait > _MAX_WAIT_S:
                        raise LLMUnavailable(f"rate limited for {wait:.0f}s (HTTP 429)")
                    pause = wait + 0.5 if wait is not None else 1.5 * (2 ** attempt)
                    if self.on_wait:
                        self.on_wait(pause, f"HTTP {status}")
                    self._sleep(pause)
                    continue
                raise LLMUnavailable(f"the AI service kept failing (HTTP {status}): "
                                     f"{text[:200]}")
            return status, text
        raise LLMUnavailable("the AI service kept failing")  # pragma: no cover

    def complete_json(self, system: str, user: str, max_tokens: int | None = None,
                      extra: dict | None = None) -> LLMResult:
        """`max_tokens` and `extra` override the client's defaults for this one call (a simple
        decision needs a small answer and no thinking; Groq counts the reserved answer tokens
        against the per-minute limit)."""
        if not self.available:
            raise LLMUnavailable("no API key configured (set GROQ_API_KEY)")
        cap = max_tokens or self.max_tokens
        body = {
            "model": self.model, "temperature": self.temperature, "max_tokens": cap,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            **self.extra, **(extra or {}),
        }
        record = self._pace(int((len(system) + len(user)) / 3) + cap)
        started = time.perf_counter()
        status, text = self._post(body)
        if status == 400 and "json_validate_failed" in text:
            # The provider's strict JSON mode rejected the reply. Often the rejected text still
            # holds the JSON (reasoning models think out loud first): salvage it, else ask again
            # without strict mode and validate the reply ourselves.
            salvaged = _salvage_failed_generation(text)
            if salvaged is not None:
                return LLMResult(salvaged, self.model, round(time.perf_counter() - started, 2))
            body.pop("response_format")
            status, text = self._post(body)
        if status in (401, 403):
            raise LLMUnavailable(f"the AI service refused the key (HTTP {status})")
        if status == 400:
            raise LLMBadOutput(f"the AI service rejected the request: {text[:600]}")
        if status != 200:
            raise LLMUnavailable(f"unexpected reply from the AI service (HTTP {status})")
        try:
            payload = json.loads(text)
            choice = payload["choices"][0]
            content = choice["message"]["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise LLMBadOutput(f"unexpected response shape: {exc}") from exc
        if choice.get("finish_reason") == "length":
            raise LLMBadOutput("the reply was cut off (raise GROQ_MAX_TOKENS or lower the "
                               "reasoning effort)")
        usage = payload.get("usage") or {}
        if usage.get("total_tokens"):
            record[1] = float(usage["total_tokens"])   # what the call really used
        return LLMResult(parse_json_object(content or ""), payload.get("model", self.model),
                         round(time.perf_counter() - started, 2), usage)


def _salvage_failed_generation(error_text: str) -> dict | None:
    """The JSON object inside a provider's 'failed_generation' field, if there is one."""
    try:
        failed = json.loads(error_text)["error"]["failed_generation"]
        return parse_json_object(str(failed))
    except (json.JSONDecodeError, KeyError, TypeError, LLMBadOutput):
        return None