"""The review queue: work out which order a payment with no order ID belongs to.

Steps
  1. detect_groups   gather the unlinked ledger lines into payments ("match groups")
  2. suggest         for each open group, find candidate orders and score them
  3. accept / reject / link_manual / undo   what a person can do about it

How a candidate is found: same partner, and the order value equals the payment's sale amount
exactly, and the payment landed within 21 days of the date the partner was due to pay, and the
order has no other payment recorded. How it is scored (0 to 1):

    0.50  the amount matches (always true for a candidate)
    0.30  how close the payment date is to the expected payout date (full marks within 3 days)
    0.20  how many of the fee lines (commission, TCS, TDS, shipping, ...) agree with the rate
          card; half marks when the payment carries no checkable fee lines

On top of that, extra columns the partner's file carried (remarks, SKU, city) adjust the score:
a piece of the order reference in the remarks is strong evidence for that order, a matching SKU
or city adds a little, a different city or SKU counts against it. With no extras nothing changes.

This is deliberately transparent: every suggestion carries the reasons and the numbers behind
it. Confidence is 'high' only when the score is high AND the runner-up is clearly worse AND no
other payment wants the same order. A person always makes the final call.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field

from psycopg import Connection
from psycopg.types.json import Jsonb

DATE_WINDOW_DAYS = 21
FULL_MARKS_DAYS = 3
W_AMOUNT, W_DATE, W_FEES = 0.50, 0.30, 0.20


# ---- scoring (pure, unit-tested) ---------------------------------------------------------------


def date_factor(days_off: int) -> float:
    a = abs(days_off)
    if a <= FULL_MARKS_DAYS:
        return 1.0
    return max(0.0, 1.0 - (a - FULL_MARKS_DAYS) / (DATE_WINDOW_DAYS - FULL_MARKS_DAYS))


def fee_factor(n_ok: int, n_checked: int) -> float:
    return 0.5 if n_checked == 0 else n_ok / n_checked


def score(days_off: int, n_ok: int, n_checked: int) -> float:
    parts = W_DATE * date_factor(days_off) + W_FEES * fee_factor(n_ok, n_checked)
    return round(W_AMOUNT + parts, 3)


def confidence(top: float, runner_up: float | None, contested: bool) -> str:
    margin = top - (runner_up if runner_up is not None else 0.0)
    if top >= 0.85 and margin >= 0.15 and not contested:
        return "high"
    return "medium" if top >= 0.65 else "low"


def _rupees(paise: int) -> str:
    return f"₹{paise / 100:,.2f}"


def reasons_for(value_paise: int, days_off: int, n_ok: int, n_checked: int) -> list[str]:
    out = [f"Payment of {_rupees(value_paise)} equals the order value exactly"]
    if days_off == 0:
        out.append("Paid on the expected payout date")
    elif days_off < 0:
        out.append(f"Paid {-days_off} day{'s' if days_off != -1 else ''} before the expected "
                   "payout date")
    else:
        out.append(f"Paid {days_off} day{'s' if days_off != 1 else ''} after the expected "
                   "payout date")
    if n_checked:
        out.append(f"{n_ok} of {n_checked} fee lines agree with the rate card")
    out.append("This order has no other payment recorded")
    return out


# ---- extra evidence from the partner's own columns (pure, unit-tested) -------------------------

_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-_/]{4,}")
_STOP = {"SETTLEMENT", "PAYMENT", "REFUND", "ORDER", "PYMT", "REFERENCE", "TRANSFER"}


@dataclass
class Evidence:
    adjust: float = 0.0
    notes: list[str] = field(default_factory=list)
    detail: dict = field(default_factory=dict)


def _near(token: str, canon: str) -> bool:
    """token appears in canon, allowing one wrong character (a mistyped reference)."""
    n = len(token)
    if n < 6 or n > len(canon):
        return False
    return any(sum(a != b for a, b in zip(token, canon[i:i + n], strict=True)) <= 1
               for i in range(len(canon) - n + 1))


def extras_evidence(group: dict[str, str], order_ref: str,
                    order_extras: dict[str, str] | None) -> Evidence:
    """How much the partner's extra columns say about whether this payment is for this order."""
    ev = Evidence()
    canon = re.sub(r"[^A-Z0-9]", "", order_ref.upper())
    best = 0.0
    for value in group.values():
        for tok in _TOKEN.findall(str(value).upper()):
            tok = re.sub(r"[^A-Z0-9]", "", tok)
            if tok in _STOP or len(tok) < 5:
                continue
            if tok in canon:
                strength = 0.30 if len(tok) >= 0.6 * len(canon) else 0.22
                if strength > best:
                    best = strength
                    ev.detail["fragment"] = tok
                    ev.notes.append(f"The remarks contain '{tok}', part of this order's "
                                    "reference")
            elif _near(tok, canon) and best < 0.12:
                best = 0.12
                ev.detail["fragment"] = tok
                ev.notes.append(f"The remarks contain '{tok}', one character off a part of "
                                "this order's reference")
    ev.adjust += best
    theirs = {k.lower(): str(v).strip().lower() for k, v in (order_extras or {}).items()}
    for kind, plus, minus in (("sku", 0.10, -0.15), ("city", 0.08, -0.15)):
        mine = next((str(v).strip().lower() for k, v in group.items() if kind in k.lower()),
                    None)
        other = next((v for k, v in theirs.items() if kind in k), None)
        if not mine or not other:
            continue
        if mine == other:
            ev.adjust += plus
            ev.detail[kind] = "match"
            ev.notes.append(f"Same {kind.upper() if kind == 'sku' else kind}: {other}")
        else:
            ev.adjust += minus
            ev.detail[kind] = "differs"
            ev.notes.append(f"Different {kind.upper() if kind == 'sku' else kind}: the "
                            f"payment says {mine}, the order says {other}")
    return ev


