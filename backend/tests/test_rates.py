"""Rate cards: reading them, adding effective-dated cards, and what saving one does."""

from __future__ import annotations

import os
import uuid
from datetime import date

import gen_data as g
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from app import auth, demo
from app.config import get_settings
from app.migrate import migrate
from app.recon import matching
from app.recon.engine import run_reconciliation

TODAY = date(2026, 11, 2)
PASSWORD = "correct horse battery"
EXEC, COFOUNDER = "exec@rc.example", "cofounder@rc.example"


@pytest.fixture(scope="module")
def server(admin_dsn: str, tmp_path_factory):
    data = tmp_path_factory.mktemp("rc_data")
    g.Generator(data, 1500, seed=7, today=TODAY, use_xlsx=True).run()
    name = f"recon_test_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    dsn = make_conninfo(**{**conninfo_to_dict(admin_dsn), "dbname": name})
    migrate(dsn)
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        demo.load_dataset(conn, data, echo=False)
        matching.refresh(conn)
        run_reconciliation(conn, TODAY)
        auth.create_user(conn, EXEC, PASSWORD, "Rc Exec", "finance_exec")
        auth.create_user(conn, COFOUNDER, PASSWORD, "Rc Cofounder", "cofounder")
    saved = {k: os.environ.get(k) for k in ("DATABASE_URL", "UPLOAD_DIR", "GROQ_API_KEY")}
    os.environ.update(DATABASE_URL=dsn, GROQ_API_KEY="",
                      UPLOAD_DIR=str(tmp_path_factory.mktemp("rc_uploads")))
    get_settings.cache_clear()
    from app.main import app
    try:
        with TestClient(app) as client:
            client.dsn = dsn
            yield client
    finally:
        get_settings.cache_clear()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def db(server):
    return psycopg.connect(server.dsn, row_factory=dict_row, autocommit=True)


def sign_in(server, email: str) -> dict:
    server.cookies.clear()
    res = server.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert res.status_code == 200, res.text
    return {"X-CSRF-Token": res.json()["csrf_token"]}


def partner(server, code: str) -> dict:
    return next(p for p in server.get("/api/rate-cards").json() if p["code"] == code)


def channel_body(server, code: str, effective_from: str, **over) -> dict:
    card = partner(server, code)["cards"][0]
    body = {"partner_id": partner(server, code)["id"], "effective_from": effective_from,
            "commission_bps": card["commission_bps"], "fixed_paise": card["fixed_paise"],
            "ship_extra_per_kg_paise": card["ship_extra_per_kg_paise"],
            "tcs_bps": card["tcs_bps"], "tds_bps": card["tds_bps"],
            "pg_fee_bps": card["pg_fee_bps"], "ship_slabs": card["ship_slabs"]}
    return {**body, **over}


# ---- reading -----------------------------------------------------------------------------------


def test_signed_out_people_see_nothing_and_both_roles_can_read(server):
    server.cookies.clear()
    assert server.get("/api/rate-cards").status_code == 401
    sign_in(server, COFOUNDER)
    assert server.get("/api/rate-cards").status_code == 200
    assert server.get("/api/rate-cards/settings").json()["receipt_wait_days"] == 7


def test_every_partner_shows_its_cards_with_exactly_one_in_force(server):
    sign_in(server, EXEC)
    partners = server.get("/api/rate-cards").json()
    assert {p["code"] for p in partners} >= {"M1", "Q1", "WEB", "CA", "CB"}
    for p in partners:
        assert p["kind"] == ("courier" if p["type"] == "courier" else "channel")
        assert len(p["cards"]) >= 1 and sum(c["in_force"] for c in p["cards"]) == 1
    m1 = partner(server, "M1")["cards"][0]
    assert m1["commission_bps"] > 0 and m1["ship_slabs"] and "created_by" in m1
    ca = partner(server, "CA")["cards"][0]
    assert [z["zone"] for z in ca["zone_rates"]] == ["A", "B", "C", "D"]
    only = server.get("/api/rate-cards", params={"partner_id": m1["partner_id"]}).json()
    assert [p["code"] for p in only] == ["M1"]


# ---- who may write -----------------------------------------------------------------------------


def test_only_the_executive_can_add_a_card_and_it_needs_the_csrf_token(server):
    ex = sign_in(server, EXEC)
    body = channel_body(server, "M1", "2026-06-01")
    assert server.post("/api/rate-cards/channel", json=body).status_code == 403   # no token
    co = sign_in(server, COFOUNDER)
    assert server.post("/api/rate-cards/channel", json=body, headers=co).status_code == 403
    assert ex


