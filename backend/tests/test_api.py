"""The web API: sign-in, sessions, roles, CSRF, audit, and the claim flow end to end."""

from __future__ import annotations

import io
import os
import uuid
import zipfile
from datetime import date

import gen_data as g
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from app import auth, demo, security, users
from app.config import get_settings
from app.migrate import migrate
from app.recon.engine import run_reconciliation

TODAY = date(2026, 11, 2)
PASSWORD = "correct horse battery"
EXEC, COFOUNDER = "exec@test.example", "cofounder@test.example"


@pytest.fixture(scope="module")
def server(admin_dsn: str, tmp_path_factory: pytest.TempPathFactory):
    """One database, loaded and reconciled once, with an executive and a co-founder."""
    name = f"recon_test_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    dsn = make_conninfo(**{**conninfo_to_dict(admin_dsn), "dbname": name})
    migrate(dsn)
    data = tmp_path_factory.mktemp("api")
    g.Generator(data, 3000, seed=5, today=TODAY, use_xlsx=True).run()
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        demo.load_dataset(conn, data, echo=False)
        run_reconciliation(conn, TODAY)
        auth.create_user(conn, EXEC, PASSWORD, "Test Exec", "finance_exec")
        auth.create_user(conn, COFOUNDER, PASSWORD, "Test Cofounder", "cofounder")
    old = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = dsn
    get_settings.cache_clear()
    from app.main import app
    try:
        with TestClient(app) as client:
            client.dsn = dsn
            yield client
    finally:
        get_settings.cache_clear()
        if old is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = old
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def db(server):
    return psycopg.connect(server.dsn, row_factory=dict_row, autocommit=True)


def sign_in(server, email: str) -> dict:
    """Sign in as this user (replacing any earlier session) and return the CSRF header."""
    server.cookies.clear()
    res = server.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert res.status_code == 200, res.text
    return {"X-CSRF-Token": res.json()["csrf_token"]}


def fresh_findings(server, n: int):
    """(partner_id, finding ids) for n open findings of one partner that are not in a claim."""
    with db(server) as conn:
        rows = conn.execute(
            """SELECT f.partner_id, array_agg(f.id ORDER BY f.id) AS ids FROM findings f
               WHERE f.status = 'open' AND NOT EXISTS (
                   SELECT 1 FROM claim_items ci WHERE ci.finding_id = f.id
                   AND ci.outcome = 'pending')
               GROUP BY f.partner_id HAVING count(*) >= %s ORDER BY count(*) DESC""",
            (n,)).fetchall()
    assert rows
    return rows[0]["partner_id"], rows[0]["ids"][:n]


# ---- passwords and demo users ------------------------------------------------------------------


def test_passwords_are_hashed_checked_and_must_be_long_enough():
    h = security.hash_password(PASSWORD)
    assert h.startswith("$argon2id$") and PASSWORD not in h
    assert security.verify_password(h, PASSWORD)
    assert not security.verify_password(h, PASSWORD + "x")
    assert not security.verify_password(None, "anything")       # unknown user: still false
    assert not security.verify_password("garbage", PASSWORD)
    with pytest.raises(security.WeakPassword):
        security.hash_password("short")


def test_demo_users_are_created_once_with_passwords_from_the_environment(dict_conn):
    first = users.ensure_demo_users(dict_conn, {"DEMO_EXEC_PASSWORD": "from-the-env-123"})
    assert [u["created"] for u in first] == [True, True]
    assert first[0]["password"] == "from-the-env-123" and first[0]["from_env"]
    assert first[1]["password"] and len(first[1]["password"]) >= 10 and not first[1]["from_env"]
    assert [u["created"] for u in users.ensure_demo_users(dict_conn, {})] == [False, False]
    assert auth.authenticate(dict_conn, "exec@isolabs.demo", "from-the-env-123")["role"] == \
        "finance_exec"


# ---- signing in --------------------------------------------------------------------------------


def test_every_data_route_needs_a_session(server):
    server.cookies.clear()
    for path in ("/api/dashboard", "/api/orders", "/api/claims", "/api/claims/candidates",
                 "/api/tax", "/api/auth/me", "/api/audit", "/api/recon/runs"):
        assert server.get(path).status_code == 401, path


def test_the_health_check_stays_public(server):
    server.cookies.clear()
    assert server.get("/api/health").status_code == 200


