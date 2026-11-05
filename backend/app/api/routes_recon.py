"""Run the reconciliation and see when it last ran."""

from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request

from app import audit, claims
from app.api.deps import Anyone, AsOf, Conn, Exec, client_ip
from app.importer import WORK_LOCK
from app.recon.engine import run_reconciliation

router = APIRouter(prefix="/api/recon", tags=["reconciliation"])


@router.post("/run")
def run(request: Request, conn: Conn, user: Exec, as_of: AsOf) -> dict:
    if not WORK_LOCK.acquire(blocking=False):
        raise HTTPException(409, "An import or another run is in progress. Try again in a moment.")
    try:
        result = run_reconciliation(conn, as_of, triggered_by=user["id"])
        synced = claims.sync_after_run(conn)
    finally:
        WORK_LOCK.release()
    audit.record(conn, user["id"], "recon.run", "recon_run", result.run_id,
                 {"as_of": str(as_of), "orders": result.orders, "synced_claims": synced},
                 client_ip(request))
    return {**asdict(result), "claims": synced}


@router.get("/runs")
def runs(conn: Conn, _: Anyone) -> list[dict]:
    return conn.execute(
        "SELECT id, as_of, status, orders_total, stats, stage_timings, started_at, finished_at "
        "FROM recon_runs ORDER BY id DESC LIMIT 20").fetchall()