def test_cards_cannot_be_edited_or_deleted(server):
    ex = sign_in(server, EXEC)
    for method in ("put", "patch", "delete"):
        assert getattr(server, method)("/api/rate-cards/channel", headers=ex).status_code == 405


# ---- refusals ----------------------------------------------------------------------------------


def test_bad_cards_are_refused_with_a_reason(server):
    ex = sign_in(server, EXEC)
    m1 = partner(server, "M1")
    ca = partner(server, "CA")
    ok = channel_body(server, "M1", "2026-06-01")

    def post(path, body):
        return server.post(f"/api/rate-cards/{path}", json=body, headers=ex)

    same_day = post("channel", {**ok, "effective_from": str(m1["cards"][0]["effective_from"])})
    assert same_day.status_code == 422 and "never overwritten" in same_day.json()["detail"]
    assert post("channel", {**ok, "effective_from": "1999-12-31"}).status_code == 422
    assert post("channel", {**ok, "effective_from": "2099-01-01"}).status_code == 422
    assert post("channel", {**ok, "commission_bps": 10001}).status_code == 422
    assert post("channel", {**ok, "fixed_paise": -1}).status_code == 422
    twin = post("channel", {**ok, "ship_slabs": [{"max_g": 500, "fee_paise": 1},
                                                  {"max_g": 500, "fee_paise": 2}]})
    assert twin.status_code == 422 and "same weight" in twin.json()["detail"]
    wrong = post("channel", {**ok, "partner_id": ca["id"]})
    assert wrong.status_code == 422 and "courier" in wrong.json()["detail"]
    nobody = post("channel", {**ok, "partner_id": 10**6})
    assert nobody.status_code == 422 and "No such partner" in nobody.json()["detail"]

    zones = [{"zone": z, "base_paise": 5000, "addl_paise": 3000} for z in "ABCD"]
    courier = {"partner_id": ca["id"], "effective_from": "2026-06-01", "zone_rates": zones}
    assert post("courier", {**courier, "zone_rates": zones[:3]}).status_code == 422
    assert post("courier", {**courier, "zone_rates": zones[:3] + [zones[0]]}).status_code == 422
    assert post("courier", {**courier, "zone_rates": zones[:3]
                            + [{"zone": "E", "base_paise": 1, "addl_paise": 1}]}
                ).status_code == 422
    not_courier = post("courier", {**courier, "partner_id": m1["id"]})
    assert not_courier.status_code == 422 and "not a courier" in not_courier.json()["detail"]
    with db(server) as conn:                   # nothing was saved by any of the above
        assert conn.execute("SELECT count(*) AS n FROM channel_cards").fetchone()["n"] == 6
        assert conn.execute("SELECT count(*) AS n FROM courier_cards").fetchone()["n"] == 2


# ---- adding a card and what it does ------------------------------------------------------------


