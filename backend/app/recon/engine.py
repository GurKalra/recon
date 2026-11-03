"""The reconciliation engine: what should each order have paid us, and did it?

Everything is set-based SQL over temp tables, so 100,000 orders reconcile in seconds. The steps:

  r_base      each order with its weight, zone and the rate cards in force on its order date
  r_fees      the fees those cards imply for that order (commission, shipping, freight, ...)
  r_comp      EXPECTED money movements: one row per (order, group, phase, line type)
  r_recv      RECEIVED money movements grouped the same way, from ledger_lines
  r_cmp       expected vs received side by side (this is what the order drawer shows)
  r_grp       the same, summed per group; decides unpaid / pending
  r_find      findings: unpaid_past_due, short_paid, freight_overcharged, return_not_received

Groups: 'channel' is what a marketplace / quick-commerce app / payment gateway settles to us
(a website prepaid order has two phases: the sale, then the refund). 'courier_cod' is the COD
remittance, which already has the courier's freight and COD fee deducted. 'charges' is what a
courier bills us separately (freight invoices).

Every rule is a rate-card lookup or an integer sum. Nothing here is AI. `as_of` is explicit so
the result is reproducible: whether something is "past due" depends on the date you ask about.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date

from psycopg import Connection
from psycopg.types.json import Jsonb

STATUS_PRIORITY = ("return_not_received", "unpaid_past_due", "short_paid", "freight_overcharged")

_BASE = """
CREATE TEMP TABLE r_base ON COMMIT DROP AS
SELECT o.id AS order_id, o.channel_partner_id, o.courier_partner_id, o.payment_mode,
       o.value_paise, o.ordered_on, o.delivered_on, o.returned_on,
       ch.type AS channel_type, ch.payout_sla_days AS ch_sla, co.payout_sla_days AS co_sla,
       CASE WHEN o.delivery_status = 'delivered' THEN 'delivered'
            WHEN o.delivery_status = 'returned' AND o.delivered_on IS NOT NULL THEN 'returned'
            WHEN o.delivery_status = 'returned' THEN 'rto'
            WHEN o.delivery_status = 'cancelled' THEN 'cancelled'
            ELSE 'in_transit' END AS state,
       greatest(coalesce(o.weight_g, 0),
                (coalesce(o.length_cm, 0)::bigint * coalesce(o.width_cm, 0)
                 * coalesce(o.height_cm, 0) / 5)::int) AS grams,      -- L*W*H / 5000 kg, in grams
       coalesce(z.zone, 'D') AS zone,
       cc.id AS ccard_id, cc.commission_bps, cc.fixed_paise, cc.ship_extra_per_kg_paise,
       cc.tcs_bps, cc.tds_bps, cc.pg_fee_bps,
       kc.id AS kcard_id, kc.cod_flat_paise, kc.cod_bps, kc.return_pct
FROM orders o
JOIN partners ch ON ch.id = o.channel_partner_id
LEFT JOIN partners co ON co.id = o.courier_partner_id
LEFT JOIN LATERAL (
    SELECT zp.zone FROM zone_prefixes zp WHERE o.pincode LIKE zp.prefix || '%%'
    ORDER BY length(zp.prefix) DESC LIMIT 1) z ON true
LEFT JOIN LATERAL (
    SELECT c.* FROM channel_cards c
    WHERE c.partner_id = o.channel_partner_id AND c.effective_from <= o.ordered_on
    ORDER BY c.effective_from DESC LIMIT 1) cc ON true
LEFT JOIN LATERAL (
    SELECT c.* FROM courier_cards c
    WHERE c.partner_id = o.courier_partner_id AND c.effective_from <= o.ordered_on
    ORDER BY c.effective_from DESC LIMIT 1) kc ON true
