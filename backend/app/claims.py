"""Claims: turning findings into money back.

    candidates      the findings worth claiming, most urgent first, with days left on the clock
    create / submit a batch for one partner, then send it (the clock never stops for a draft)
    record_outcome  per order: recovered, part-paid, rejected, written off
    sync_after_run  when a payment arrives in a later file, the claim notices by itself
    evidence_zip    the pack a partner needs: what we expected, what arrived, from which file
                    and row, under which rate card

Every state change is checked here (not left to the caller) and recorded in finding_events.
Nothing in this module deletes a finding.
"""

from __future__ import annotations

from datetime import date

from psycopg import Connection

CRITICAL_DAYS, SOON_DAYS = 7, 14   # days left: expired < 0, critical <= 7, soon <= 14, else ok

_URGENCY_SQL = (f"CASE WHEN f.claim_deadline < %(as_of)s THEN 'expired' "
                f"WHEN f.claim_deadline - %(as_of)s <= {CRITICAL_DAYS} THEN 'critical' "
                f"WHEN f.claim_deadline - %(as_of)s <= {SOON_DAYS} THEN 'soon' ELSE 'ok' END")


class ClaimError(Exception):
    """An action that makes no sense in the claim's current state."""


def urgency(days_left: int) -> str:
    if days_left < 0:
        return "expired"
    return "critical" if days_left <= CRITICAL_DAYS else "soon" if days_left <= SOON_DAYS else "ok"


# ---- what to claim ------------------------------------------------------------------------------


def candidates(conn: Connection, as_of: date, partner_id: int | None = None,
               types: list[str] | None = None, bucket: str | None = None,
               include_claimed: bool = False, limit: int = 100, offset: int = 0) -> list[dict]:
    """Findings that can be claimed, ordered by deadline, then by amount."""
    statuses = ["open", "claimed"] if include_claimed else ["open"]
    return conn.execute(
        f"""
        SELECT f.id, f.order_id, o.order_ref_raw AS order_ref, f.partner_id,
               p.code AS partner_code, p.name AS partner_name, f.type, f.amount_paise,
               f.since_on, f.claim_deadline, f.status, f.recovered_paise,
               (f.claim_deadline - %(as_of)s) AS days_left, {_URGENCY_SQL} AS urgency,
               ci.claim_id
        FROM findings f
        JOIN orders o ON o.id = f.order_id JOIN partners p ON p.id = f.partner_id
        LEFT JOIN claim_items ci ON ci.finding_id = f.id AND ci.outcome = 'pending'
        WHERE f.status = ANY(%(statuses)s)
          AND (%(partner)s::bigint IS NULL OR f.partner_id = %(partner)s)
          AND (%(types)s::text[] IS NULL OR f.type = ANY(%(types)s))
          AND (%(bucket)s::text IS NULL OR {_URGENCY_SQL} = %(bucket)s)
        ORDER BY f.claim_deadline, f.amount_paise DESC, f.id
        LIMIT %(limit)s OFFSET %(offset)s
        """, {"as_of": as_of, "statuses": statuses, "partner": partner_id, "types": types,
              "bucket": bucket, "limit": limit, "offset": offset}).fetchall()


def deadline_summary(conn: Connection, as_of: date) -> list[dict]:
    """Open money by partner and urgency: the numbers behind 'expires in 7 days'."""
    return conn.execute(
        f"""
        SELECT p.code AS partner_code, p.name AS partner_name, {_URGENCY_SQL} AS urgency,
               count(*) AS findings, sum(f.amount_paise) AS amount_paise,
               min(f.claim_deadline) AS nearest_deadline
        FROM findings f JOIN partners p ON p.id = f.partner_id
        WHERE f.status = 'open'
        GROUP BY p.code, p.name, {_URGENCY_SQL}
        ORDER BY min(f.claim_deadline), p.code
        """, {"as_of": as_of}).fetchall()


# ---- history ------------------------------------------------------------------------------------