def test_a_new_card_is_added_not_edited_and_only_applies_from_its_date(server):
    ex = sign_in(server, EXEC)
    m1 = partner(server, "M1")
    old_card = m1["cards"][0]
    with db(server) as conn:
        dates = [r["ordered_on"] for r in conn.execute(
            "SELECT ordered_on FROM orders WHERE channel_partner_id = %s AND payment_mode = "
            "'prepaid' AND value_paise >= 100000 ORDER BY ordered_on", (m1["id"],))]
        start = dates[len(dates) // 2]
        before = conn.execute(
            "SELECT o.id, s.expected_paise FROM orders o JOIN order_status s ON s.order_id = o.id "
            "WHERE o.channel_partner_id = %s AND o.payment_mode = 'prepaid' "
            "AND o.value_paise >= 100000 AND o.ordered_on < %s ORDER BY o.id LIMIT 1",
            (m1["id"], start)).fetchone()
        after = conn.execute(
            "SELECT o.id, s.expected_paise FROM orders o JOIN order_status s ON s.order_id = o.id "
            "WHERE o.channel_partner_id = %s AND o.payment_mode = 'prepaid' "
            "AND o.value_paise >= 100000 AND o.ordered_on >= %s ORDER BY o.id LIMIT 1",
            (m1["id"], start)).fetchone()
        n_after = conn.execute("SELECT count(*) AS n FROM orders WHERE channel_partner_id = %s "
                               "AND ordered_on >= %s", (m1["id"], start)).fetchone()["n"]
    assert before and after

    status = server.get("/api/recon/status").json()
    assert status["stale"] is False and status["reasons"] == []

    body = channel_body(server, "M1", str(start), commission_bps=old_card["commission_bps"] + 300,
                        note="renegotiated")
    res = server.post("/api/rate-cards/channel", json=body, headers=ex)
    assert res.status_code == 201, res.text
    out = res.json()
    assert out["recon_stale"] is True and out["orders_affected"] == n_after
    assert out["changes"] == [{"field": "commission_bps", "from": old_card["commission_bps"],
                               "to": old_card["commission_bps"] + 300}]

    cards = partner(server, "M1")["cards"]
    assert len(cards) == 2 and [c["note"] for c in cards] == ["renegotiated", "built-in demo card"]
    assert cards[1]["commission_bps"] == old_card["commission_bps"]        # history untouched
    assert cards[0]["created_by"] == "Rc Exec" and cards[0]["ship_slabs"] == old_card["ship_slabs"]
    assert [c["in_force"] for c in cards] == [True, False]

    stale = server.get("/api/recon/status").json()
    assert stale["stale"] is True and stale["reasons"] == ["rate_cards"]
    with db(server) as conn:                 # saving a card changes no order until the next run
        same = conn.execute("SELECT expected_paise FROM order_status WHERE order_id = %s",
                            (after["id"],)).fetchone()["expected_paise"]
    assert same == after["expected_paise"]

    assert server.post("/api/recon/run", params={"as_of": TODAY.isoformat()},
                       headers=ex).status_code == 200
    with db(server) as conn:
        new_after = conn.execute("SELECT expected_paise FROM order_status WHERE order_id = %s",
                                 (after["id"],)).fetchone()["expected_paise"]
        new_before = conn.execute("SELECT expected_paise FROM order_status WHERE order_id = %s",
                                  (before["id"],)).fetchone()["expected_paise"]
    assert new_after < after["expected_paise"]           # more commission, less expected payout
    assert new_before == before["expected_paise"]        # older orders keep their old card
    done = server.get("/api/recon/status").json()
    assert done["stale"] is False and done["last_run"]["id"]


def test_a_courier_card_shows_exactly_which_rates_changed(server):
    ex = sign_in(server, EXEC)
    ca = partner(server, "CA")
    old = ca["cards"][0]
    zones = [{"zone": z["zone"], "base_paise": z["base_paise"] + (500 if z["zone"] == "B" else 0),
              "addl_paise": z["addl_paise"]} for z in old["zone_rates"]]
    res = server.post("/api/rate-cards/courier", headers=ex, json={
        "partner_id": ca["id"], "effective_from": "2026-08-01",
        "cod_flat_paise": old["cod_flat_paise"], "cod_bps": old["cod_bps"], "return_pct": 80,
        "zone_rates": zones})
    assert res.status_code == 201, res.text
    changes = {c["field"]: (c["from"], c["to"]) for c in res.json()["changes"]}
    assert changes == {"return_pct": (old["return_pct"], 80),
                       "zone_B_base_paise": (old["zone_rates"][1]["base_paise"],
                                             old["zone_rates"][1]["base_paise"] + 500)}
    assert res.json()["orders_affected"] >= 0
    newest = partner(server, "CA")["cards"][0]
    assert newest["return_pct"] == 80 and len(newest["zone_rates"]) == 4


def test_a_review_decision_also_marks_the_run_out_of_date(server):
    ex = sign_in(server, EXEC)
    server.post("/api/recon/run", params={"as_of": TODAY.isoformat()}, headers=ex)
    assert server.get("/api/recon/status").json()["stale"] is False
    queue = server.get("/api/review/queue", params={"limit": 50}).json()["rows"]
    row = next(r for r in queue if r["suggestions"])
    assert server.post(f"/api/review/suggestions/{row['suggestions'][0]['id']}/accept",
                       headers=ex).status_code == 200
    status = server.get("/api/recon/status").json()
    assert status["stale"] is True and "review" in status["reasons"]


def test_the_audit_trail_records_rate_card_changes(server):
    sign_in(server, COFOUNDER)
    rows = [r for r in server.get("/api/audit", params={"limit": 500}).json()
            if r["action"] == "rate_card.add"]
    assert len(rows) == 2
    assert any(r["detail"]["changes"] for r in rows)