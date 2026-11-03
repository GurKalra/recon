"""Read-only numbers for the screens: headline, money at risk, tax, order list, order drawer.

Money at risk is the money we believe a partner owes and has not paid: findings that are open or
already claimed (a claim is not money in the bank). Everything here only reads.
"""

from __future__ import annotations

from datetime import date

from psycopg import Connection

from app.recon.engine import explain_orders

AGE_BUCKETS = ("0-7", "8-15", "16-30", "31-60", "60+")
AT_RISK = ("open", "claimed")
_AGE_SQL = ("CASE WHEN %(as_of)s - f.since_on <= 7 THEN '0-7' "
            "WHEN %(as_of)s - f.since_on <= 15 THEN '8-15' "
            "WHEN %(as_of)s - f.since_on <= 30 THEN '16-30' "
            "WHEN %(as_of)s - f.since_on <= 60 THEN '31-60' ELSE '60+' END")


def age_bucket(days: int) -> str:
    return ("0-7" if days <= 7 else "8-15" if days <= 15 else "16-30" if days <= 30
            else "31-60" if days <= 60 else "60+")


def headline(conn: Connection, as_of: date) -> dict:
    """The big numbers on the first screen."""
    f = conn.execute(
        """
        SELECT
          coalesce(sum(amount_paise) FILTER (WHERE status = ANY(%(risk)s)), 0) AS at_risk,
          count(*) FILTER (WHERE status = ANY(%(risk)s)) AS at_risk_count,
          coalesce(sum(amount_paise) FILTER (WHERE status = 'open'), 0) AS open_paise,
          coalesce(sum(amount_paise) FILTER (WHERE status = 'claimed'), 0) AS claimed_paise,
          coalesce(sum(amount_paise) FILTER (WHERE status = 'open'
                AND claim_deadline >= %(as_of)s AND claim_deadline - %(as_of)s <= 7), 0)
                AS expiring_7d,
          count(*) FILTER (WHERE status = 'open'
                AND claim_deadline >= %(as_of)s AND claim_deadline - %(as_of)s <= 7)
                AS expiring_7d_count,
          coalesce(sum(amount_paise) FILTER (WHERE status = 'open'
                AND claim_deadline < %(as_of)s), 0) AS expired,
          count(*) FILTER (WHERE status = 'open' AND claim_deadline < %(as_of)s) AS expired_count,
          coalesce(sum(recovered_paise), 0) AS recovered
        FROM findings""", {"risk": list(AT_RISK), "as_of": as_of}).fetchone()
    lines = conn.execute(
        "SELECT count(*) AS total, count(order_id) AS linked FROM ledger_lines").fetchone()
    orders = conn.execute(
        "SELECT count(*) AS n FROM orders").fetchone()["n"]
    last = conn.execute(
        "SELECT id, as_of, finished_at, orders_total FROM recon_runs WHERE status = 'succeeded' "
        "ORDER BY id DESC LIMIT 1").fetchone()
    review = conn.execute(
        "SELECT count(*) AS n FROM ledger_lines WHERE order_id IS NULL").fetchone()["n"]
    return {
        "as_of": as_of,
        "money_at_risk_paise": int(f["at_risk"]), "findings_at_risk": f["at_risk_count"],
        "open_paise": int(f["open_paise"]), "claimed_paise": int(f["claimed_paise"]),
        "expiring_7d_paise": int(f["expiring_7d"]), "expiring_7d_count": f["expiring_7d_count"],
        "expired_paise": int(f["expired"]), "expired_count": f["expired_count"],
        "recovered_paise": int(f["recovered"]),
        "orders": orders,
        "auto_match_pct": (round(100 * lines["linked"] / lines["total"], 1)
                           if lines["total"] else None),
        "lines_to_review": review,
        "last_run": last,
    }


