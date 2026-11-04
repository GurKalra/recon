"""Read-only screens: the dashboard, the orders table and drawer, the audit trail."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from app import audit, dashboard
from app.api.deps import Anyone, AsOf, Cofounder, Conn

router = APIRouter(prefix="/api", tags=["data"])


@router.get("/dashboard")
def get_dashboard(conn: Conn, _: Anyone, as_of: AsOf) -> dict:
    """Everything on the money-at-risk screen in one call."""
    return {"headline": dashboard.headline(conn, as_of),
            "matrix": dashboard.risk_matrix(conn, as_of),
            "by_type": dashboard.by_type(conn),
            "order_status": dashboard.status_counts(conn)}


@router.get("/tax")
def get_tax(conn: Conn, _: Anyone) -> list[dict]:
    return dashboard.tax_summary(conn)


@router.get("/partners")
def get_partners(conn: Conn, _: Anyone) -> list[dict]:
    return conn.execute(
        "SELECT id, code, name, type, payout_sla_days, claim_window_days, tolerance_paise "
        "FROM partners WHERE is_active ORDER BY type, code").fetchall()


@router.get("/orders")
def get_orders(
    conn: Conn, _: Anyone,
    status: str | None = None, partner_id: int | None = None,
    payment_mode: Annotated[str | None, Query(pattern="^(prepaid|cod)$")] = None,
    q: Annotated[str | None, Query(max_length=60)] = None,
    date_from: date | None = None, date_to: date | None = None,
    problems_only: bool = False,
    sort: Annotated[str, Query(pattern="^(variance|newest|oldest|value)$")] = "variance",
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0, le=10_000_000)] = 0,
) -> dict:
    return dashboard.orders_list(
        conn, status=status, partner_id=partner_id, payment_mode=payment_mode, search=q,
        date_from=date_from, date_to=date_to, problems_only=problems_only, sort=sort,
        limit=limit, offset=offset)


@router.get("/orders/{order_id}")
def get_order(order_id: int, conn: Conn, _: Anyone, as_of: AsOf) -> dict:
    detail = dashboard.order_detail(conn, order_id, as_of)
    if detail is None:
        raise HTTPException(404, "No such order.")
    return detail


@router.get("/audit")
def get_audit(
    conn: Conn, _: Cofounder, action: str | None = None, user_id: int | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[dict]:
    return audit.recent(conn, limit, offset, user_id, action)