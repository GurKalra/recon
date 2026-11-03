"""Ask the model which candidate order a payment belongs to, when the rules cannot say.

The rules (app/recon/matching.py) narrow 100,000 orders to a few candidates and score them. For
payments where the top candidate is not clearly ahead, this module shows the model the payment,
the candidates, the evidence the rules found, and how this partner's payments have been
reviewed before, and asks for its pick with a one-line reason.

What the model can and cannot do:
  * it can only choose among the numbered candidates or say "none"; anything else is ignored
  * its answer is stored next to the rules' ranking and shown to the reviewer; it never changes
    the ranking, never links a payment and never closes anything
  * personal data in remarks is masked before it is sent; amounts, dates and order numbers are not
  * if the service is unavailable the queue is exactly what the rules produced
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field

from psycopg import Connection

from app.ai.llm import LLMBadOutput, LLMClient, LLMUnavailable
from app.ai.mask import mask_text

SYSTEM = """You help a finance person decide which order a partner's payment belongs to. The
payment arrived without an order ID. You are given the payment, a numbered list of candidate
orders, the evidence a rules engine already found, and how earlier payments from this partner
were reviewed. Answer with ONE JSON object and nothing else:
{"choice": <candidate number, or null>, "confidence": "high" | "medium" | "low",
 "reason": "<one sentence a finance person would understand>"}

Rules:
- Choose only from the numbered candidates. Choose null if none fits or two fit equally well;
  a wrong confident answer is worse than "null".
- Every candidate already has exactly the right amount. Decide on the rest: how close the
  payment date is to the expected payout date, whether the fee lines agree, and what the
  partner's own columns say. Remarks may hold a piece of the order reference, possibly mistyped.
  A different city or SKU is evidence against a candidate.
