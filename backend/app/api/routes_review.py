"""The review queue: payments that arrived without an order ID, their ranked suggestions, and the
person's decisions. Only the finance executive decides; the cofounder can read.

Nothing here links a payment on its own. The AI's pick is stored beside the rules' ranking and
shown, never acted on."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app import audit, review
from app.ai.llm import LLMClient
from app.ai.rerank import rerank
from app.api.deps import Anyone, Conn, Exec, client_ip
from app.config import get_settings
from app.ratelimit import SlidingWindow
from app.recon import matching

router = APIRouter(prefix="/api/review", tags=["review"])
MAX_RERANK = 10
_rerank_limit: SlidingWindow | None = None


def rerank_limiter() -> SlidingWindow:
    global _rerank_limit
    if _rerank_limit is None:
        _rerank_limit = SlidingWindow(get_settings().ai_reranks_per_10min, 600)
    return _rerank_limit


def make_llm() -> LLMClient:
    """A seam for tests: they swap this for a client with a fake transport."""
    return LLMClient.from_settings()


@contextmanager
def _decision() -> Iterator[None]:
    """Turn a refused decision into a clear HTTP answer: 404 if the thing is gone, else 409."""
    try:
        yield
    except matching.ReviewError as exc:
        text = str(exc)
        raise HTTPException(404 if text.startswith("No such") else 409, text) from exc


class LinkBody(BaseModel):
    order_id: int
    note: str | None = Field(default=None, max_length=300)


class DismissBody(BaseModel):
    note: str | None = Field(default=None, max_length=300)


class RerankBody(BaseModel):
    group_ids: list[int] | None = Field(default=None, max_length=MAX_RERANK)
    limit: int = Field(default=MAX_RERANK, ge=1, le=MAX_RERANK)
    everything: bool = False     # also ask about payments the rules are already confident in


# ---- reading -----------------------------------------------------------------------------------


@router.get("/summary")
def get_summary(conn: Conn, _: Anyone) -> dict:
    return review.summary(conn)


@router.get("/queue")
def get_queue(
    conn: Conn, _: Anyone,
    status: Annotated[str, Query(pattern="^(open|linked|dismissed)$")] = "open",
    partner_id: int | None = None,
    confidence: Annotated[str | None, Query(pattern="^(high|medium|low|none)$")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 25,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
) -> dict:
    return {"total": review.total(conn, status, partner_id, confidence),
            "rows": review.page(conn, status, partner_id, confidence, limit, offset)}


@router.get("/groups/{group_id}")
def get_group(group_id: int, conn: Conn, _: Anyone) -> dict:
    found = review.detail(conn, group_id)
    if found is None:
        raise HTTPException(404, "No such payment.")
    return found


# ---- deciding ----------------------------------------------------------------------------------

_RECON_NOTE = "Run the reconciliation to bring the order's status up to date."


@router.post("/suggestions/{suggestion_id}/accept")
def accept(suggestion_id: int, request: Request, conn: Conn, user: Exec) -> dict:
    with _decision():
        lines = matching.accept(conn, suggestion_id, user["id"])
    audit.record(conn, user["id"], "review.accept", "suggestion", suggestion_id,
                 {"lines_linked": lines}, client_ip(request))
    return {"lines_linked": lines, "recon_stale": True, "note": _RECON_NOTE}


@router.post("/suggestions/{suggestion_id}/reject")
def reject(suggestion_id: int, request: Request, conn: Conn, user: Exec) -> dict:
    with _decision():
        matching.reject(conn, suggestion_id, user["id"])
    audit.record(conn, user["id"], "review.reject", "suggestion", suggestion_id,
                 ip=client_ip(request))
    return {"ok": True}


@router.post("/groups/{group_id}/link")
def link(group_id: int, body: LinkBody, request: Request, conn: Conn, user: Exec) -> dict:
    """The reviewer names the order themselves."""
    with _decision():
        lines = matching.link_manual(conn, group_id, body.order_id, user["id"], body.note)
    audit.record(conn, user["id"], "review.link", "match_group", group_id,
                 {"order_id": body.order_id, "lines_linked": lines}, client_ip(request))
    return {"lines_linked": lines, "recon_stale": True, "note": _RECON_NOTE}


@router.post("/groups/{group_id}/undo")
def undo(group_id: int, request: Request, conn: Conn, user: Exec) -> dict:
    with _decision():
        lines = matching.undo(conn, group_id, user["id"])
    audit.record(conn, user["id"], "review.undo", "match_group", group_id,
                 {"lines_unlinked": lines}, client_ip(request))
    return {"lines_unlinked": lines, "recon_stale": True, "note": _RECON_NOTE}


@router.post("/groups/{group_id}/dismiss")
def dismiss(group_id: int, body: DismissBody, request: Request, conn: Conn, user: Exec) -> dict:
    with _decision():
        matching.dismiss(conn, group_id, user["id"], body.note)
    audit.record(conn, user["id"], "review.dismiss", "match_group", group_id,
                 ip=client_ip(request))
    return {"ok": True}


# ---- asking the AI -----------------------------------------------------------------------------


@router.post("/rerank")
def ask_ai(body: RerankBody, request: Request, conn: Conn, user: Exec) -> dict:
    """Ask the model about up to 10 unsure payments. It adds an opinion beside the rules'
    ranking; it never links anything. Limited per person because every payment is a model call."""
    limiter = rerank_limiter()
    if not limiter.allow(user["id"]):
        raise HTTPException(429, "Too many AI requests. Try again shortly.",
                            headers={"Retry-After": str(limiter.retry_after(user["id"]))})
    llm = make_llm()
    if not llm.available:
        raise HTTPException(503, "The AI is not set up on this server (no API key). The rules' "
                                 "ranking still works.")
    stats = rerank(conn, llm, ids=body.group_ids, everything=body.everything, limit=body.limit)
    audit.record(conn, user["id"], "review.rerank", "match_group", None,
                 {"asked": stats.asked, "agreed": stats.agreed, "other": stats.other,
                  "none": stats.none, "errors": stats.errors, "tokens": stats.tokens},
                 client_ip(request))
    return {"asked": stats.asked, "agreed_with_rules": stats.agreed, "picked_another": stats.other,
            "unsure": stats.none, "errors": stats.errors, "tokens": stats.tokens,
            "notes": stats.notes}