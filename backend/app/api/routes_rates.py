"""Rate cards: see what each partner should charge, and add a new effective-dated card.

Cards are never edited or deleted; a change is a new card with a later start date, so old orders
are still judged by the card that applied to them. Saving a card flags the reconciliation as out
of date; order statuses change only when it is run again."""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from app import audit, rate_cards
from app.api.deps import Anyone, Conn, Exec, client_ip, today_ist

router = APIRouter(prefix="/api/rate-cards", tags=["rate cards"])

Bps = Annotated[int, Field(ge=0, le=10_000)]
Paise = Annotated[int, Field(ge=0, le=100_000_000)]
Note = Annotated[str | None, Field(max_length=300)]


class Slab(BaseModel):
    max_g: Annotated[int, Field(gt=0, le=1_000_000)]
    fee_paise: Paise


class ChannelCard(BaseModel):
    partner_id: int
    effective_from: date
    commission_bps: Bps = 0
    fixed_paise: Paise = 0
    ship_extra_per_kg_paise: Paise = 0
    tcs_bps: Bps = 0
    tds_bps: Bps = 0
    pg_fee_bps: Bps = 0
    ship_slabs: Annotated[list[Slab], Field(max_length=30)] = []
    note: Note = None


class ZoneRate(BaseModel):
    zone: str
    base_paise: Paise
    addl_paise: Paise

    @field_validator("zone")
    @classmethod
    def _zone(cls, v: str) -> str:
        if v not in rate_cards.ZONES:
            raise ValueError("zone must be A, B, C or D")
        return v


class CourierCard(BaseModel):
    partner_id: int
    effective_from: date
    cod_flat_paise: Paise = 0
    cod_bps: Bps = 0
    return_pct: Annotated[int, Field(ge=0, le=1000)] = 100
    zone_rates: Annotated[list[ZoneRate], Field(max_length=4)]
    note: Note = None


@router.get("")
def get_cards(conn: Conn, _: Anyone, partner_id: int | None = None) -> list[dict]:
    return rate_cards.list_cards(conn, today_ist(), partner_id)


@router.get("/settings")
def get_settings_(conn: Conn, _: Anyone) -> dict:
    return rate_cards.settings(conn)


@router.post("/channel", status_code=201)
def add_channel(body: ChannelCard, request: Request, conn: Conn, user: Exec) -> dict:
    fields = {f: getattr(body, f) for f in rate_cards.CHANNEL_FIELDS}
    try:
        out = rate_cards.add_channel(
            conn, user["id"], body.partner_id, body.effective_from, fields,
            [(s.max_g, s.fee_paise) for s in body.ship_slabs], body.note, today_ist())
    except rate_cards.RateCardInvalid as exc:
        raise HTTPException(422, str(exc)) from exc
    return _saved(conn, request, user, out, body.partner_id, body.effective_from)


@router.post("/courier", status_code=201)
def add_courier(body: CourierCard, request: Request, conn: Conn, user: Exec) -> dict:
    fields = {f: getattr(body, f) for f in rate_cards.COURIER_FIELDS}
    zones = {z.zone: (z.base_paise, z.addl_paise) for z in body.zone_rates}
    if len(zones) != len(body.zone_rates):
        raise HTTPException(422, "A zone appears twice.")
    try:
        out = rate_cards.add_courier(conn, user["id"], body.partner_id, body.effective_from,
                                     fields, zones, body.note, today_ist())
    except rate_cards.RateCardInvalid as exc:
        raise HTTPException(422, str(exc)) from exc
    return _saved(conn, request, user, out, body.partner_id, body.effective_from)


def _saved(conn, request: Request, user: dict, out: dict, partner_id: int,
           effective_from: date) -> dict:
    audit.record(conn, user["id"], "rate_card.add", "partner", partner_id,
                 {"card_id": out["card_id"], "effective_from": str(effective_from),
                  "changes": out["changes"], "orders_affected": out["orders_affected"]},
                 client_ip(request))
    return {**out, "recon_stale": True,
            "note": "Run the reconciliation to apply this card to the affected orders."}