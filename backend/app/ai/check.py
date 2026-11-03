"""Is the AI connection working?   python -m app.ai.check

Sends one tiny request with your GROQ_API_KEY and prints the model, the time it took and the
tokens used. Nothing from your data is sent.
"""

from __future__ import annotations

import sys

from app.ai.llm import LLMBadOutput, LLMClient, LLMUnavailable


def main() -> int:
    llm = LLMClient.from_settings()
    print(f"model: {llm.model}\nendpoint: {llm.base_url}\nextra: {llm.extra}")
    if not llm.available:
        print("No GROQ_API_KEY set. Add it to .env (see .env.example).")
        return 1
    try:
        # no small max_tokens here: a reasoning model's thinking counts towards the limit
        result = llm.complete_json(
            "Reply with one JSON object only.", 'Return {"ok": true, "word": "ping"}.')
    except (LLMUnavailable, LLMBadOutput) as exc:
        print(f"FAILED: {exc}")
        return 1
    print(f"OK in {result.seconds}s, reply {result.data}, tokens {result.usage}")
    return 0


if __name__ == "__main__":
    sys.exit(main())