# ---- 1. groups ---------------------------------------------------------------------------------


@dataclass
class _Group:
    partner_id: int
    import_id: int
    first_row: int
    last_row: int
    reference: str | None
    occurred_on: object
    lines: list[dict]

    @property
    def has_sale(self) -> bool:
        return any(line["line_type"] == "sale" for line in self.lines)


def _split_into_payments(lines: list[dict]) -> list[_Group]:
    """Lines of one file, ordered by row. A new payment starts at a row that carries a sale line
    when the current payment already has one, or at a gap in the row numbers."""
    groups: list[_Group] = []
    by_row: dict[int, list[dict]] = defaultdict(list)
    for line in lines:
        by_row[line["source_row"]].append(line)
    for row in sorted(by_row):
        batch = by_row[row]
        starts_sale = any(line["line_type"] == "sale" for line in batch)
        current = groups[-1] if groups else None
        new = (current is None or row != current.last_row + 1
               or (starts_sale and current.has_sale))
        if new:
            first = batch[0]
            current = _Group(first["partner_id"], first["import_id"], row, row,
                             first["reference"], first["occurred_on"], [])
            groups.append(current)
        current.last_row = row
        current.lines.extend(batch)
    return groups


def detect_groups(conn: Connection) -> int:
    """Turn unlinked, ID-less ledger lines into match groups. Safe to repeat. Returns how many
    groups are open afterwards."""
    rows = conn.execute(
        """
        SELECT id, import_id, partner_id, source_row, line_type, amount_paise, occurred_on,
               reference, line_hash
        FROM ledger_lines
        WHERE order_id IS NULL AND order_ref IS NULL AND dup_seq = 0 AND source_row IS NOT NULL
        ORDER BY import_id, partner_id, source_row, id
        """).fetchall()
    by_file: dict[tuple[int, int], list[dict]] = defaultdict(list)
    for r in rows:
        by_file[(r["import_id"], r["partner_id"])].append(r)

    for lines in by_file.values():
        for g in _split_into_payments(lines):
            ids = [line["id"] for line in g.lines]
            hashes = [line["line_hash"] for line in g.lines]
            # verbatim duplicate copies of these lines travel with the group
            dups = conn.execute(
                """SELECT id FROM ledger_lines WHERE partner_id = %s AND order_id IS NULL
                   AND dup_seq > 0 AND line_hash = ANY(%s)""", (g.partner_id, hashes)).fetchall()
            ids += [d["id"] for d in dups]
            sale = [line["amount_paise"] for line in g.lines if line["line_type"] == "sale"]
            conn.execute(
                """
                INSERT INTO match_groups (partner_id, import_id, first_row, last_row, reference,
                                          occurred_on, line_ids, n_lines, sale_paise, net_paise,
                                          has_refund)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (import_id, first_row) DO UPDATE SET
                    last_row = EXCLUDED.last_row, line_ids = EXCLUDED.line_ids,
                    n_lines = EXCLUDED.n_lines, sale_paise = EXCLUDED.sale_paise,
                    net_paise = EXCLUDED.net_paise, has_refund = EXCLUDED.has_refund
                WHERE match_groups.status = 'open'
                """,
                (g.partner_id, g.import_id, g.first_row, g.last_row, g.reference, g.occurred_on,
                 ids, len(ids), sum(sale) if sale else None,
                 sum(line["amount_paise"] for line in g.lines),
                 any(line["line_type"] == "refund" for line in g.lines)))
    # a group whose lines got linked some other way (an orders import) is done
    conn.execute(
        """UPDATE match_groups g SET status = 'linked', decided_at = now()
           WHERE g.status = 'open' AND NOT EXISTS (
               SELECT 1 FROM ledger_lines l WHERE l.id = ANY(g.line_ids) AND l.order_id IS NULL)""")
    n = conn.execute("SELECT count(*) AS n FROM match_groups WHERE status = 'open'").fetchone()
    conn.commit()
    return n["n"]


