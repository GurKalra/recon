"""The review queue for payments that arrived without an order ID: counts, filtered pages, one
payment in full. The decisions themselves (accept, reject, link, undo, dismiss) live in
app/recon/matching.py; this module only reads."""

from __future__ import annotations

from psycopg import Connection

from app.ai.mask import is_sensitive_header

STATUSES = ("open", "linked", "dismissed")
CONFIDENCES = ("high", "medium", "low", "none")


def summary(conn: Connection) -> dict:
    """The numbers above the queue."""
    by_status = {s: {"payments": 0, "value_paise": 0} for s in STATUSES}
    for r in conn.execute(
            """SELECT status, count(*) AS n,
                      coalesce(sum(abs(coalesce(sale_paise, net_paise))), 0)::bigint AS v
               FROM match_groups GROUP BY status"""):
        by_status[r["status"]] = {"payments": r["n"], "value_paise": r["v"]}
    confidence = {c: 0 for c in CONFIDENCES}
    for r in conn.execute(
            """SELECT coalesce(s.confidence, 'none') AS c, count(*) AS n
               FROM match_groups g LEFT JOIN match_suggestions s
                 ON s.group_id = g.id AND s.rank = 1 AND s.status = 'pending'
               WHERE g.status = 'open' GROUP BY 1"""):
        confidence[r["c"]] = r["n"]
    ai = {"not_asked": 0, "picked": 0, "none": 0, "error": 0}
    for r in conn.execute("SELECT coalesce(ai_verdict, 'not_asked') AS v, count(*) AS n "
                          "FROM match_groups WHERE status = 'open' GROUP BY 1"):
        ai[r["v"]] = r["n"]
    # a decision made after the last finished run means order statuses are out of date
    stale = conn.execute(
        """SELECT EXISTS (
               SELECT 1 FROM match_groups g
               WHERE g.decided_at IS NOT NULL AND (g.status = 'linked' OR g.status = 'open')
                 AND g.decided_at > coalesce((SELECT max(finished_at) FROM recon_runs
                                              WHERE status = 'succeeded'), '-infinity')
           ) AS stale""").fetchone()["stale"]
    return {"by_status": by_status, "open_by_confidence": confidence, "open_by_ai": ai,
            "recon_stale": stale}


def total(conn: Connection, status: str, partner_id: int | None, confidence: str | None) -> int:
    return conn.execute(
        """SELECT count(*) AS n FROM match_groups g
           LEFT JOIN match_suggestions s
             ON s.group_id = g.id AND s.rank = 1 AND s.status = 'pending'
           WHERE g.status = %(status)s AND (%(p)s::bigint IS NULL OR g.partner_id = %(p)s)
             AND (%(c)s::text IS NULL OR coalesce(s.confidence, 'none') = %(c)s)""",
        {"status": status, "p": partner_id, "c": confidence}).fetchone()["n"]


def page(conn: Connection, status: str, partner_id: int | None, confidence: str | None,
         limit: int, offset: int) -> list[dict]:
    """Biggest payments first, each with its ranked suggestions."""
    groups = conn.execute(
        """SELECT g.id, g.partner_id, p.code AS partner_code, p.name AS partner_name,
                  g.reference, g.occurred_on, g.n_lines, g.sale_paise, g.net_paise, g.has_refund,
                  g.status, g.linked_order_id, g.link_method, g.note, g.ai_verdict, g.ai_note
           FROM match_groups g JOIN partners p ON p.id = g.partner_id
           LEFT JOIN match_suggestions top
             ON top.group_id = g.id AND top.rank = 1 AND top.status = 'pending'
           WHERE g.status = %(status)s AND (%(p)s::bigint IS NULL OR g.partner_id = %(p)s)
             AND (%(c)s::text IS NULL OR coalesce(top.confidence, 'none') = %(c)s)
           ORDER BY abs(coalesce(g.sale_paise, g.net_paise)) DESC, g.id
           LIMIT %(l)s OFFSET %(o)s""",
        {"status": status, "p": partner_id, "c": confidence, "l": limit, "o": offset}).fetchall()
    ids = [g["id"] for g in groups]
    by_group: dict[int, list[dict]] = {i: [] for i in ids}
    for s in _suggestions(conn, ids, live_only=True):
        by_group[s["group_id"]].append(s)
    for g in groups:
        g["suggestions"] = by_group[g["id"]]
    return groups


def _suggestions(conn: Connection, group_ids: list[int], live_only: bool) -> list[dict]:
    statuses = ("pending", "accepted") if live_only else (
        "pending", "accepted", "rejected", "superseded")
    return conn.execute(
        """SELECT s.id, s.group_id, s.order_id, o.order_ref_raw, o.value_paise, o.ordered_on,
                  o.delivery_status, o.payment_mode, s.rank, s.score, s.confidence, s.reasons,
                  s.status, s.ai_pick, s.ai_confidence, s.ai_reason
           FROM match_suggestions s JOIN orders o ON o.id = s.order_id
           WHERE s.group_id = ANY(%s) AND s.status = ANY(%s)
           ORDER BY s.group_id, s.rank""", (group_ids, list(statuses))).fetchall()


def detail(conn: Connection, group_id: int) -> dict | None:
    """One payment in full: its lines, every suggestion ever made for it, and the decision."""
    g = conn.execute(
        """SELECT g.id, g.partner_id, p.code AS partner_code, p.name AS partner_name,
                  g.reference, g.occurred_on, g.n_lines, g.sale_paise, g.net_paise, g.has_refund,
                  g.status, g.linked_order_id, g.link_method, g.note, g.decided_at,
                  u.full_name AS decided_by, g.ai_verdict, g.ai_note, g.ai_model, g.ai_at,
                  g.line_ids
           FROM match_groups g JOIN partners p ON p.id = g.partner_id
           LEFT JOIN users u ON u.id = g.decided_by WHERE g.id = %s""", (group_id,)).fetchone()
    if g is None:
        return None
    ids = g.pop("line_ids")
    lines = conn.execute(
        """SELECT id, line_type, amount_paise, occurred_on, reference, dup_seq, extras
           FROM ledger_lines WHERE id = ANY(%s) ORDER BY source_row NULLS LAST, id""",
        (ids,)).fetchall()
    for line in lines:
        extras = line.pop("extras") or {}
        line["extras"] = {k: v for k, v in extras.items() if not is_sensitive_header(k)}
    g["lines"] = lines
    g["suggestions"] = _suggestions(conn, [group_id], live_only=False)
    return g