def test_wrong_password_and_unknown_user_look_the_same(server):
    server.cookies.clear()
    a = server.post("/api/auth/login", json={"email": EXEC, "password": "wrong-password-1"})
    b = server.post("/api/auth/login", json={"email": "nobody@test.example",
                                             "password": "wrong-password-1"})
    assert a.status_code == b.status_code == 401 and a.json() == b.json()
    with db(server) as conn:
        n = conn.execute("SELECT count(*) AS n FROM audit_log WHERE action = 'login.failed'"
                         ).fetchone()["n"]
    assert n >= 2


def test_the_session_cookie_cannot_be_read_by_scripts(server):
    server.cookies.clear()
    res = server.post("/api/auth/login", json={"email": EXEC, "password": PASSWORD})
    cookie = res.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    body = res.json()
    assert body["user"] == {"id": body["user"]["id"], "email": EXEC, "full_name": "Test Exec",
                            "role": "finance_exec"}
    assert "password" not in res.text.lower() and body["csrf_token"]
    me = server.get("/api/auth/me").json()
    assert me["user"]["email"] == EXEC and me["csrf_token"] == body["csrf_token"]


def test_the_session_token_is_stored_only_as_a_hash(server):
    sign_in(server, EXEC)
    token = server.cookies.get("recon_session")
    with db(server) as conn:
        hashes = [r["token_hash"] for r in conn.execute("SELECT token_hash FROM sessions")]
    assert token not in hashes and security.token_digest(token) in hashes


def test_repeated_failures_lock_the_account_even_for_the_right_password(server):
    server.cookies.clear()
    email = "locked@test.example"
    with db(server) as conn:
        auth.create_user(conn, email, PASSWORD, "Locked", "finance_exec")
    for _ in range(auth.MAX_FAILS_PER_EMAIL):
        assert server.post("/api/auth/login",
                           json={"email": email, "password": "nope-nope-nope"}
                           ).status_code == 401
    res = server.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert res.status_code == 429 and int(res.headers["retry-after"]) > 0


def test_logging_out_ends_the_session(server):
    headers = sign_in(server, EXEC)
    old = dict(server.cookies)
    assert server.post("/api/auth/logout", headers=headers).status_code == 200
    server.cookies.clear()
    server.cookies.update(old)                # a thief replaying the old cookie
    assert server.get("/api/auth/me").status_code == 401


def test_expired_and_deactivated_sessions_stop_working(server):
    sign_in(server, EXEC)
    with db(server) as conn:
        conn.execute("UPDATE sessions SET expires_at = now() - interval '1 minute'")
    assert server.get("/api/auth/me").status_code == 401
    sign_in(server, EXEC)
    with db(server) as conn:
        conn.execute("UPDATE sessions SET last_seen_at = now() - interval '3 hours'")
    assert server.get("/api/auth/me").status_code == 401
    sign_in(server, COFOUNDER)
    with db(server) as conn:
        conn.execute("UPDATE users SET is_active = false WHERE email = %s", (COFOUNDER,))
    assert server.get("/api/auth/me").status_code == 401
    with db(server) as conn:
        conn.execute("UPDATE users SET is_active = true WHERE email = %s", (COFOUNDER,))


def test_changing_a_password_signs_the_user_out_everywhere(server):
    email = "rotate@test.example"
    with db(server) as conn:
        uid = auth.create_user(conn, email, PASSWORD, "Rotate", "cofounder")
    sign_in(server, email)
    with db(server) as conn:
        auth.set_password(conn, uid, "a brand new password")
    assert server.get("/api/auth/me").status_code == 401


# ---- CSRF and roles ----------------------------------------------------------------------------


def test_a_change_without_the_csrf_token_is_refused(server):
    headers = sign_in(server, EXEC)
    pid, ids = fresh_findings(server, 1)
    body = {"partner_id": pid, "finding_ids": ids}
    assert server.post("/api/claims", json=body).status_code == 403
    assert server.post("/api/claims", json=body,
                       headers={"X-CSRF-Token": "forged"}).status_code == 403
    assert server.get("/api/claims").status_code == 200        # reads need no token
    res = server.post("/api/claims", json=body, headers=headers)
    assert res.status_code == 201
    server.delete(f"/api/claims/{res.json()['id']}", headers=headers)


