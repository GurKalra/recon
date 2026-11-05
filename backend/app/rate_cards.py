"""Rate cards: what each partner is supposed to charge us, effective-dated.

A card is never edited. A change is a NEW card with a new `effective_from`; the card in force on an
order's date is the latest one that started on or before it. So history stays intact and an old
order is always judged by the card that applied to it. Saving a card does not change any order's
status until the reconciliation is run again.
"""

from __future__ import annotations

from datetime import date, timedelta

from psycopg import Connection

CHANNEL_TYPES = ("website", "marketplace", "quick_commerce")
CHANNEL_FIELDS = ("commission_bps", "fixed_paise", "ship_extra_per_kg_paise", "tcs_bps",
                  "tds_bps", "pg_fee_bps")
COURIER_FIELDS = ("cod_flat_paise", "cod_bps", "return_pct")
EARLIEST = date(2000, 1, 1)
ZONES = ("A", "B", "C", "D")


class RateCardInvalid(ValueError):
    """The card cannot be saved; the message says why in plain words."""


# ---- reading -----------------------------------------------------------------------------------


def _cards(conn: Connection, partner_id: int, kind: str) -> list[dict]:
    table = "channel_cards" if kind == "channel" else "courier_cards"
    cards = conn.execute(
        f"""SELECT c.*, u.full_name AS created_by_name FROM {table} c
            LEFT JOIN users u ON u.id = c.created_by
            WHERE c.partner_id = %s ORDER BY c.effective_from DESC""", (partner_id,)).fetchall()
    for c in cards:
        c["created_by"] = c.pop("created_by_name")
        if kind == "channel":
            c["ship_slabs"] = conn.execute(
                "SELECT max_g, fee_paise FROM channel_ship_slabs WHERE card_id = %s ORDER BY max_g",
                (c["id"],)).fetchall()
        else:
            c["zone_rates"] = conn.execute(
                "SELECT zone, base_paise, addl_paise FROM courier_zone_rates "
                "WHERE card_id = %s ORDER BY zone", (c["id"],)).fetchall()
    return cards


def list_cards(conn: Connection, today: date, partner_id: int | None = None) -> list[dict]:
    """Every active partner with its cards, newest first. `in_force` marks the card that applies
    to an order placed today."""
    partners = conn.execute(
        """SELECT id, code, name, type FROM partners
           WHERE is_active AND (%(p)s::bigint IS NULL OR id = %(p)s)
           ORDER BY type, code""", {"p": partner_id}).fetchall()
    for p in partners:
        p["kind"] = "courier" if p["type"] == "courier" else "channel"
        p["cards"] = _cards(conn, p["id"], p["kind"])
        in_force = next((c for c in p["cards"] if c["effective_from"] <= today), None)
        for c in p["cards"]:
            c["in_force"] = c is in_force
    return partners


def settings(conn: Connection) -> dict:
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM recon_settings")}


# ---- writing -----------------------------------------------------------------------------------


def _partner(conn: Connection, partner_id: int, kind: str) -> dict:
    row = conn.execute("SELECT id, code, name, type FROM partners WHERE id = %s AND is_active",
                       (partner_id,)).fetchone()
    if row is None:
        raise RateCardInvalid("No such partner.")
    is_courier = row["type"] == "courier"
    if kind == "courier" and not is_courier:
        raise RateCardInvalid(f"{row['name']} is not a courier. Use a channel card for it.")
    if kind == "channel" and is_courier:
        raise RateCardInvalid(f"{row['name']} is a courier. Use a courier card for it.")
    return row


def _check_date(conn: Connection, table: str, partner_id: int, effective_from: date,
                today: date) -> None:
    if effective_from < EARLIEST or effective_from > today + timedelta(days=366):
        raise RateCardInvalid("The start date must be between 2000-01-01 and a year from today.")
    if conn.execute(f"SELECT 1 FROM {table} WHERE partner_id = %s AND effective_from = %s",
                    (partner_id, effective_from)).fetchone():
        raise RateCardInvalid(f"There is already a card starting {effective_from}. Cards are never "
                              "overwritten: pick a different start date.")


def _affected_orders(conn: Connection, partner_id: int, kind: str, effective_from: date) -> int:
    """Orders whose expected fees will change the next time the reconciliation runs: those from
    the new start date until the next card begins."""
    table, column = (("channel_cards", "channel_partner_id") if kind == "channel"
                     else ("courier_cards", "courier_partner_id"))
    return conn.execute(
        f"""SELECT count(*) AS n FROM orders o
            WHERE o.{column} = %(p)s AND o.ordered_on >= %(d)s
              AND o.ordered_on < coalesce((SELECT min(effective_from) FROM {table}
                                           WHERE partner_id = %(p)s AND effective_from > %(d)s),
                                          'infinity')""",
        {"p": partner_id, "d": effective_from}).fetchone()["n"]


def _previous(cards: list[dict], effective_from: date) -> dict | None:
    return next((c for c in cards if c["effective_from"] < effective_from), None)