WHERE {order_filter}
"""

_FEES = """
CREATE TEMP TABLE r_fees ON COMMIT DROP AS
SELECT b.*,
       recon_pct(b.value_paise, b.commission_bps) AS comm,
       recon_pct(b.value_paise, b.tcs_bps) AS tcs,
       recon_pct(b.value_paise, b.tds_bps) AS tds,
       recon_pct(b.value_paise, b.pg_fee_bps) AS pgfee,
       coalesce(
         (SELECT s.fee_paise FROM channel_ship_slabs s
          WHERE s.card_id = b.ccard_id AND b.grams <= s.max_g ORDER BY s.max_g LIMIT 1),
         (SELECT s.fee_paise + ((b.grams - s.max_g + 999) / 1000) * b.ship_extra_per_kg_paise
          FROM channel_ship_slabs s WHERE s.card_id = b.ccard_id ORDER BY s.max_g DESC LIMIT 1),
         0) AS ship,
       kz.base_paise + (greatest(1, (b.grams + 499) / 500) - 1) * kz.addl_paise AS fwd,
       (b.payment_mode = 'cod' AND b.state = 'delivered'
        AND b.channel_type = 'website') AS is_cod_delivered
FROM r_base b
LEFT JOIN courier_zone_rates kz ON kz.card_id = b.kcard_id AND kz.zone = b.zone
"""

# Expected money movements. Amounts are signed from the brand's side: money in (+), out (-).
_COMP = """
CREATE TEMP TABLE r_comp ON COMMIT DROP AS
-- marketplace / quick-commerce: a delivered order settles its sale less every fee and tax
SELECT f.order_id, 'channel'::text AS grp, 1 AS phase, t.line_type, t.amount AS expected_paise,
       f.channel_partner_id AS partner_id, f.delivered_on + f.ch_sla AS due_on
FROM r_fees f CROSS JOIN LATERAL (VALUES
        ('sale', f.value_paise), ('commission', -f.comm), ('fixed_fee', -f.fixed_paise::bigint),
        ('shipping_fee', -f.ship::bigint), ('tcs', -f.tcs), ('tds', -f.tds)) AS t(line_type, amount)
WHERE f.channel_type IN ('marketplace', 'quick_commerce') AND f.state = 'delivered'
  AND (t.amount <> 0 OR t.line_type = 'sale')
UNION ALL
-- a returned marketplace order is refunded; the shipping fee stays charged, plus a return fee
SELECT f.order_id, 'channel', 1, t.line_type, t.amount, f.channel_partner_id,
       f.returned_on + f.ch_sla
FROM r_fees f CROSS JOIN LATERAL (VALUES
        ('sale', f.value_paise), ('refund', -f.value_paise), ('shipping_fee', -f.ship::bigint),
        ('return_fee', -f.ship::bigint)) AS t(line_type, amount)
WHERE f.channel_type = 'marketplace' AND f.state = 'returned' AND t.amount <> 0
UNION ALL
-- website prepaid, phase 1: the payment gateway settles the sale less its fee
SELECT f.order_id, 'channel', 1, t.line_type, t.amount, f.channel_partner_id,
       f.ordered_on + f.ch_sla
FROM r_fees f CROSS JOIN LATERAL (VALUES
        ('sale', f.value_paise), ('pg_fee', -f.pgfee)) AS t(line_type, amount)
WHERE f.channel_type = 'website' AND f.payment_mode = 'prepaid'
  AND f.state IN ('delivered', 'returned', 'in_transit') AND t.amount <> 0
UNION ALL
-- website prepaid, phase 2: a customer return is refunded and the gateway fee comes back
SELECT f.order_id, 'channel', 2, t.line_type, t.amount, f.channel_partner_id,
       f.returned_on + f.ch_sla
FROM r_fees f CROSS JOIN LATERAL (VALUES
        ('refund', -f.value_paise), ('pg_fee', f.pgfee)) AS t(line_type, amount)
WHERE f.channel_type = 'website' AND f.payment_mode = 'prepaid' AND f.state = 'returned'
  AND t.amount <> 0