def test_the_cofounder_reads_and_exports_but_does_not_change_claims(server):
    headers = sign_in(server, COFOUNDER)
    pid, ids = fresh_findings(server, 1)
    assert server.get("/api/dashboard").status_code == 200
    assert server.post("/api/claims", json={"partner_id": pid, "finding_ids": ids},
                       headers=headers).status_code == 403
    assert server.post("/api/recon/run", headers=headers).status_code == 403


def test_the_executive_cannot_write_off_or_read_the_audit_trail(server):
    headers = sign_in(server, EXEC)
    _, ids = fresh_findings(server, 1)
    assert server.post("/api/claims/write-off", json={"finding_ids": ids},
                       headers=headers).status_code == 403
    assert server.get("/api/audit").status_code == 403


def test_the_cofounder_signs_off_a_write_off_and_it_is_logged(server):
    headers = sign_in(server, COFOUNDER)
    _, ids = fresh_findings(server, 2)
    res = server.post("/api/claims/write-off", json={"finding_ids": ids[:1], "note": "tiny"},
                      headers=headers)
    assert res.status_code == 200 and res.json() == {"written_off": 1}
    log = server.get("/api/audit", params={"action": "finding.write_off"}).json()
    assert log and log[0]["user_email"] == COFOUNDER and log[0]["detail"]["note"] == "tiny"


def test_the_audit_trail_cannot_be_edited_or_erased(server):
    with db(server) as conn:
        assert conn.execute("SELECT count(*) AS n FROM audit_log").fetchone()["n"] > 0
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute("UPDATE audit_log SET action = 'nothing'")
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute("DELETE FROM audit_log")


# ---- screens -----------------------------------------------------------------------------------


def test_the_dashboard_adds_up_and_money_is_whole_paise(server):
    sign_in(server, COFOUNDER)
    d = server.get("/api/dashboard", params={"as_of": TODAY.isoformat()}).json()
    head = d["headline"]
    assert head["money_at_risk_paise"] == d["matrix"]["amount_paise"] > 0
    assert sum(t["amount_paise"] for t in d["by_type"]) == head["money_at_risk_paise"]
    assert isinstance(head["money_at_risk_paise"], int)
    assert all(isinstance(p["amount_paise"], int) for p in d["matrix"]["partners"])
    assert sum(d["order_status"].values()) == 3000


def test_the_orders_table_pages_filters_and_validates(server):
    sign_in(server, COFOUNDER)
    page = server.get("/api/orders", params={"limit": 5, "problems_only": True}).json()
    assert page["total"] > 0 and len(page["rows"]) == 5
    assert server.get("/api/orders", params={"limit": 501}).status_code == 422
    assert server.get("/api/orders", params={"sort": "id; drop table orders"}
                      ).status_code == 422
    assert server.get("/api/orders", params={"payment_mode": "barter"}).status_code == 422
    one = page["rows"][0]
    found = server.get("/api/orders", params={"q": one["order_ref"]}).json()
    assert any(r["id"] == one["id"] for r in found["rows"])


def test_the_order_drawer_has_the_waterfall_and_unknown_orders_are_404(server):
    sign_in(server, COFOUNDER)
    row = server.get("/api/orders", params={"status": "short_paid", "limit": 1}).json()["rows"][0]
    d = server.get(f"/api/orders/{row['id']}", params={"as_of": TODAY.isoformat()}).json()
    assert d["waterfall"] and d["lines"] and d["findings"]
    assert all(line["filename"] for line in d["lines"])
    assert server.get("/api/orders/999999999").status_code == 404


def test_responses_carry_security_headers_and_are_not_cached(server):
    sign_in(server, COFOUNDER)
    res = server.get("/api/dashboard")
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["x-frame-options"] == "DENY"
    assert res.headers["cache-control"] == "no-store"


# ---- the claim flow through the API ------------------------------------------------------------