def _slabs(slabs: list[tuple[int, int]]) -> list[tuple[int, int]]:
    ordered = sorted(slabs)
    if len({max_g for max_g, _ in ordered}) != len(ordered):
        raise RateCardInvalid("Two shipping bands end at the same weight.")
    return ordered


def add_channel(conn: Connection, user_id: int | None, partner_id: int, effective_from: date,
                fields: dict[str, int], slabs: list[tuple[int, int]], note: str | None,
                today: date) -> dict:
    partner = _partner(conn, partner_id, "channel")
    _check_date(conn, "channel_cards", partner_id, effective_from, today)
    ordered = _slabs(slabs)
    previous = _previous(_cards(conn, partner_id, "channel"), effective_from)
    card_id = conn.execute(
        """INSERT INTO channel_cards (partner_id, effective_from, commission_bps, fixed_paise,
                                      ship_extra_per_kg_paise, tcs_bps, tds_bps, pg_fee_bps, note,
                                      created_by)
           VALUES (%(partner_id)s, %(effective_from)s, %(commission_bps)s, %(fixed_paise)s,
                   %(ship_extra_per_kg_paise)s, %(tcs_bps)s, %(tds_bps)s, %(pg_fee_bps)s,
                   %(note)s, %(user)s) RETURNING id""",
        {**fields, "partner_id": partner_id, "effective_from": effective_from, "note": note,
         "user": user_id}).fetchone()["id"]
    for max_g, fee in ordered:
        conn.execute("INSERT INTO channel_ship_slabs (card_id, max_g, fee_paise) "
                     "VALUES (%s, %s, %s)", (card_id, max_g, fee))
    conn.commit()
    changes = _diff_channel(previous, fields, ordered)
    return {"card_id": card_id, "partner_code": partner["code"], "changes": changes,
            "orders_affected": _affected_orders(conn, partner_id, "channel", effective_from)}


def add_courier(conn: Connection, user_id: int | None, partner_id: int, effective_from: date,
                fields: dict[str, int], zones: dict[str, tuple[int, int]], note: str | None,
                today: date) -> dict:
    partner = _partner(conn, partner_id, "courier")
    _check_date(conn, "courier_cards", partner_id, effective_from, today)
    if set(zones) != set(ZONES):
        raise RateCardInvalid("A courier card needs rates for all four zones: A, B, C and D.")
    previous = _previous(_cards(conn, partner_id, "courier"), effective_from)
    card_id = conn.execute(
        """INSERT INTO courier_cards (partner_id, effective_from, cod_flat_paise, cod_bps,
                                      return_pct, note, created_by)
           VALUES (%(partner_id)s, %(effective_from)s, %(cod_flat_paise)s, %(cod_bps)s,
                   %(return_pct)s, %(note)s, %(user)s) RETURNING id""",
        {**fields, "partner_id": partner_id, "effective_from": effective_from, "note": note,
         "user": user_id}).fetchone()["id"]
    for zone in ZONES:
        base, addl = zones[zone]
        conn.execute("INSERT INTO courier_zone_rates (card_id, zone, base_paise, addl_paise) "
                     "VALUES (%s, %s, %s, %s)", (card_id, zone, base, addl))
    conn.commit()
    changes = _diff_courier(previous, fields, zones)
    return {"card_id": card_id, "partner_code": partner["code"], "changes": changes,
            "orders_affected": _affected_orders(conn, partner_id, "courier", effective_from)}


# ---- what changed ------------------------------------------------------------------------------


def _diff_channel(previous: dict | None, fields: dict[str, int],
                  slabs: list[tuple[int, int]]) -> list[dict]:
    if previous is None:
        return []
    out = [{"field": f, "from": previous[f], "to": fields[f]}
           for f in CHANNEL_FIELDS if previous[f] != fields[f]]
    before = {s["max_g"]: s["fee_paise"] for s in previous["ship_slabs"]}
    after = dict(slabs)
    for max_g in sorted(before.keys() | after.keys()):
        if before.get(max_g) != after.get(max_g):
            out.append({"field": f"shipping_fee_up_to_{max_g}g_paise",
                        "from": before.get(max_g), "to": after.get(max_g)})
    return out


def _diff_courier(previous: dict | None, fields: dict[str, int],
                  zones: dict[str, tuple[int, int]]) -> list[dict]:
    if previous is None:
        return []
    out = [{"field": f, "from": previous[f], "to": fields[f]}
           for f in COURIER_FIELDS if previous[f] != fields[f]]
    before = {r["zone"]: (r["base_paise"], r["addl_paise"]) for r in previous["zone_rates"]}
    for zone in ZONES:
        for i, label in enumerate(("base_paise", "addl_paise")):
            if before[zone][i] != zones[zone][i]:
                out.append({"field": f"zone_{zone}_{label}", "from": before[zone][i],
                            "to": zones[zone][i]})
    return out