# ---- 2. suggestions ----------------------------------------------------------------------------

_CANDIDATES = """
WITH pairs AS MATERIALIZED (   -- cheap filters first: partner, exact value, date window, unpaid
    SELECT g.id AS group_id, g.line_ids, o.id AS order_id, o.order_ref_raw, o.order_ref,
           o.extras AS order_extras, o.value_paise,
           o.ordered_on, o.channel_partner_id, ch.tolerance_paise,
           greatest(coalesce(o.weight_g, 0),
                    (coalesce(o.length_cm, 0)::bigint * coalesce(o.width_cm, 0)
                     * coalesce(o.height_cm, 0) / 5)::int) AS grams,
           (g.occurred_on - dd.d) AS days_off, dd.d AS due_on
    FROM match_groups g
    JOIN partners ch ON ch.id = g.partner_id AND ch.type <> 'courier'
    JOIN orders o ON o.channel_partner_id = g.partner_id AND o.value_paise = abs(g.sale_paise)
    CROSS JOIN LATERAL (SELECT CASE WHEN ch.type = 'website' THEN o.ordered_on
                                    WHEN g.has_refund THEN o.returned_on
                                    ELSE o.delivered_on END + ch.payout_sla_days AS d) dd
    WHERE g.status = 'open' AND g.sale_paise IS NOT NULL AND g.sale_paise <> 0
      AND dd.d IS NOT NULL AND abs(g.occurred_on - dd.d) <= %(window)s
      AND NOT EXISTS (SELECT 1 FROM ledger_lines e
                      WHERE e.order_id = o.id AND e.partner_id = o.channel_partner_id
                        AND e.line_type = 'sale' AND e.dup_seq = 0)
)
SELECT p.group_id, p.order_id, p.order_ref_raw, p.order_ref, p.order_extras, p.value_paise,
       p.days_off, p.due_on,
       fc.n_checked, fc.n_ok
FROM pairs p
LEFT JOIN LATERAL (
    SELECT c.* FROM channel_cards c
    WHERE c.partner_id = p.channel_partner_id AND c.effective_from <= p.ordered_on
    ORDER BY c.effective_from DESC LIMIT 1) cc ON true
CROSS JOIN LATERAL (
    SELECT coalesce(
        (SELECT s.fee_paise FROM channel_ship_slabs s
         WHERE s.card_id = cc.id AND p.grams <= s.max_g ORDER BY s.max_g LIMIT 1),
        (SELECT s.fee_paise + ((p.grams - s.max_g + 999) / 1000) * cc.ship_extra_per_kg_paise
         FROM channel_ship_slabs s WHERE s.card_id = cc.id ORDER BY s.max_g DESC LIMIT 1),
        0)::bigint AS fee) ship
CROSS JOIN LATERAL (
    SELECT count(*) FILTER (WHERE x.exp IS NOT NULL) AS n_checked,
           count(*) FILTER (WHERE x.exp IS NOT NULL
                            AND abs(abs(l.amount_paise) - x.exp) <= p.tolerance_paise) AS n_ok
    FROM ledger_lines l
    CROSS JOIN LATERAL (SELECT CASE l.line_type
            WHEN 'commission' THEN recon_pct(p.value_paise, cc.commission_bps)
            WHEN 'fixed_fee'  THEN cc.fixed_paise::bigint
            WHEN 'tcs'        THEN recon_pct(p.value_paise, cc.tcs_bps)
            WHEN 'tds'        THEN recon_pct(p.value_paise, cc.tds_bps)
            WHEN 'pg_fee'     THEN recon_pct(p.value_paise, cc.pg_fee_bps)
            WHEN 'shipping_fee' THEN CASE WHEN ship.fee > 0 THEN ship.fee END
            WHEN 'return_fee'   THEN CASE WHEN ship.fee > 0 THEN ship.fee END
            END AS exp) x
    WHERE l.id = ANY(p.line_ids) AND l.dup_seq = 0) fc
"""


