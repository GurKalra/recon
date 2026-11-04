"""Claims: find money to claim, raise a claim, record what the partner decided, export evidence."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from app import audit, claim_evidence, claims
from app.api.deps import Anyone, AsOf, Cofounder, Conn, Exec, User, client_ip

router = APIRouter(prefix="/api/claims", tags=["claims"])

FindingIds = Annotated[list[int], Field(min_length=1, max_length=5000)]


class NewClaim(BaseModel):
    partner_id: int
    finding_ids: FindingIds
    note: str | None = Field(default=None, max_length=500)


class FindingList(BaseModel):
    finding_ids: FindingIds


class Submit(BaseModel):
    partner_ref: str | None = Field(default=None, max_length=100)


class Outcome(BaseModel):
    outcome: str = Field(pattern="^(recovered|partial|rejected|written_off)$")
    recovered_paise: int | None = Field(default=None, ge=0)
    note: str | None = Field(default=None, max_length=500)


class WriteOff(BaseModel):
    finding_ids: FindingIds
    note: str | None = Field(default=None, max_length=500)


def _audit(conn, user, request, action, entity="claim", entity_id=None, **detail):
    audit.record(conn, user["id"], action, entity, entity_id, detail, client_ip(request))


# ---- what to claim -----------------------------------------------------------------------------


@router.get("/candidates")
def candidates(
    conn: Conn, _: Anyone, as_of: AsOf, partner_id: int | None = None,
    types: Annotated[list[str] | None, Query()] = None,
    bucket: Annotated[str | None, Query(pattern="^(expired|critical|soon|ok)$")] = None,
    include_claimed: bool = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[dict]:
    return claims.candidates(conn, as_of, partner_id, types, bucket, include_claimed, limit,
                             offset)


@router.get("/deadlines")
def deadlines(conn: Conn, _: Anyone, as_of: AsOf) -> list[dict]:
    return claims.deadline_summary(conn, as_of)


@router.get("/findings/{finding_id}/history")
def finding_history(finding_id: int, conn: Conn, _: Anyone) -> list[dict]:
    return claims.history(conn, finding_id)


@router.post("/write-off")
def write_off(body: WriteOff, request: Request, conn: Conn, user: Cofounder) -> dict:
    """Giving up on money is the co-founder's call."""
    n = claims.write_off(conn, body.finding_ids, user["id"], body.note)
    _audit(conn, user, request, "finding.write_off", "finding", None,
           finding_ids=body.finding_ids[:200], count=n, note=body.note)
    return {"written_off": n}


# ---- claims ------------------------------------------------------------------------------------


@router.get("")
def list_claims(
    conn: Conn, _: Anyone, as_of: AsOf,
    status: Annotated[str | None, Query(pattern="^(draft|submitted|closed)$")] = None,
    partner_id: int | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[dict]:
    return claims.list_claims(conn, as_of, status, partner_id, limit, offset)


@router.post("", status_code=201)
def create(body: NewClaim, request: Request, conn: Conn, user: Exec) -> dict:
    claim_id = claims.create_claim(conn, body.partner_id, body.finding_ids, user["id"], body.note)
    _audit(conn, user, request, "claim.create", entity_id=claim_id,
           findings=len(set(body.finding_ids)))
    return {"id": claim_id}


@router.get("/{claim_id}")
def detail(claim_id: int, conn: Conn, _: Anyone, as_of: AsOf) -> dict:
    return claims.claim_detail(conn, claim_id, as_of)


@router.post("/{claim_id}/items", status_code=201)
def add_items(claim_id: int, body: FindingList, request: Request, conn: Conn, user: Exec) -> dict:
    claims.add_findings(conn, claim_id, body.finding_ids)
    _audit(conn, user, request, "claim.add", entity_id=claim_id,
           findings=len(set(body.finding_ids)))
    return {"ok": True}


@router.delete("/{claim_id}/items/{finding_id}")
def remove_item(claim_id: int, finding_id: int, request: Request, conn: Conn,
                user: Exec) -> dict:
    claims.remove_finding(conn, claim_id, finding_id)
    _audit(conn, user, request, "claim.remove", entity_id=claim_id, finding_id=finding_id)
    return {"ok": True}


@router.delete("/{claim_id}")
def discard(claim_id: int, request: Request, conn: Conn, user: Exec) -> dict:
    claims.discard_draft(conn, claim_id)
    _audit(conn, user, request, "claim.discard", entity_id=claim_id)
    return {"ok": True}


@router.post("/{claim_id}/submit")
def submit(claim_id: int, body: Submit, request: Request, conn: Conn, user: Exec,
           as_of: AsOf) -> dict:
    warnings = claims.submit_claim(conn, claim_id, as_of, user["id"], body.partner_ref)
    _audit(conn, user, request, "claim.submit", entity_id=claim_id,
           partner_ref=body.partner_ref, late=len(warnings))
    return {"warnings": warnings}


@router.post("/items/{item_id}/outcome")
def outcome(item_id: int, body: Outcome, request: Request, conn: Conn, user: User) -> dict:
    """The executive records recovered / partial / rejected; writing an order off needs the
    co-founder."""
    allowed = {"finance_exec": {"recovered", "partial", "rejected"},
               "cofounder": {"written_off"}}[user["role"]]
    if body.outcome not in allowed:
        raise HTTPException(403, "Your role cannot record that outcome.")
    claims.record_outcome(conn, item_id, body.outcome, user["id"], body.recovered_paise,
                          body.note)
    _audit(conn, user, request, f"claim.{body.outcome}", "claim_item", item_id,
           recovered_paise=body.recovered_paise)
    return {"ok": True}


@router.post("/items/{item_id}/reopen")
def reopen(item_id: int, request: Request, conn: Conn, user: Exec) -> dict:
    claims.reopen_item(conn, item_id, user["id"])
    _audit(conn, user, request, "claim.reopen", "claim_item", item_id)
    return {"ok": True}


@router.get("/{claim_id}/evidence")
def evidence(claim_id: int, request: Request, conn: Conn, user: Anyone, as_of: AsOf) -> Response:
    name, blob = claim_evidence.evidence_zip(conn, claim_id, as_of)
    _audit(conn, user, request, "evidence.download", entity_id=claim_id, file=name)
    return Response(blob, media_type="application/zip", headers={
        "Content-Disposition": f'attachment; filename="{name}"',
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})