def _event(conn: Connection, finding_ids: list[int], kind: str, user_id: int | None,
           detail: dict | None = None) -> None:
    from psycopg.types.json import Jsonb
    conn.execute(
        "INSERT INTO finding_events (finding_id, user_id, kind, detail) "
        "SELECT x, %s, %s, %s FROM unnest(%s::bigint[]) AS x",
        (user_id, kind, Jsonb(detail or {}), finding_ids))


def history(conn: Connection, finding_id: int) -> list[dict]:
    return conn.execute(
        "SELECT at, user_id, kind, detail FROM finding_events WHERE finding_id = %s "
        "ORDER BY at, id", (finding_id,)).fetchall()


# ---- creating and sending a claim ---------------------------------------------------------------


def create_claim(conn: Connection, partner_id: int, finding_ids: list[int],
                 user_id: int | None = None, note: str | None = None) -> int:
    """A draft claim for one partner. Every finding must be open, belong to that partner and not
    already be in a live claim."""
    ids = sorted(set(finding_ids))
    if not ids:
        raise ClaimError("Pick at least one finding.")
    rows = conn.execute(
        """SELECT f.id, f.partner_id, f.status, f.amount_paise, ci.claim_id
           FROM findings f
           LEFT JOIN claim_items ci ON ci.finding_id = f.id AND ci.outcome = 'pending'
           WHERE f.id = ANY(%s) FOR UPDATE OF f""", (ids,)).fetchall()
    if len(rows) != len(ids):
        raise ClaimError("Some of those findings do not exist.")
    for r in rows:
        if r["partner_id"] != partner_id:
            raise ClaimError("A claim covers one partner: those findings belong to another.")
        if r["status"] != "open":
            raise ClaimError(f"Finding {r['id']} is {r['status']}, not open.")
        if r["claim_id"] is not None:
            raise ClaimError(f"Finding {r['id']} is already in claim {r['claim_id']}.")
    try:
        claim_id = conn.execute(
            "INSERT INTO claims (partner_id, created_by, note) VALUES (%s, %s, %s) RETURNING id",
            (partner_id, user_id, note)).fetchone()["id"]
        conn.cursor().executemany(
            "INSERT INTO claim_items (claim_id, finding_id, claimed_paise) VALUES (%s, %s, %s)",
            [(claim_id, r["id"], r["amount_paise"]) for r in rows])
    except Exception:
        conn.rollback()
        raise
    conn.commit()
    return claim_id


def _claim(conn: Connection, claim_id: int, lock: bool = True) -> dict:
    row = conn.execute(f"SELECT * FROM claims WHERE id = %s{' FOR UPDATE' if lock else ''}",
                       (claim_id,)).fetchone()
    if row is None:
        raise ClaimError("No such claim.")
    return row


def add_findings(conn: Connection, claim_id: int, finding_ids: list[int]) -> None:
    """Add to a draft claim."""
    claim = _claim(conn, claim_id)
    if claim["status"] != "draft":
        conn.rollback()
        raise ClaimError("Only a draft claim can be changed.")
    rows = conn.execute(
        """SELECT f.id, f.partner_id, f.status, f.amount_paise, ci.claim_id
           FROM findings f
           LEFT JOIN claim_items ci ON ci.finding_id = f.id AND ci.outcome = 'pending'
           WHERE f.id = ANY(%s) FOR UPDATE OF f""", (sorted(set(finding_ids)),)).fetchall()
    for r in rows:
        if r["partner_id"] != claim["partner_id"] or r["status"] != "open" or r["claim_id"]:
            conn.rollback()
            raise ClaimError(f"Finding {r['id']} cannot be added to this claim.")
    conn.cursor().executemany(
        "INSERT INTO claim_items (claim_id, finding_id, claimed_paise) VALUES (%s, %s, %s)",
        [(claim_id, r["id"], r["amount_paise"]) for r in rows])
    conn.commit()