UNION ALL
-- website COD delivered: the courier remits the cash with freight and COD fee already deducted
SELECT f.order_id, 'courier_cod', 1, t.line_type, t.amount, f.courier_partner_id,
       f.delivered_on + f.co_sla
FROM r_fees f CROSS JOIN LATERAL (VALUES
        ('sale', f.value_paise), ('freight', -f.fwd::bigint),
        ('cod_fee', -greatest(f.cod_flat_paise, recon_pct(f.value_paise, f.cod_bps))::bigint))
        AS t(line_type, amount)
WHERE f.is_cod_delivered AND f.fwd IS NOT NULL
UNION ALL
-- courier freight invoices: forward freight, plus return freight when the parcel came back
SELECT f.order_id, 'charges', 0, t.line_type, t.amount, f.courier_partner_id, NULL::date
FROM r_fees f CROSS JOIN LATERAL (VALUES
        ('freight', -f.fwd::bigint),
        ('return_freight', -recon_pct(f.fwd, f.return_pct * 100))) AS t(line_type, amount)
WHERE f.channel_type = 'website' AND f.courier_partner_id IS NOT NULL AND f.fwd IS NOT NULL
  AND ((t.line_type = 'freight'
        AND ((f.payment_mode = 'prepaid' AND f.state IN ('delivered', 'returned'))
             OR (f.payment_mode = 'cod' AND f.state = 'rto')))
       OR (t.line_type = 'return_freight' AND f.state IN ('returned', 'rto')))
"""

# Received money movements, mapped into the same (group, phase, line type) keys.
_RECV = """
CREATE TEMP TABLE r_recv ON COMMIT DROP AS
SELECT l.order_id,
       CASE WHEN l.partner_id = r.channel_partner_id THEN 'channel'
            WHEN r.is_cod_delivered THEN 'courier_cod'
            ELSE 'charges' END AS grp,
       CASE WHEN l.partner_id = r.channel_partner_id THEN
                 CASE WHEN r.channel_type = 'website'
                           AND (l.line_type = 'refund'
                                OR (l.line_type = 'pg_fee' AND l.amount_paise > 0))
                      THEN 2 ELSE 1 END
            WHEN r.is_cod_delivered THEN 1
            ELSE 0 END AS phase,
       l.line_type, count(*) AS n, sum(l.amount_paise) AS amt, max(l.occurred_on) AS last_on
FROM ledger_lines l JOIN r_fees r ON r.order_id = l.order_id
WHERE l.dup_seq = 0
GROUP BY 1, 2, 3, 4
"""

_CMP = """
CREATE TEMP TABLE r_cmp ON COMMIT DROP AS
SELECT order_id, grp, phase, line_type,
       c.partner_id, c.due_on,
       coalesce(c.expected_paise, 0) AS expected_paise,
       coalesce(v.amt, 0) AS received_paise,
       coalesce(v.n, 0) AS n_received,
       (c.line_type IS NOT NULL) AS is_expected,
       v.last_on,
       coalesce(p.tolerance_paise, 1000) AS tol
FROM r_comp c
FULL JOIN r_recv v USING (order_id, grp, phase, line_type)
LEFT JOIN partners p ON p.id = c.partner_id
"""

_GRP = """
CREATE TEMP TABLE r_grp ON COMMIT DROP AS
SELECT order_id, grp, phase,
       max(partner_id) AS partner_id, max(due_on) AS due_on, max(tol) AS tol,
       sum(expected_paise) AS exp_net, sum(received_paise) AS recv_net,
       sum(n_received) AS n, max(last_on) AS last_on, bool_or(is_expected) AS has_expected
FROM r_cmp GROUP BY order_id, grp, phase
"""

_FIND = """
CREATE TEMP TABLE r_find ON COMMIT DROP AS
-- nothing at all arrived for a settlement that is already due
SELECT g.order_id, 'unpaid_past_due'::text AS type, g.partner_id, g.exp_net AS amount_paise,
       g.due_on AS since_on
