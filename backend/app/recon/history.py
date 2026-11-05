"""Is this payment's timing normal for this partner?

When a payment has only one candidate order, nothing else can tell us it is the right one. So we
look at the partner's earlier orders of a similar value that were already paid and ask how far
from the expected payout date those payments arrived. A payment that lands where 9 in 10 of them
did is unremarkable; one that lands further out than 19 in 20 is worth a second look.

Pure maths here; one query loads the history for the partners involved."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from math import ceil

from psycopg import Connection

SIMILAR = 0.25       # "similar value" = within 25% either way
MIN_SIMILAR = 20     # fewer than this and the history says nothing worth showing
UNUSUAL_BELOW = 0.05  # fewer than 1 in 20 similar orders were paid this far from the date


def _rs(paise: int) -> str:
    return f"₹{paise // 100:,}"


class PartnerHistory:
    """(order value, days from expected payout date) of one partner's earlier paid orders."""

    def __init__(self, rows: list[tuple[int, int]]) -> None:
        rows = sorted(rows)
        self.values = [v for v, _ in rows]
        self.days = [d for _, d in rows]

    def check(self, value_paise: int, days_off: int) -> dict | None:
        lo, hi = int(value_paise * (1 - SIMILAR)), int(value_paise * (1 + SIMILAR))
        a, b = bisect_left(self.values, lo), bisect_right(self.values, hi)
        n = b - a
        if n < MIN_SIMILAR:
            return None
        far = sorted(abs(d) for d in self.days[a:b])
        p90 = far[ceil(0.9 * n) - 1]
        share = sum(1 for x in far if x >= abs(days_off)) / n
        typical = share >= UNUSUAL_BELOW
        when = ("on the expected date" if days_off == 0 else
                f"{abs(days_off)} day{'s' if abs(days_off) != 1 else ''} "
                f"{'after' if days_off > 0 else 'before'} it")
        spread = ("on the expected payout date" if p90 == 0 else
                  f"within {p90} day{'s' if p90 != 1 else ''} of the expected payout date")
        text = (f"Similar orders from this partner ({_rs(lo)} to {_rs(hi)}, {n} paid so far): "
                f"9 in 10 were paid {spread}. This payment came {when}, "
                f"which is {'normal for them' if typical else 'unusual for them'}")
        return {"n": n, "p90_days": p90, "share_as_far": round(share, 3), "typical": typical,
                "text": text}


def load_history(conn: Connection, partner_ids: set[int]) -> dict[int, PartnerHistory]:
    """Earlier paid orders per partner. Same expected-date rule the matcher uses."""
    rows = conn.execute(
        """
        SELECT o.channel_partner_id AS partner_id, o.value_paise,
               (l.occurred_on - d.due) AS days_off
        FROM ledger_lines l
        JOIN orders o ON o.id = l.order_id AND o.channel_partner_id = l.partner_id
        JOIN partners p ON p.id = o.channel_partner_id
        CROSS JOIN LATERAL (SELECT CASE WHEN p.type = 'website' THEN o.ordered_on
                                        ELSE o.delivered_on END + p.payout_sla_days AS due) d
        WHERE l.line_type = 'sale' AND l.dup_seq = 0 AND l.amount_paise > 0
          AND o.channel_partner_id = ANY(%s) AND d.due IS NOT NULL
        """, (sorted(partner_ids),)).fetchall()
    by: dict[int, list[tuple[int, int]]] = {}
    for r in rows:
        by.setdefault(r["partner_id"], []).append((r["value_paise"], r["days_off"]))
    return {pid: PartnerHistory(v) for pid, v in by.items()}