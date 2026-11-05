"""Checking a lone candidate against the partner's earlier, similar, paid orders."""

from __future__ import annotations

import pytest

from app.recon.history import MIN_SIMILAR, PartnerHistory


def hist(n: int = 40, value: int = 100_000,
         spread: tuple[int, ...] = (0, 1, 1, 2)) -> PartnerHistory:
    return PartnerHistory([(value + i * 50, spread[i % len(spread)]) for i in range(n)])


def test_a_payment_with_normal_timing_is_called_normal():
    r = hist().check(100_500, 1)
    assert r and r["typical"] and r["n"] == 40 and r["p90_days"] == 2
    assert "9 in 10 were paid within 2 days" in r["text"] and "normal" in r["text"]


def test_a_payment_far_from_the_usual_is_called_unusual():
    r = hist().check(100_500, 9)
    assert r and not r["typical"] and r["share_as_far"] == 0
    assert "9 days after it" in r["text"] and "unusual" in r["text"]


def test_early_payments_say_before():
    assert "3 days before it" in hist().check(100_500, -3)["text"]


def test_too_little_history_says_nothing():
    assert hist(MIN_SIMILAR - 1).check(100_500, 1) is None


def test_only_orders_of_a_similar_value_count():
    h = PartnerHistory([(100_000, 1)] * 30 + [(900_000, 12)] * 30)
    assert h.check(100_000, 1)["n"] == 30
    assert h.check(900_000, 12)["typical"]
    assert h.check(400_000, 1) is None   # nothing of a similar value


@pytest.mark.parametrize("days", [0])
def test_on_the_date_is_worded_plainly(days):
    h = PartnerHistory([(100_000, 0)] * 30)
    r = h.check(100_000, days)
    assert "on the expected payout date" in r["text"] and "on the expected date" in r["text"]