def remove_finding(conn: Connection, claim_id: int, finding_id: int) -> None:
    """Take a finding out of a draft claim."""
    claim = _claim(conn, claim_id)
    if claim["status"] != "draft":
        conn.rollback()
        raise ClaimError("Only a draft claim can be changed.")
    conn.execute("DELETE FROM claim_items WHERE claim_id = %s AND finding_id = %s",
                 (claim_id, finding_id))
    conn.commit()


def discard_draft(conn: Connection, claim_id: int) -> None:
    claim = _claim(conn, claim_id)
    if claim["status"] != "draft":
        conn.rollback()
        raise ClaimError("Only a draft claim can be discarded.")
    conn.execute("DELETE FROM claims WHERE id = %s", (claim_id,))   # its items go with it
    conn.commit()


def submit_claim(conn: Connection, claim_id: int, as_of: date, user_id: int | None = None,
                 partner_ref: str | None = None) -> list[str]:
    """Mark the claim as sent. Returns warnings: findings whose claim window had already closed
    (the partner may refuse them, but the decision to send is the person's)."""
    claim = _claim(conn, claim_id)
    if claim["status"] != "draft":
        conn.rollback()
        raise ClaimError("This claim has already been sent.")
    items = conn.execute(
        """SELECT ci.finding_id, f.claim_deadline, f.status, o.order_ref_raw
           FROM claim_items ci JOIN findings f ON f.id = ci.finding_id
           JOIN orders o ON o.id = f.order_id
           WHERE ci.claim_id = %s AND ci.outcome = 'pending' ORDER BY ci.id""",
        (claim_id,)).fetchall()
    if not items:
        conn.rollback()
        raise ClaimError("The claim is empty.")
    gone = [i["order_ref_raw"] for i in items if i["status"] != "open"]
    if gone:
        conn.rollback()
        raise ClaimError("These are no longer open (the money may have arrived): "
                         + ", ".join(gone[:5]))
    warnings = [f"{i['order_ref_raw']}: the claim window closed on {i['claim_deadline']}"
                for i in items if i["claim_deadline"] < as_of]
    ids = [i["finding_id"] for i in items]
    conn.execute(
        "UPDATE claims SET status = 'submitted', submitted_by = %s, submitted_on = %s, "
        "submitted_at = now(), partner_ref = coalesce(%s, partner_ref) WHERE id = %s",
        (user_id, as_of, partner_ref, claim_id))
    conn.execute("UPDATE findings SET status = 'claimed', updated_at = now() "
                 "WHERE id = ANY(%s)", (ids,))
    _event(conn, ids, "claimed", user_id, {"claim_id": claim_id, "partner_ref": partner_ref})
    conn.commit()
    return warnings


# ---- what the partner decided -------------------------------------------------------------------

_OUTCOME_TO_FINDING = {"recovered": "recovered", "partial": "recovered",
                       "rejected": "rejected", "written_off": "written_off"}


def _close_if_done(conn: Connection, claim_id: int) -> None:
    pending = conn.execute("SELECT count(*) AS n FROM claim_items WHERE claim_id = %s "
                           "AND outcome = 'pending'", (claim_id,)).fetchone()["n"]
    if pending == 0:
        conn.execute("UPDATE claims SET status = 'closed', closed_at = now() "
                     "WHERE id = %s AND status = 'submitted'", (claim_id,))