def test_a_claim_goes_from_candidate_to_recovered_and_the_pack_downloads(server):
    exec_headers = sign_in(server, EXEC)
    pid, ids = fresh_findings(server, 2)
    cands = server.get("/api/claims/candidates",
                       params={"partner_id": pid, "as_of": TODAY.isoformat()}).json()
    assert {c["id"] for c in cands} >= set(ids)

    made = server.post("/api/claims", json={"partner_id": pid, "finding_ids": ids,
                                            "note": "Q3"}, headers=exec_headers)
    assert made.status_code == 201
    cid = made.json()["id"]
    sent = server.post(f"/api/claims/{cid}/submit", json={"partner_ref": "CASE-5"},
                       headers=exec_headers, params={"as_of": TODAY.isoformat()})
    assert sent.status_code == 200 and isinstance(sent.json()["warnings"], list)
    assert server.post(f"/api/claims/{cid}/submit", json={}, headers=exec_headers
                       ).status_code == 409                       # already sent

    detail = server.get(f"/api/claims/{cid}", params={"as_of": TODAY.isoformat()}).json()
    assert detail["status"] == "submitted" and len(detail["items"]) == 2
    for item in detail["items"]:
        res = server.post(f"/api/claims/items/{item['id']}/outcome",
                          json={"outcome": "recovered"}, headers=exec_headers)
        assert res.status_code == 200
    assert server.get(f"/api/claims/{cid}").json()["status"] == "closed"

    sign_in(server, COFOUNDER)                                   # the co-founder exports it
    pack = server.get(f"/api/claims/{cid}/evidence", params={"as_of": TODAY.isoformat()})
    assert pack.status_code == 200 and pack.headers["content-type"] == "application/zip"
    assert f"claim-{cid}-" in pack.headers["content-disposition"]
    assert "README.txt" in zipfile.ZipFile(io.BytesIO(pack.content)).namelist()
    log = server.get("/api/audit", params={"action": "evidence.download"}).json()
    assert log[0]["entity_id"] == cid and log[0]["user_email"] == COFOUNDER
    actions = {r["action"] for r in server.get("/api/audit", params={"limit": 500}).json()}
    assert {"claim.create", "claim.submit", "claim.recovered"} <= actions


def test_only_the_cofounder_can_record_a_write_off_on_a_sent_claim(server):
    exec_headers = sign_in(server, EXEC)
    pid, ids = fresh_findings(server, 1)
    cid = server.post("/api/claims", json={"partner_id": pid, "finding_ids": ids},
                      headers=exec_headers).json()["id"]
    server.post(f"/api/claims/{cid}/submit", json={}, headers=exec_headers)
    item = server.get(f"/api/claims/{cid}").json()["items"][0]
    url = f"/api/claims/items/{item['id']}/outcome"
    assert server.post(url, json={"outcome": "written_off"}, headers=exec_headers
                       ).status_code == 403
    assert server.post(url, json={"outcome": "maybe"}, headers=exec_headers).status_code == 422
    co_headers = sign_in(server, COFOUNDER)
    assert server.post(url, json={"outcome": "written_off"}, headers=co_headers
                       ).status_code == 200
    assert server.post(url, json={"outcome": "recovered"}, headers=co_headers
                       ).status_code == 403                      # not theirs to record


def test_refused_claim_actions_give_clear_status_codes(server):
    headers = sign_in(server, EXEC)
    assert server.get("/api/claims/999999999").status_code == 404
    assert server.post("/api/claims", json={"partner_id": 1, "finding_ids": []},
                       headers=headers).status_code == 422
    pid, ids = fresh_findings(server, 1)
    first = server.post("/api/claims", json={"partner_id": pid, "finding_ids": ids},
                        headers=headers)
    again = server.post("/api/claims", json={"partner_id": pid, "finding_ids": ids},
                        headers=headers)
    assert first.status_code == 201 and again.status_code == 409
    assert "already in claim" in again.json()["detail"]


def test_running_the_reconciliation_is_logged_and_syncs_claims(server):
    headers = sign_in(server, EXEC)
    res = server.post("/api/recon/run", params={"as_of": TODAY.isoformat()}, headers=headers)
    assert res.status_code == 200
    body = res.json()
    assert body["orders"] == 3000 and set(body["claims"]) == {"paid", "withdrawn"}
    assert server.get("/api/recon/runs").json()[0]["id"] == body["run_id"]
    sign_in(server, COFOUNDER)
    assert server.get("/api/audit", params={"action": "recon.run"}).json()


def test_the_cors_policy_names_the_allowed_origin_only(server):
    res = server.options("/api/dashboard", headers={
        "Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"})
    assert res.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert res.headers["access-control-allow-credentials"] == "true"
    evil = server.options("/api/dashboard", headers={
        "Origin": "http://evil.example", "Access-Control-Request-Method": "GET"})
    assert "access-control-allow-origin" not in evil.headers