def risk_matrix(conn: Connection, as_of: date) -> dict:
    """Money at risk: partner x age since the money fell due. Rows sorted by size."""
    rows = conn.execute(
        f"""
        SELECT p.code AS partner_code, p.name AS partner_name, {_AGE_SQL} AS bucket,
               count(*) AS findings, sum(f.amount_paise) AS amount_paise
        FROM findings f JOIN partners p ON p.id = f.partner_id
        WHERE f.status = ANY(%(risk)s)
        GROUP BY p.code, p.name, {_AGE_SQL}""",
        {"as_of": as_of, "risk": list(AT_RISK)}).fetchall()
    table: dict[str, dict] = {}
    for r in rows:
        t = table.setdefault(r["partner_code"], {
            "partner_code": r["partner_code"], "partner_name": r["partner_name"],
            "buckets": {b: {"findings": 0, "amount_paise": 0} for b in AGE_BUCKETS},
            "findings": 0, "amount_paise": 0})
        t["buckets"][r["bucket"]] = {"findings": r["findings"],
                                     "amount_paise": int(r["amount_paise"])}
        t["findings"] += r["findings"]
        t["amount_paise"] += int(r["amount_paise"])
    partners = sorted(table.values(), key=lambda t: -t["amount_paise"])
    totals = {b: {"findings": sum(p["buckets"][b]["findings"] for p in partners),
                  "amount_paise": sum(p["buckets"][b]["amount_paise"] for p in partners)}
              for b in AGE_BUCKETS}
    return {"as_of": as_of, "buckets": list(AGE_BUCKETS), "partners": partners, "totals": totals,
            "amount_paise": sum(p["amount_paise"] for p in partners)}


def by_type(conn: Connection) -> list[dict]:
    """Where the leaks are: money at risk by kind of problem."""
    return conn.execute(
        "SELECT type, count(*) AS findings, sum(amount_paise)::bigint AS amount_paise "
        "FROM findings WHERE status = ANY(%s) GROUP BY type ORDER BY 3 DESC",
        (list(AT_RISK),)).fetchall()