def _group_extras(conn: Connection) -> dict[int, dict[str, str]]:
    """For every open payment, the extra columns its lines carried (first value per key)."""
    out: dict[int, dict[str, str]] = {}
    rows = conn.execute(
        """SELECT g.id AS gid, l.extras FROM match_groups g
           JOIN ledger_lines l ON l.id = ANY(g.line_ids)
           WHERE g.status = 'open' AND l.extras IS NOT NULL""").fetchall()
    for r in rows:
        bucket = out.setdefault(r["gid"], {})
        for key, value in r["extras"].items():
            bucket.setdefault(key, str(value))
    return out


def suggest(conn: Connection, top_n: int = 3) -> dict[str, int]:
    """(Re)build the pending suggestions for every open group. Rejected suggestions are
    remembered and never offered again."""
    cands = conn.execute(_CANDIDATES, {"window": DATE_WINDOW_DAYS}).fetchall()
    rejected = {(r["group_id"], r["order_id"]) for r in conn.execute(
        "SELECT group_id, order_id FROM match_suggestions WHERE status = 'rejected'").fetchall()}

    group_extras = _group_extras(conn)
    per_group: dict[int, list[dict]] = defaultdict(list)
    for c in cands:
        if (c["group_id"], c["order_id"]) in rejected:
            continue
        c["base"] = score(c["days_off"], c["n_ok"], c["n_checked"])
        ev = extras_evidence(group_extras.get(c["group_id"], {}), c["order_ref"],
                             c["order_extras"])
        c["evidence"] = ev
        c["raw"] = round(c["base"] + ev.adjust, 3)   # can exceed 1; only the ranking uses it
        c["score"] = min(1.0, max(0.0, c["raw"]))
        per_group[c["group_id"]].append(c)
    for rows in per_group.values():
        rows.sort(key=lambda c: (-c["raw"], abs(c["days_off"]), c["order_id"]))

    # an order that is the best candidate of several payments is contested
    best_for: dict[int, set[int]] = defaultdict(set)
    for gid, rows in per_group.items():
        best_for[rows[0]["order_id"]].add(gid)

    conn.execute("""DELETE FROM match_suggestions s USING match_groups g
                    WHERE s.group_id = g.id AND g.status = 'open' AND s.status = 'pending'""")
    made = 0
    for gid, rows in per_group.items():
        runner = rows[1]["raw"] if len(rows) > 1 else None
        for rank, c in enumerate(rows[:top_n], start=1):
            contested = len(best_for.get(c["order_id"], ())) > 1
            conf = confidence(c["raw"], runner if rank == 1 else None, contested) \
                if rank == 1 else ("medium" if c["score"] >= 0.65 else "low")
            why = reasons_for(c["value_paise"], c["days_off"], c["n_ok"], c["n_checked"])
            why = why[:-1] + c["evidence"].notes + why[-1:]
            if contested:
                why.append("Another payment is also best matched to this order")
            conn.execute(
                """
                INSERT INTO match_suggestions (group_id, order_id, rank, score, confidence,
                                               reasons, features)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (group_id, order_id) DO UPDATE SET
                    rank = EXCLUDED.rank, score = EXCLUDED.score,
                    confidence = EXCLUDED.confidence, reasons = EXCLUDED.reasons,
                    features = EXCLUDED.features, status = 'pending'
                WHERE match_suggestions.status IN ('pending', 'superseded')
                """,
                (gid, c["order_id"], rank, c["score"], conf, Jsonb(why),
                 Jsonb({"days_off": c["days_off"], "due_on": c["due_on"].isoformat(),
                        "fee_lines_ok": c["n_ok"], "fee_lines_checked": c["n_checked"],
                        "contested": contested, "base_score": c["base"], "raw_score": c["raw"],
                        "evidence": c["evidence"].detail})))
            made += 1
    open_groups = conn.execute(
        "SELECT count(*) AS n FROM match_groups WHERE status = 'open'").fetchone()["n"]
    conn.commit()
    return {"groups_open": open_groups, "groups_with_suggestions": len(per_group),
            "suggestions": made}