def record_outcome(conn: Connection, item_id: int, outcome: str, user_id: int | None = None,
                   recovered_paise: int | None = None, note: str | None = None) -> None:
    """Record what the partner did with one order of a sent claim.

    recovered   the full amount (or `recovered_paise` if given and equal to it)
    partial     part of it: 0 < recovered_paise < claimed; the rest is accepted as lost
    rejected / written_off   nothing recovered
    """
    if outcome not in _OUTCOME_TO_FINDING:
        raise ClaimError(f"Unknown outcome '{outcome}'.")
    item = conn.execute(
        """SELECT ci.*, c.status AS claim_status FROM claim_items ci
           JOIN claims c ON c.id = ci.claim_id WHERE ci.id = %s FOR UPDATE OF ci""",
        (item_id,)).fetchone()
    if item is None:
        raise ClaimError("No such claim item.")
    if item["claim_status"] != "submitted" or item["outcome"] != "pending":
        conn.rollback()
        raise ClaimError("That order's outcome cannot be recorded now.")
    claimed = item["claimed_paise"]
    if outcome == "recovered":
        recovered = claimed if recovered_paise is None else recovered_paise
        if recovered != claimed:
            conn.rollback()
            raise ClaimError("Fully recovered means the whole claimed amount; "
                             "use 'partial' for less.")
    elif outcome == "partial":
        recovered = recovered_paise or 0
        if not 0 < recovered < claimed:
            conn.rollback()
            raise ClaimError("A part payment must be more than 0 and less than the claim.")
    else:
        recovered = 0
    conn.execute(
        "UPDATE claim_items SET outcome = %s, recovered_paise = %s, note = %s, decided_by = %s, "
        "decided_at = now() WHERE id = %s", (outcome, recovered, note, user_id, item_id))
    conn.execute(
        "UPDATE findings SET status = %s, recovered_paise = %s, updated_at = now() "
        "WHERE id = %s", (_OUTCOME_TO_FINDING[outcome], recovered, item["finding_id"]))
    _event(conn, [item["finding_id"]], outcome, user_id,
           {"claim_id": item["claim_id"], "recovered_paise": recovered, "note": note})
    _close_if_done(conn, item["claim_id"])
    conn.commit()


def reopen_item(conn: Connection, item_id: int, user_id: int | None = None) -> None:
    """Undo a recorded outcome (it was entered wrongly). The order goes back to 'claimed'."""
    item = conn.execute(
        "SELECT ci.*, c.status AS claim_status FROM claim_items ci "
        "JOIN claims c ON c.id = ci.claim_id WHERE ci.id = %s FOR UPDATE OF ci",
        (item_id,)).fetchone()
    if item is None or item["outcome"] in ("pending", "withdrawn") \
            or item["claim_status"] == "draft":
        conn.rollback()
        raise ClaimError("There is no recorded outcome to undo.")
    live = conn.execute("SELECT 1 FROM claim_items WHERE finding_id = %s AND outcome = 'pending'",
                        (item["finding_id"],)).fetchone()
    if live:
        conn.rollback()
        raise ClaimError("That order is already in another live claim.")
    conn.execute("UPDATE claim_items SET outcome = 'pending', recovered_paise = 0, "
                 "decided_by = NULL, decided_at = NULL WHERE id = %s", (item_id,))
    conn.execute("UPDATE findings SET status = 'claimed', recovered_paise = 0, "
                 "updated_at = now() WHERE id = %s", (item["finding_id"],))
    conn.execute("UPDATE claims SET status = 'submitted', closed_at = NULL WHERE id = %s",
                 (item["claim_id"],))
    _event(conn, [item["finding_id"]], "outcome_undone", user_id, {"claim_id": item["claim_id"]})
    conn.commit()


def write_off(conn: Connection, finding_ids: list[int], user_id: int | None = None,
              note: str | None = None) -> int:
    """Give up on open findings without claiming (too small, partner relationship, expired)."""
    rows = conn.execute(
        """UPDATE findings SET status = 'written_off', updated_at = now()
           WHERE id = ANY(%s) AND status = 'open'
             AND NOT EXISTS (SELECT 1 FROM claim_items ci
                             WHERE ci.finding_id = findings.id AND ci.outcome = 'pending')
           RETURNING id""", (sorted(set(finding_ids)),)).fetchall()
    ids = [r["id"] for r in rows]
    if ids:
        _event(conn, ids, "written_off", user_id, {"note": note})
    conn.commit()
    return len(ids)