def status_counts(conn: Connection) -> dict[str, int]:
    return {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, count(*) AS n FROM order_status GROUP BY status").fetchall()}


def tax_summary(conn: Connection) -> list[dict]:
    """TCS and TDS the marketplaces held back, by partner and month. Shown as positive rupees:
    this is credit the brand can claim against its own tax."""
    rows = conn.execute(
        """
        SELECT p.code AS partner_code, p.name AS partner_name,
               to_char(date_trunc('month', l.occurred_on), 'YYYY-MM') AS month,
               -sum(l.amount_paise) FILTER (WHERE l.line_type = 'tcs') AS tcs_paise,
               -sum(l.amount_paise) FILTER (WHERE l.line_type = 'tds') AS tds_paise,
               sum(l.amount_paise) FILTER (WHERE l.line_type = 'sale') AS sales_paise
        FROM ledger_lines l JOIN partners p ON p.id = l.partner_id
        WHERE l.line_type IN ('tcs', 'tds', 'sale')
        GROUP BY p.code, p.name, date_trunc('month', l.occurred_on)
        HAVING sum(l.amount_paise) FILTER (WHERE l.line_type IN ('tcs', 'tds')) IS NOT NULL
        ORDER BY month DESC, p.code""").fetchall()
    return [{**r, "tcs_paise": int(r["tcs_paise"] or 0), "tds_paise": int(r["tds_paise"] or 0),
             "sales_paise": int(r["sales_paise"] or 0)} for r in rows]


# ---- the order list ----------------------------------------------------------------------------

_SORTS = {
    "variance": "s.variance_paise ASC, o.id",          # biggest shortfall first
    "newest": "o.ordered_on DESC, o.id",
    "oldest": "o.ordered_on, o.id",
    "value": "o.value_paise DESC, o.id",
}


def orders_list(conn: Connection, status: str | None = None, partner_id: int | None = None,
                payment_mode: str | None = None, search: str | None = None,
                date_from: date | None = None, date_to: date | None = None,
                problems_only: bool = False, sort: str = "variance",
                limit: int = 50, offset: int = 0) -> dict:
    """One page of orders with their verdict. `search` matches an order reference or AWB."""
    if sort not in _SORTS:
        raise ValueError(f"sort must be one of {sorted(_SORTS)}")
    limit = max(1, min(limit, 500))
    where = """
        WHERE (%(status)s::text IS NULL OR s.status = %(status)s)
          AND (%(partner)s::bigint IS NULL OR o.channel_partner_id = %(partner)s)
          AND (%(mode)s::text IS NULL OR o.payment_mode = %(mode)s)
          AND (%(dfrom)s::date IS NULL OR o.ordered_on >= %(dfrom)s)
          AND (%(dto)s::date IS NULL OR o.ordered_on <= %(dto)s)
          AND (%(q)s::text IS NULL OR o.order_ref LIKE %(q_ref)s OR o.awb = %(q_raw)s)
          AND (NOT %(problems)s OR s.status IN ('short_paid', 'unpaid_past_due',
                   'freight_overcharged', 'return_not_received'))"""
    q = search.strip() if search and search.strip() else None
    params = {"status": status, "partner": partner_id, "mode": payment_mode,
              "dfrom": date_from, "dto": date_to, "q": q, "problems": problems_only,
              "q_ref": None if q is None else "%" + "".join(
                  c for c in q.upper() if c.isalnum()) + "%",
              "q_raw": q, "limit": limit, "offset": offset}
    from_ = ("FROM orders o JOIN order_status s ON s.order_id = o.id "
             "JOIN partners ch ON ch.id = o.channel_partner_id "
             "LEFT JOIN partners co ON co.id = o.courier_partner_id")
    total = conn.execute(f"SELECT count(*) AS n {from_} {where}", params).fetchone()["n"]
    rows = conn.execute(
        f"""
        SELECT o.id, o.order_ref_raw AS order_ref, o.ordered_on, o.payment_mode, o.value_paise,
               o.delivery_status, ch.code AS channel, co.code AS courier, s.status,
               s.expected_paise, s.received_paise, s.variance_paise, s.flags,
               (SELECT count(*) FROM findings f WHERE f.order_id = o.id
                AND f.status = ANY(ARRAY['open','claimed'])) AS live_findings
        {from_} {where} ORDER BY {_SORTS[sort]} LIMIT %(limit)s OFFSET %(offset)s""",
        params).fetchall()
    return {"total": total, "limit": limit, "offset": offset, "rows": rows}


def order_detail(conn: Connection, order_id: int, as_of: date) -> dict | None:
    """Everything for the drawer: the order, its verdict, the expected-vs-received waterfall,
    every line we hold with its source file and row, findings and their history."""
    order = conn.execute(
        """
        SELECT o.*, ch.code AS channel, ch.name AS channel_name, co.code AS courier,
               s.status, s.expected_paise, s.received_paise, s.variance_paise, s.flags
        FROM orders o JOIN partners ch ON ch.id = o.channel_partner_id
        LEFT JOIN partners co ON co.id = o.courier_partner_id
        LEFT JOIN order_status s ON s.order_id = o.id WHERE o.id = %s""",
        (order_id,)).fetchone()
    if order is None:
        return None
    waterfall = explain_orders(conn, [order_id], as_of)
    lines = conn.execute(
        """
        SELECT l.id, p.code AS partner_code, l.line_type, l.amount_paise, l.occurred_on,
               l.reference, i.filename, l.source_row, l.dup_seq
        FROM ledger_lines l JOIN partners p ON p.id = l.partner_id
        JOIN imports i ON i.id = l.import_id
        WHERE l.order_id = %s ORDER BY l.occurred_on, l.id""", (order_id,)).fetchall()
    findings = conn.execute(
        """
        SELECT f.id, f.type, f.amount_paise, f.status, f.since_on, f.claim_deadline,
               f.recovered_paise, p.code AS partner_code,
               (f.claim_deadline - %s) AS days_left
        FROM findings f JOIN partners p ON p.id = f.partner_id
        WHERE f.order_id = %s ORDER BY f.id""", (as_of, order_id)).fetchall()
    for f in findings:
        f["history"] = conn.execute(
            "SELECT at, kind, detail FROM finding_events WHERE finding_id = %s ORDER BY at, id",
            (f["id"],)).fetchall()
    receipts = conn.execute(
        "SELECT received_on, condition FROM warehouse_receipts WHERE order_id = %s "
        "ORDER BY received_on", (order_id,)).fetchall()
    return {"order": order, "waterfall": waterfall, "lines": lines, "findings": findings,
            "receipts": receipts}