def refresh(conn: Connection) -> dict[str, int]:
    """Detect new payments and rebuild suggestions. Run after every import."""
    detect_groups(conn)
    return suggest(conn)


# ---- 3. decisions ------------------------------------------------------------------------------


class ReviewError(Exception):
    """A decision that cannot be made (already decided, order already paid, ...)."""


def _link(conn: Connection, group_id: int, order_id: int, user_id: int | None, method: str) -> int:
    g = conn.execute("SELECT * FROM match_groups WHERE id = %s FOR UPDATE", (group_id,)).fetchone()
    if g is None:
        raise ReviewError("No such payment.")
    if g["status"] != "open":
        raise ReviewError("This payment has already been decided.")
    o = conn.execute("SELECT channel_partner_id, order_ref FROM orders WHERE id = %s",
                     (order_id,)).fetchone()
    if o is None:
        raise ReviewError("No such order.")
    if o["channel_partner_id"] != g["partner_id"]:
        raise ReviewError("That order belongs to a different partner.")
    paid = conn.execute(
        """SELECT 1 FROM ledger_lines WHERE order_id = %s AND partner_id = %s
           AND line_type = 'sale' AND dup_seq = 0""", (order_id, g["partner_id"])).fetchone()
    if paid and g["sale_paise"] is not None:
        raise ReviewError("That order already has a payment recorded.")
    n = conn.execute(
        "UPDATE ledger_lines SET order_id = %s, order_ref = %s "
        "WHERE id = ANY(%s) AND order_id IS NULL",
        (order_id, o["order_ref"], g["line_ids"])).rowcount
    conn.execute(
        """UPDATE match_groups SET status = 'linked', linked_order_id = %s, link_method = %s,
                  decided_by = %s, decided_at = now() WHERE id = %s""",
        (order_id, method, user_id, group_id))
    return n


def accept(conn: Connection, suggestion_id: int, user_id: int | None = None) -> int:
    """Link the payment to the suggested order. Returns the number of lines linked. The caller
    re-runs the reconciliation so the order's status reflects the payment."""
    s = conn.execute("SELECT * FROM match_suggestions WHERE id = %s", (suggestion_id,)).fetchone()
    if s is None or s["status"] != "pending":
        raise ReviewError("That suggestion is no longer available.")
    try:
        n = _link(conn, s["group_id"], s["order_id"], user_id, "suggestion")
        conn.execute(
            "UPDATE match_suggestions SET status = 'accepted', decided_by = %s, decided_at = now() "
            "WHERE id = %s", (user_id, suggestion_id))
        conn.execute(
            "UPDATE match_suggestions SET status = 'superseded' "
            "WHERE group_id = %s AND id <> %s AND status = 'pending'",
            (s["group_id"], suggestion_id))
        # no other payment may now claim this order
        conn.execute(
            "UPDATE match_suggestions SET status = 'superseded' "
            "WHERE order_id = %s AND id <> %s AND status = 'pending'",
            (s["order_id"], suggestion_id))
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return n