- Earlier reviewer decisions show what usually holds for this partner.
- Use only what is written below. Do not invent facts."""


# the rules' reasons that carry real evidence (the others just repeat numbers sent anyway)
_EVIDENCE_PREFIXES = ("The remarks", "Same ", "Different ", "Another payment")

# a simple decision: a short answer and no thinking keeps each call cheap
ANSWER_TOKENS = 400


@dataclass
class RerankStats:
    asked: int = 0
    agreed: int = 0        # the model picked the rules' first candidate
    other: int = 0         # the model picked a different candidate
    none: int = 0          # the model said none / unsure
    errors: int = 0
    tokens: int = 0
    notes: list[str] = field(default_factory=list)


# ---- what the model is shown --------------------------------------------------------------------


def _history(conn: Connection, partner_id: int, limit: int = 12) -> str:
    rows = conn.execute(
        """
        SELECT s.status, (s.features->>'days_off')::int AS days_off, g.link_method
        FROM match_suggestions s JOIN match_groups g ON g.id = s.group_id
        WHERE g.partner_id = %s AND s.status IN ('accepted', 'rejected')
        ORDER BY s.decided_at DESC NULLS LAST LIMIT %s
        """, (partner_id, limit)).fetchall()
    manual = conn.execute(
        "SELECT count(*) AS n FROM match_groups WHERE partner_id = %s AND link_method = 'manual'",
        (partner_id,)).fetchone()["n"]
    if not rows and not manual:
        return "No earlier decisions for this partner."
    counts = Counter(r["status"] for r in rows)
    ok_days = [r["days_off"] for r in rows
               if r["status"] == "accepted" and r["days_off"] is not None]
    parts = [f"Of the last {len(rows)} suggestions for this partner, {counts['accepted']} were "
             f"accepted and {counts['rejected']} rejected."]
    if ok_days:
        parts.append("Accepted payments arrived between "
                     f"{min(ok_days)} and {max(ok_days)} days from the expected payout date.")
    if manual:
        parts.append(f"{manual} payment(s) had to be linked by hand.")
    return " ".join(parts)


def _payment_block(conn: Connection, group: dict) -> dict:
    lines = conn.execute(
        """SELECT l.extras FROM match_groups g
           JOIN ledger_lines l ON l.id = ANY(g.line_ids) WHERE g.id = %s AND l.dup_seq = 0
           ORDER BY l.id""", (group["id"],)).fetchall()
    extras: dict[str, str] = {}
    for line in lines:
        for k, v in (line["extras"] or {}).items():
            extras.setdefault(k, mask_text(str(v)))
    return {"partner": group["partner_name"], "paid_on": group["occurred_on"].isoformat(),
            "sale_amount": group["sale_paise"] / 100 if group["sale_paise"] else None,
            "partner_columns": extras}


def build_prompt(conn: Connection, group: dict, candidates: list[dict]) -> str:
    cand_rows = []
    for i, s in enumerate(candidates, start=1):
        feats = s["features"]
        evidence = [r for r in s["reasons"] if r.startswith(_EVIDENCE_PREFIXES)]
        cand_rows.append({
            "n": i, "order": s["order_ref_raw"],
            "days_from_expected_payout": feats.get("days_off"),
            "fee_lines_agreeing": (f"{feats.get('fee_lines_ok')}/"
                                   f"{feats.get('fee_lines_checked')}"),
            "order_columns": {k: mask_text(str(v)) for k, v in (s["order_extras"] or {}).items()},
            "rules_score": float(s["score"]), "evidence": evidence})
    return "\n\n".join([
        "PAYMENT:\n" + json.dumps(_payment_block(conn, group), ensure_ascii=False),
        "CANDIDATES (best first by the rules):\n"
        + json.dumps(cand_rows, ensure_ascii=False),
        "EARLIER REVIEWS OF THIS PARTNER: " + _history(conn, group["partner_id"]),
        "Which candidate is this payment for?"])


# ---- validating and storing the answer -----------------------------------------------------------


def parse_choice(data: dict, n: int) -> tuple[int | None, str, str]:
    """(choice 1..n or None, confidence, reason). Raises ValueError for an unusable answer."""
    if "choice" not in data:
        raise ValueError("no 'choice' in the answer")
    choice = data["choice"]
    if choice is not None:
        if isinstance(choice, bool) or not isinstance(choice, (int, float)) \
                or int(choice) != choice or not 1 <= int(choice) <= n:
            raise ValueError(f"'choice' must be 1..{n} or null, got {choice!r}")
        choice = int(choice)
    conf = str(data.get("confidence", "low")).lower()
    conf = conf if conf in ("high", "medium", "low") else "low"
    reason = " ".join(str(data.get("reason", "")).split())[:240]
    return choice, conf, reason


def _store(conn: Connection, group_id: int, candidates: list[dict], choice: int | None,
           conf: str, reason: str, model: str) -> None:
    for i, s in enumerate(candidates, start=1):
        conn.execute(
            "UPDATE match_suggestions SET ai_pick = %s, ai_confidence = %s, ai_reason = %s "
            "WHERE id = %s",
            (i == choice, conf if i == choice else None, reason if i == choice else None, s["id"]))
    conn.execute(
        "UPDATE match_groups SET ai_verdict = %s, ai_model = %s, ai_at = now(), ai_note = %s "
        "WHERE id = %s", ("picked" if choice else "none", model, reason or None, group_id))


def _store_error(conn: Connection, group_id: int, message: str) -> None:
    conn.execute("UPDATE match_groups SET ai_verdict = 'error', ai_at = now(), ai_note = %s "
                 "WHERE id = %s", (message[:240], group_id))


# ---- the run -------------------------------------------------------------------------------------


def _groups_to_ask(conn: Connection, ids: list[int] | None, everything: bool, limit: int
                   ) -> list[dict]:
    return conn.execute(
        """
        SELECT g.id, g.partner_id, p.name AS partner_name, g.reference, g.occurred_on,
               g.sale_paise
        FROM match_groups g JOIN partners p ON p.id = g.partner_id
        JOIN match_suggestions s ON s.group_id = g.id AND s.rank = 1 AND s.status = 'pending'
        WHERE g.status = 'open' AND (%(ids)s::bigint[] IS NULL OR g.id = ANY(%(ids)s))
          AND (g.ai_verdict IS NULL OR g.ai_verdict = 'error')
          AND (%(all)s OR s.confidence <> 'high')
        ORDER BY abs(coalesce(g.sale_paise, 0)) DESC, g.id LIMIT %(limit)s
        """, {"ids": ids, "all": everything, "limit": limit}).fetchall()


def _candidates(conn: Connection, group_id: int) -> list[dict]:
    return conn.execute(
        """SELECT s.id, s.order_id, o.order_ref_raw, o.ordered_on, o.extras AS order_extras,
                  s.rank, s.score, s.features, s.reasons
           FROM match_suggestions s JOIN orders o ON o.id = s.order_id
           WHERE s.group_id = %s AND s.status = 'pending' ORDER BY s.rank""",
        (group_id,)).fetchall()


def rerank(conn: Connection, llm: LLMClient, ids: list[int] | None = None,
           everything: bool = False, limit: int = 50) -> RerankStats:
    """Ask the model about the payments the rules are unsure of (or all, with `everything`).
    Safe to repeat: payments already answered are skipped."""
    stats = RerankStats()
    if not llm.available:
        stats.notes.append("no API key: the model was not asked")
        return stats
    for group in _groups_to_ask(conn, ids, everything, limit):
        cands = _candidates(conn, group["id"])
        if not cands:
            continue
        stats.asked += 1
        try:
            quick = {"reasoning_effort": "none"} if "reasoning_effort" in llm.extra else None
            result = llm.complete_json(SYSTEM, build_prompt(conn, group, cands),
                                       max_tokens=ANSWER_TOKENS, extra=quick)
            choice, conf, reason = parse_choice(result.data, len(cands))
        except LLMUnavailable as exc:
            _store_error(conn, group["id"], f"AI unavailable: {exc}")
            conn.commit()
            stats.errors += 1
            stats.notes.append(f"stopped: {exc}")
            break  # the service is down or limited: do not hammer it
        except (LLMBadOutput, ValueError) as exc:
            _store_error(conn, group["id"], f"unusable answer: {exc}")
            conn.commit()
            stats.errors += 1
            continue
        _store(conn, group["id"], cands, choice, conf, reason, result.model)
        conn.commit()
        stats.tokens += int(result.usage.get("total_tokens") or 0)
        if choice is None:
            stats.none += 1
        elif choice == 1:
            stats.agreed += 1
        else:
            stats.other += 1
    return stats