FROM r_grp g
WHERE g.grp IN ('channel', 'courier_cod') AND g.has_expected AND g.n = 0 AND g.exp_net > 0
  AND g.due_on <= %(as_of)s
UNION ALL
-- a settlement arrived but paid less than the rate card says it should
SELECT g.order_id, 'short_paid', g.partner_id, g.exp_net - g.recv_net, g.last_on
FROM r_grp g
WHERE g.grp = 'channel' AND g.has_expected AND g.n > 0 AND g.recv_net - g.exp_net < -g.tol
UNION ALL
-- the courier remitted less COD cash than it collected
SELECT c.order_id, 'short_paid', c.partner_id, c.expected_paise - c.received_paise, c.last_on
FROM r_cmp c
WHERE c.grp = 'courier_cod' AND c.line_type = 'sale' AND c.is_expected AND c.n_received > 0
  AND c.received_paise - c.expected_paise < -c.tol
UNION ALL
-- the courier billed more than its rate card (freight, return freight or COD fee)
SELECT c.order_id, 'freight_overcharged', c.partner_id, c.expected_paise - c.received_paise,
       c.last_on
FROM r_cmp c
WHERE c.grp IN ('charges', 'courier_cod') AND c.is_expected
  AND c.line_type IN ('freight', 'return_freight', 'cod_fee') AND c.n_received > 0
  AND c.received_paise - c.expected_paise < -c.tol
UNION ALL
-- a return that never reached the warehouse, long after it should have
SELECT f.order_id, 'return_not_received',
       CASE WHEN f.channel_type = 'website' THEN f.courier_partner_id
            ELSE f.channel_partner_id END,
       f.value_paise, f.returned_on + %(wait)s::int
FROM r_fees f
WHERE f.state IN ('returned', 'rto') AND f.returned_on + %(wait)s::int <= %(as_of)s
  AND NOT EXISTS (SELECT 1 FROM warehouse_receipts w WHERE w.order_id = f.order_id)
"""

_STATUS = """
CREATE TEMP TABLE r_status ON COMMIT DROP AS
WITH fa AS (
    SELECT order_id,
           bool_or(type = 'return_not_received') AS rnr, bool_or(type = 'unpaid_past_due') AS unp,
           bool_or(type = 'short_paid') AS sp, bool_or(type = 'freight_overcharged') AS fo
    FROM r_find GROUP BY order_id
), pend AS (
    SELECT order_id FROM r_grp
    WHERE grp IN ('channel', 'courier_cod') AND has_expected AND n = 0 AND due_on > %(as_of)s
    UNION
    SELECT order_id FROM r_fees WHERE state = 'in_transit'
    UNION
    SELECT f.order_id FROM r_fees f
    WHERE f.state IN ('returned', 'rto') AND f.returned_on + %(wait)s::int > %(as_of)s
      AND NOT EXISTS (SELECT 1 FROM warehouse_receipts w WHERE w.order_id = f.order_id)
), tot AS (
    SELECT c.order_id,
           sum(CASE WHEN c.grp = 'charges' THEN (CASE WHEN c.n_received > 0 THEN c.expected_paise
                                                       ELSE 0 END)
                    WHEN g.n > 0 OR g.due_on <= %(as_of)s THEN c.expected_paise
                    ELSE 0 END) AS expected_paise,
           sum(c.received_paise) AS received_paise
    FROM r_cmp c JOIN r_grp g USING (order_id, grp, phase)
    GROUP BY c.order_id
), over AS (
    SELECT order_id FROM r_grp
    WHERE grp = 'channel' AND has_expected AND n > 0 AND recv_net - exp_net > tol
    GROUP BY order_id
), dup AS (
    SELECT order_id FROM ledger_lines WHERE dup_seq > 0 AND order_id IS NOT NULL
    GROUP BY order_id
)
SELECT f.order_id,
       CASE WHEN fa.rnr THEN 'return_not_received' WHEN fa.unp THEN 'unpaid_past_due'
            WHEN fa.sp THEN 'short_paid' WHEN fa.fo THEN 'freight_overcharged'
            WHEN f.state = 'cancelled' THEN 'cancelled'
            WHEN pend.order_id IS NOT NULL THEN 'pending' ELSE 'settled' END AS status,
       coalesce(tot.expected_paise, 0) AS expected_paise,
       coalesce(tot.received_paise, 0) AS received_paise,
       array_remove(ARRAY[CASE WHEN over.order_id IS NOT NULL THEN 'overpaid' END,
                          CASE WHEN dup.order_id IS NOT NULL THEN 'duplicate_line' END], NULL)
           AS flags