def reject(conn: Connection, suggestion_id: int, user_id: int | None = None) -> None:
    """'Not this one.' It will not be suggested again for this payment."""
    n = conn.execute(
        "UPDATE match_suggestions SET status = 'rejected', decided_by = %s, decided_at = now() "
        "WHERE id = %s AND status = 'pending'", (user_id, suggestion_id)).rowcount
    if not n:
        conn.rollback()
        raise ReviewError("That suggestion is no longer available.")
    conn.commit()


def link_manual(conn: Connection, group_id: int, order_id: int,
                user_id: int | None = None, note: str | None = None) -> int:
    """The reviewer picks the order themselves (no suggestion needed)."""
    try:
        n = _link(conn, group_id, order_id, user_id, "manual")
        if note:
            conn.execute("UPDATE match_groups SET note = %s WHERE id = %s", (note, group_id))
        conn.execute(
            "UPDATE match_suggestions SET status = 'superseded' "
            "WHERE group_id = %s AND status = 'pending'", (group_id,))
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return n


def undo(conn: Connection, group_id: int, user_id: int | None = None) -> int:
    """Take a link back: the payment returns to the queue."""
    g = conn.execute("SELECT * FROM match_groups WHERE id = %s FOR UPDATE", (group_id,)).fetchone()
    if g is None or g["status"] != "linked" or g["linked_order_id"] is None:
        conn.rollback()
        raise ReviewError("Only a payment you linked can be undone.")
    n = conn.execute(
        "UPDATE ledger_lines SET order_id = NULL, order_ref = NULL "
        "WHERE id = ANY(%s) AND order_id = %s", (g["line_ids"], g["linked_order_id"])).rowcount
    conn.execute(
        """UPDATE match_groups SET status = 'open', linked_order_id = NULL, link_method = NULL,
                  decided_by = %s, decided_at = now() WHERE id = %s""", (user_id, group_id))
    conn.execute(
        "UPDATE match_suggestions SET status = 'pending' "
        "WHERE group_id = %s AND status IN ('accepted', 'superseded')", (group_id,))
    conn.commit()
    return n


def dismiss(conn: Connection, group_id: int, user_id: int | None = None,
            note: str | None = None) -> None:
    """'This payment is not ours / not an order.' Leaves the queue without being linked."""
    n = conn.execute(
        "UPDATE match_groups SET status = 'dismissed', decided_by = %s, decided_at = now(), "
        "note = %s WHERE id = %s AND status = 'open'", (user_id, note, group_id)).rowcount
    if not n:
        conn.rollback()
        raise ReviewError("This payment has already been decided.")
    conn.commit()


def queue(conn: Connection, status: str = "open", limit: int = 50, offset: int = 0) -> list[dict]:
    """The review queue, biggest payments first, each with its ranked suggestions."""
    groups = conn.execute(
        """
        SELECT g.id, g.partner_id, p.code AS partner_code, p.name AS partner_name, g.reference,
               g.occurred_on, g.n_lines, g.sale_paise, g.net_paise, g.status, g.linked_order_id,
               g.link_method, g.note, g.ai_verdict, g.ai_note
        FROM match_groups g JOIN partners p ON p.id = g.partner_id
        WHERE g.status = %s
        ORDER BY abs(coalesce(g.sale_paise, 0)) DESC, g.id
        LIMIT %s OFFSET %s
        """, (status, limit, offset)).fetchall()
    if not groups:
        return []
    by_group = defaultdict(list)
    for s in conn.execute(
            """
            SELECT s.id, s.group_id, s.order_id, o.order_ref_raw, o.value_paise, o.ordered_on,
                   s.rank, s.score, s.confidence, s.reasons, s.status, s.ai_pick, s.ai_confidence,
                   s.ai_reason
            FROM match_suggestions s JOIN orders o ON o.id = s.order_id
            WHERE s.group_id = ANY(%s) AND s.status IN ('pending', 'accepted')
            ORDER BY s.group_id, s.rank
            """, ([g["id"] for g in groups],)).fetchall():
        by_group[s["group_id"]].append(s)
    for g in groups:
        g["suggestions"] = by_group.get(g["id"], [])
    return groups