def sync_after_run(conn: Connection) -> dict[str, int]:
    """Call after every reconciliation run. When the money for a claimed order arrived in a later
    file the engine marks its finding 'resolved'; the claim must notice."""
    paid = conn.execute(
        """UPDATE claim_items ci SET outcome = 'recovered', recovered_paise = ci.claimed_paise,
                  note = 'The money arrived in a later file.', decided_at = now()
           FROM claims c, findings f
           WHERE c.id = ci.claim_id AND f.id = ci.finding_id AND ci.outcome = 'pending'
             AND c.status = 'submitted' AND f.status = 'resolved'
           RETURNING ci.finding_id, ci.claimed_paise, ci.claim_id""").fetchall()
    if paid:
        conn.execute(
            "UPDATE findings f SET recovered_paise = x.amt FROM "
            "unnest(%s::bigint[], %s::bigint[]) AS x(id, amt) WHERE f.id = x.id",
            ([p["finding_id"] for p in paid], [p["claimed_paise"] for p in paid]))
        _event(conn, [p["finding_id"] for p in paid], "paid_by_partner", None,
               {"note": "payment arrived in a later file"})
    withdrawn = conn.execute(
        """UPDATE claim_items ci SET outcome = 'withdrawn',
                  note = 'No longer needed: the money arrived.', decided_at = now()
           FROM claims c, findings f
           WHERE c.id = ci.claim_id AND f.id = ci.finding_id AND ci.outcome = 'pending'
             AND c.status = 'draft' AND f.status = 'resolved'
           RETURNING ci.claim_id""").fetchall()
    for claim_id in {p["claim_id"] for p in paid}:
        _close_if_done(conn, claim_id)
    conn.commit()
    return {"paid": len(paid), "withdrawn": len(withdrawn)}


# ---- looking at claims --------------------------------------------------------------------------


def list_claims(conn: Connection, as_of: date, status: str | None = None,
                partner_id: int | None = None, limit: int = 50, offset: int = 0) -> list[dict]:
    return conn.execute(
        """
        SELECT c.id, c.status, c.partner_ref, c.note, c.submitted_on, c.created_at,
               p.code AS partner_code, p.name AS partner_name,
               count(ci.id) AS items,
               coalesce(sum(ci.claimed_paise), 0) AS claimed_paise,
               coalesce(sum(ci.recovered_paise), 0) AS recovered_paise,
               coalesce(sum(ci.claimed_paise) FILTER (WHERE ci.outcome = 'pending'), 0)
                   AS pending_paise,
               min(f.claim_deadline) FILTER (WHERE ci.outcome = 'pending') AS nearest_deadline,
               min(f.claim_deadline - %(as_of)s) FILTER (WHERE ci.outcome = 'pending')
                   AS days_left
        FROM claims c JOIN partners p ON p.id = c.partner_id
        LEFT JOIN claim_items ci ON ci.claim_id = c.id
        LEFT JOIN findings f ON f.id = ci.finding_id
        WHERE (%(status)s::text IS NULL OR c.status = %(status)s)
          AND (%(partner)s::bigint IS NULL OR c.partner_id = %(partner)s)
        GROUP BY c.id, p.code, p.name
        ORDER BY (c.status = 'closed'), min(f.claim_deadline) FILTER (WHERE ci.outcome = 'pending')
                 NULLS LAST, c.id DESC
        LIMIT %(limit)s OFFSET %(offset)s
        """, {"as_of": as_of, "status": status, "partner": partner_id, "limit": limit,
              "offset": offset}).fetchall()


def claim_detail(conn: Connection, claim_id: int, as_of: date) -> dict:
    claim = _claim(conn, claim_id, lock=False)
    claim["items"] = conn.execute(
        """
        SELECT ci.id, ci.finding_id, o.order_ref_raw AS order_ref, f.type, ci.claimed_paise,
               ci.outcome, ci.recovered_paise, ci.note, f.since_on, f.claim_deadline,
               (f.claim_deadline - %s) AS days_left
        FROM claim_items ci JOIN findings f ON f.id = ci.finding_id
        JOIN orders o ON o.id = f.order_id
        WHERE ci.claim_id = %s ORDER BY f.claim_deadline, ci.id
        """, (as_of, claim_id)).fetchall()
    return claim