FROM r_fees f
LEFT JOIN fa ON fa.order_id = f.order_id
LEFT JOIN pend ON pend.order_id = f.order_id
LEFT JOIN tot ON tot.order_id = f.order_id
LEFT JOIN over ON over.order_id = f.order_id
LEFT JOIN dup ON dup.order_id = f.order_id
"""


@dataclass
class ReconResult:
    run_id: int
    as_of: date
    orders: int
    by_status: dict[str, int]
    findings_open: int
    findings_new: int
    findings_resolved: int
    money_at_risk_paise: int
    timings: dict[str, float] = field(default_factory=dict)


def _receipt_wait_days(conn: Connection) -> int:
    row = conn.execute(
        "SELECT value FROM recon_settings WHERE key = 'receipt_wait_days'").fetchone()
    return int(row["value"]) if row else 7


def _build(conn: Connection, as_of: date, order_ids: list[int] | None,
           timings: dict[str, float] | None = None) -> None:
    """Create the r_* temp tables for all orders, or only `order_ids`."""
    params = {"as_of": as_of, "wait": _receipt_wait_days(conn), "ids": order_ids}
    order_filter = "TRUE" if order_ids is None else "o.id = ANY(%(ids)s)"
    steps = [("base", _BASE.format(order_filter=order_filter)), ("fees", _FEES),
             ("expected", _COMP), ("received", _RECV), ("compare", _CMP), ("groups", _GRP),
             ("findings", _FIND), ("status", _STATUS)]
    for name, sql in steps:
        start = time.perf_counter()
        conn.execute(sql, params)
        if name in ("fees", "expected", "received", "compare", "groups"):
            table = {"fees": "r_fees", "expected": "r_comp", "received": "r_recv",
                     "compare": "r_cmp", "groups": "r_grp"}[name]
            conn.execute(f"ANALYZE {table}")  # temp tables are never auto-analyzed
        if timings is not None:
            timings[name] = round(time.perf_counter() - start, 3)


def run_reconciliation(conn: Connection, as_of: date,
                       triggered_by: int | None = None) -> ReconResult:
    """Reconcile every order as of `as_of`, rewrite order_status and update the findings."""
    timings: dict[str, float] = {}
    started = time.perf_counter()
    run_id = conn.execute(
        "INSERT INTO recon_runs (as_of, triggered_by) VALUES (%s, %s) RETURNING id",
        (as_of, triggered_by)).fetchone()["id"]
    conn.commit()  # the 'running' row must survive a rollback
    try:
        _build(conn, as_of, None, timings)
        start = time.perf_counter()
        conn.execute("DELETE FROM order_status")
        conn.execute(
            """
            INSERT INTO order_status (order_id, run_id, status, expected_paise, received_paise,
                                      variance_paise, flags)
            SELECT order_id, %s, status, expected_paise, received_paise,
                   received_paise - expected_paise, flags
            FROM r_status
            """, (run_id,))
        timings["write_status"] = round(time.perf_counter() - start, 3)

        start = time.perf_counter()
        conn.execute(
            """
            INSERT INTO findings (order_id, partner_id, type, amount_paise, since_on,
                                  claim_deadline, first_seen_run, last_seen_run)
            SELECT x.order_id, x.partner_id, x.type, x.amount_paise, x.since_on,
                   x.since_on + p.claim_window_days, %(run)s, %(run)s
            FROM (SELECT order_id, type,
                         (array_agg(partner_id ORDER BY partner_id))[1] AS partner_id,
                         sum(amount_paise) AS amount_paise, min(since_on) AS since_on
                  FROM r_find GROUP BY order_id, type) x
            JOIN partners p ON p.id = x.partner_id
            ON CONFLICT (order_id, type) DO UPDATE SET
                partner_id = EXCLUDED.partner_id, amount_paise = EXCLUDED.amount_paise,
                since_on = EXCLUDED.since_on, claim_deadline = EXCLUDED.claim_deadline,
                last_seen_run = EXCLUDED.last_seen_run, updated_at = now(),
                status = CASE WHEN findings.status = 'resolved' THEN 'open'
                              ELSE findings.status END,
                resolved_at = CASE WHEN findings.status = 'resolved' THEN NULL
                                   ELSE findings.resolved_at END
            """, {"run": run_id})
        resolved = conn.execute(
            """
            UPDATE findings SET status = 'resolved', resolved_at = now(), updated_at = now()
            WHERE status IN ('open', 'claimed') AND last_seen_run <> %s
            """, (run_id,)).rowcount
        timings["write_findings"] = round(time.perf_counter() - start, 3)

        by_status = {r["status"]: r["n"] for r in conn.execute(
            "SELECT status, count(*) AS n FROM order_status GROUP BY status").fetchall()}
        agg = conn.execute(
            """
            SELECT count(*) FILTER (WHERE status IN ('open', 'claimed')) AS open_n,
                   count(*) FILTER (WHERE first_seen_run = %(run)s) AS new_n,
                   coalesce(sum(amount_paise) FILTER (WHERE status IN ('open', 'claimed')), 0)
                       AS at_risk
            FROM findings
            """, {"run": run_id}).fetchone()
        timings["total"] = round(time.perf_counter() - started, 3)
        result = ReconResult(run_id, as_of, sum(by_status.values()), by_status, agg["open_n"],
                             agg["new_n"], resolved, int(agg["at_risk"]), timings)
        conn.execute(
            "UPDATE recon_runs SET status = 'succeeded', orders_total = %s, stats = %s, "
            "stage_timings = %s, finished_at = now() WHERE id = %s",
            (result.orders, Jsonb({"by_status": by_status, "findings_open": result.findings_open,
                                   "findings_new": result.findings_new,
                                   "findings_resolved": resolved,
                                   "money_at_risk_paise": result.money_at_risk_paise}),
             Jsonb(timings), run_id))
        conn.commit()
        return result
    except Exception as exc:
        conn.rollback()
        conn.execute("UPDATE recon_runs SET status = 'failed', error = %s, finished_at = now() "
                     "WHERE id = %s", (f"{type(exc).__name__}: {exc}", run_id))
        conn.commit()
        raise


def explain_orders(conn: Connection, order_ids: list[int], as_of: date) -> list[dict]:
    """Expected vs received, line by line, for a few orders. Read-only: powers the order drawer
    and uses exactly the same rules as the full run."""
    _build(conn, as_of, order_ids)
    rows = conn.execute(
        """
        SELECT c.order_id, c.grp, c.phase, c.line_type, c.partner_id, c.due_on,
               c.expected_paise, c.received_paise, c.n_received, c.is_expected, c.last_on
        FROM r_cmp c ORDER BY c.order_id, c.grp, c.phase, c.line_type
        """).fetchall()
    conn.rollback()  # temp tables are dropped; nothing was written
    return rows