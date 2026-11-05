"""The review queue API: reading, deciding, undoing, asking the AI, and uploads feeding it."""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import date
from pathlib import Path

import gen_data as g
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from app import auth, demo, importer
from app.ai.llm import LLMClient
from app.api import routes_imports, routes_review
from app.config import get_settings
from app.migrate import migrate
from app.ratelimit import SlidingWindow
from app.recon import matching
from app.recon.engine import run_reconciliation

TODAY = date(2026, 11, 2)
PASSWORD = "correct horse battery"
EXEC, COFOUNDER = "exec@rv.example", "cofounder@rv.example"


@pytest.fixture(scope="module")
def data(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("rv_data")
    g.Generator(path, 1500, seed=7, today=TODAY, use_xlsx=True).run()
    return path


@pytest.fixture(scope="module")
def server(admin_dsn: str, data: Path, tmp_path_factory):
    name = f"recon_test_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    dsn = make_conninfo(**{**conninfo_to_dict(admin_dsn), "dbname": name})
    migrate(dsn)
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        demo.load_dataset(conn, data, echo=False)
        matching.refresh(conn)
        run_reconciliation(conn, TODAY)
        auth.create_user(conn, EXEC, PASSWORD, "Rv Exec", "finance_exec")
        auth.create_user(conn, COFOUNDER, PASSWORD, "Rv Cofounder", "cofounder")
    saved = {k: os.environ.get(k) for k in ("DATABASE_URL", "UPLOAD_DIR", "GROQ_API_KEY")}
    os.environ.update(DATABASE_URL=dsn, GROQ_API_KEY="",
                      UPLOAD_DIR=str(tmp_path_factory.mktemp("rv_uploads")))
    get_settings.cache_clear()
    routes_imports._ai_limit = None
    routes_review._rerank_limit = None
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


def queue(server, **params) -> dict:
    res = server.get("/api/review/queue", params=params)
    assert res.status_code == 200, res.text
    return res.json()


def first_open_with_suggestion(server) -> dict:
    for row in queue(server, limit=200)["rows"]:
        if row["suggestions"]:
            return row
    raise AssertionError("the demo data has no open payment with a suggestion")


# ---- reading -----------------------------------------------------------------------------------


def test_the_queue_needs_a_signed_in_user_and_both_roles_can_read(server):
    server.cookies.clear()
    assert server.get("/api/review/summary").status_code == 401
    sign_in(server, COFOUNDER)
    assert server.get("/api/review/summary").status_code == 200
    assert server.get("/api/review/queue").status_code == 200


def test_the_summary_counts_payments_by_status_confidence_and_ai(server):
    sign_in(server, EXEC)
    s = server.get("/api/review/summary").json()
    assert s["by_status"]["open"]["payments"] > 0 and s["by_status"]["open"]["value_paise"] > 0
    assert set(s["open_by_confidence"]) == {"high", "medium", "low", "none"}
    assert sum(s["open_by_confidence"].values()) == s["by_status"]["open"]["payments"]
    assert sum(s["open_by_ai"].values()) == s["by_status"]["open"]["payments"]
    assert s["recon_stale"] is False


def test_the_queue_pages_filters_and_puts_the_biggest_payments_first(server):
    sign_in(server, EXEC)
    everything = queue(server, limit=200)
    assert everything["total"] >= len(everything["rows"]) > 0
    values = [abs(r["sale_paise"] if r["sale_paise"] is not None else r["net_paise"])
              for r in everything["rows"]]
    assert values == sorted(values, reverse=True)
    first = queue(server, limit=2, offset=0)["rows"]
    second = queue(server, limit=2, offset=2)["rows"]
    assert [r["id"] for r in first + second] == [r["id"] for r in everything["rows"][:4]]
    for conf in ("high", "medium", "low", "none"):
        part = queue(server, confidence=conf, limit=200)
        assert all((r["suggestions"][0]["confidence"] if r["suggestions"] else "none") == conf
                   for r in part["rows"])
    one = everything["rows"][0]["partner_id"]
    mine = queue(server, partner_id=one, limit=200)
    assert mine["rows"] and all(r["partner_id"] == one for r in mine["rows"])
    assert mine["total"] == len(mine["rows"])
    assert server.get("/api/review/queue", params={"status": "weird"}).status_code == 422
    assert server.get("/api/review/queue", params={"limit": 999}).status_code == 422


def test_one_payment_shows_its_lines_suggestions_and_reasons(server):
    sign_in(server, EXEC)
    row = first_open_with_suggestion(server)
    d = server.get(f"/api/review/groups/{row['id']}").json()
    assert d["id"] == row["id"] and d["partner_code"] and d["status"] == "open"
    assert d["lines"]
    first_copies = [line["amount_paise"] for line in d["lines"] if line["dup_seq"] == 0]
    assert sum(first_copies) == d["net_paise"]
    assert d["suggestions"] and d["suggestions"][0]["reasons"]
    assert "line_ids" not in d
    assert server.get("/api/review/groups/99999999").status_code == 404


# ---- deciding ----------------------------------------------------------------------------------


def test_only_the_executive_can_decide_and_it_needs_the_csrf_token(server):
    sign_in(server, EXEC)
    sid = first_open_with_suggestion(server)["suggestions"][0]["id"]
    assert server.post(f"/api/review/suggestions/{sid}/accept").status_code == 403   # no token
    co = sign_in(server, COFOUNDER)
    for path in (f"/api/review/suggestions/{sid}/accept", f"/api/review/suggestions/{sid}/reject",
                 "/api/review/groups/1/undo"):
        assert server.post(path, headers=co).status_code == 403
    assert server.post("/api/review/groups/1/dismiss", headers=co, json={}).status_code == 403
    assert server.post("/api/review/rerank", headers=co, json={}).status_code == 403


def test_accepting_links_the_payment_flags_the_run_as_stale_and_can_be_undone(server):
    ex = sign_in(server, EXEC)
    row = first_open_with_suggestion(server)
    top = row["suggestions"][0]
    before = server.get("/api/review/summary").json()["by_status"]
    done = server.post(f"/api/review/suggestions/{top['id']}/accept", headers=ex)
    assert done.status_code == 200 and done.json()["lines_linked"] >= 1
    assert done.json()["recon_stale"] is True

    detail = server.get(f"/api/review/groups/{row['id']}").json()
    assert detail["status"] == "linked" and detail["linked_order_id"] == top["order_id"]
    assert detail["decided_by"] == "Rv Exec"
    with db(server) as conn:
        linked = conn.execute("SELECT count(*) AS n FROM ledger_lines WHERE order_id = %s "
                              "AND id = ANY(SELECT unnest(line_ids) FROM match_groups "
                              "WHERE id = %s)", (top["order_id"], row["id"])).fetchone()["n"]
    assert linked == done.json()["lines_linked"]
    after = server.get("/api/review/summary").json()
    assert after["by_status"]["open"]["payments"] == before["open"]["payments"] - 1
    assert after["by_status"]["linked"]["payments"] == before["linked"]["payments"] + 1
    assert after["recon_stale"] is True
    assert row["id"] not in [r["id"] for r in queue(server, limit=200)["rows"]]
    assert row["id"] in [r["id"] for r in queue(server, status="linked", limit=200)["rows"]]

    again = server.post(f"/api/review/suggestions/{top['id']}/accept", headers=ex)
    assert again.status_code == 409 and "no longer available" in again.json()["detail"]

    undone = server.post(f"/api/review/groups/{row['id']}/undo", headers=ex)
    assert undone.status_code == 200 and undone.json()["lines_unlinked"] == linked
    assert server.get(f"/api/review/groups/{row['id']}").json()["status"] == "open"
    assert server.post(f"/api/review/groups/{row['id']}/undo", headers=ex).status_code == 409

    run = server.post("/api/recon/run", params={"as_of": TODAY.isoformat()}, headers=ex)
    assert run.status_code == 200
    assert server.get("/api/review/summary").json()["recon_stale"] is False


def test_a_rejected_suggestion_is_never_offered_again(server):
    ex = sign_in(server, EXEC)
    row = first_open_with_suggestion(server)
    top = row["suggestions"][0]
    assert server.post(f"/api/review/suggestions/{top['id']}/reject", headers=ex).status_code == 200
    assert server.post(f"/api/review/suggestions/{top['id']}/reject", headers=ex).status_code == 409
    with db(server) as conn:
        matching.suggest(conn)                                   # the queue is rebuilt
    now = server.get(f"/api/review/groups/{row['id']}").json()
    assert top["order_id"] not in [s["order_id"] for s in now["suggestions"]
                                   if s["status"] in ("pending", "accepted")]
    assert any(s["id"] == top["id"] and s["status"] == "rejected" for s in now["suggestions"])


def test_the_reviewer_can_link_by_hand_and_wrong_choices_are_explained(server):
    ex = sign_in(server, EXEC)
    rows = queue(server, limit=200)["rows"]
    target = next(r for r in rows if r["suggestions"])
    other_partner = next(r for r in rows if r["partner_id"] != target["partner_id"]
                         and r["suggestions"])
    foreign_order = other_partner["suggestions"][0]["order_id"]
    wrong = server.post(f"/api/review/groups/{target['id']}/link", headers=ex,
                        json={"order_id": foreign_order})
    assert wrong.status_code == 409 and "different partner" in wrong.json()["detail"]
    missing = server.post(f"/api/review/groups/{target['id']}/link", headers=ex,
                          json={"order_id": 99999999})
    assert missing.status_code == 404
    nothing = server.post("/api/review/groups/99999999/link", headers=ex, json={"order_id": 1})
    assert nothing.status_code == 404
    ok = server.post(f"/api/review/groups/{target['id']}/link", headers=ex, json={
        "order_id": target["suggestions"][-1]["order_id"], "note": "chosen by hand"})
    assert ok.status_code == 200, ok.text
    d = server.get(f"/api/review/groups/{target['id']}").json()
    assert (d["status"], d["link_method"], d["note"]) == ("linked", "manual", "chosen by hand")
    assert not any(s["status"] == "pending" for s in d["suggestions"])


def test_a_payment_can_be_dismissed_with_a_reason(server):
    ex = sign_in(server, EXEC)
    row = queue(server, limit=200)["rows"][-1]
    ok = server.post(f"/api/review/groups/{row['id']}/dismiss", headers=ex,
                     json={"note": "not our payment"})
    assert ok.status_code == 200
    gone = server.get(f"/api/review/groups/{row['id']}").json()
    assert gone["status"] == "dismissed" and gone["note"] == "not our payment"
    assert server.post(f"/api/review/groups/{row['id']}/dismiss", headers=ex,
                       json={}).status_code == 409
    assert row["id"] in [r["id"] for r in queue(server, status="dismissed", limit=200)["rows"]]


# ---- asking the AI -----------------------------------------------------------------------------


def fake_llm(answer: dict, calls: list | None = None) -> LLMClient:
    def transport(url, headers, body, timeout):
        if calls is not None:
            calls.append(json.loads(body))
        reply = {"choices": [{"message": {"content": json.dumps(answer)},
                              "finish_reason": "stop"}],
                 "usage": {"total_tokens": 100}, "model": "fake-model"}
        return 200, json.dumps(reply)
    return LLMClient("key", "fake-model", "http://fake", transport=transport)


def test_asking_the_ai_without_a_key_says_so_and_changes_nothing(server):
    ex = sign_in(server, EXEC)
    res = server.post("/api/review/rerank", headers=ex, json={})
    assert res.status_code == 503 and "not set up" in res.json()["detail"]


def test_the_ai_adds_an_opinion_but_never_links_or_reorders(server, monkeypatch):
    ex = sign_in(server, EXEC)
    routes_review._rerank_limit = None
    calls: list = []
    monkeypatch.setattr(routes_review, "make_llm",
                        lambda: fake_llm({"choice": 2, "confidence": "medium",
                                          "reason": "dates fit better"}, calls))
    row = next(r for r in queue(server, limit=200)["rows"] if len(r["suggestions"]) >= 2)
    order_before = [s["order_id"] for s in row["suggestions"]]
    res = server.post("/api/review/rerank", headers=ex,
                      json={"group_ids": [row["id"]], "everything": True})
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["asked"] == 1 and out["picked_another"] == 1 and out["tokens"] == 100
    d = server.get(f"/api/review/groups/{row['id']}").json()
    assert d["status"] == "open" and d["ai_verdict"] == "picked" and d["ai_note"]
    pending = [s for s in d["suggestions"] if s["status"] == "pending"]
    assert [s["order_id"] for s in pending] == order_before        # ranking untouched
    assert [s["ai_pick"] for s in pending][:2] == [False, True]
    assert pending[1]["ai_reason"] == "dates fit better"
    again = server.post("/api/review/rerank", headers=ex, json={"group_ids": [row["id"]],
                                                                "everything": True}).json()
    assert again["asked"] == 0 and len(calls) == 1              # answered payments are skipped
    assert "review.rerank" in {a["action"] for a in sign_in_and_audit(server)}


def sign_in_and_audit(server) -> list[dict]:
    sign_in(server, COFOUNDER)
    return server.get("/api/audit", params={"limit": 500}).json()


def test_a_broken_ai_answer_is_recorded_as_an_error_not_a_decision(server, monkeypatch):
    ex = sign_in(server, EXEC)
    routes_review._rerank_limit = None
    monkeypatch.setattr(routes_review, "make_llm", lambda: fake_llm({"choice": 99}))
    row = next(r for r in queue(server, limit=200)["rows"]
               if r["ai_verdict"] is None and r["suggestions"])
    out = server.post("/api/review/rerank", headers=ex,
                      json={"group_ids": [row["id"]], "everything": True}).json()
    assert out["errors"] == 1 and out["picked_another"] == 0
    assert server.get(f"/api/review/groups/{row['id']}").json()["ai_verdict"] == "error"


def test_asking_the_ai_is_limited_per_person_and_to_ten_payments(server, monkeypatch):
    ex = sign_in(server, EXEC)
    monkeypatch.setattr(routes_review, "make_llm", lambda: fake_llm({"choice": None}))
    routes_review._rerank_limit = SlidingWindow(1, 600)
    try:
        assert server.post("/api/review/rerank", headers=ex, json={"limit": 1}).status_code == 200
        limited = server.post("/api/review/rerank", headers=ex, json={"limit": 1})
        assert limited.status_code == 429 and int(limited.headers["retry-after"]) >= 1
        too_many = server.post("/api/review/rerank", headers=ex,
                               json={"group_ids": list(range(1, 12))})
        assert too_many.status_code == 422
    finally:
        routes_review._rerank_limit = None


# ---- uploads feed the queue --------------------------------------------------------------------


def test_an_import_rebuilds_the_review_queue_when_it_finishes(server, data, monkeypatch):
    """A settlement file with no order IDs adds payments to the queue by itself."""
    ex = sign_in(server, EXEC)
    calls = []
    real = matching.refresh
    monkeypatch.setattr(importer.matching, "refresh",
                        lambda conn: calls.append(1) or real(conn))
    rows = (data / "settlement_q1.csv").read_text(encoding="utf-8-sig").splitlines()
    new = "\n".join([rows[0]] + [r.replace(r.split(",")[0], r.split(",")[0] + "Z", 1)
                               for r in rows[1:4]])
    up = server.post("/api/uploads", headers=ex, files={"file": ("tiny.csv", new.encode())})
    assert up.status_code == 201, up.text
    with db(server) as conn:
        q1 = conn.execute("SELECT s.id FROM sources s JOIN partners p ON p.id = s.partner_id "
                          "WHERE p.code = 'Q1' AND s.kind = 'settlement'").fetchone()["id"]
    res = server.post(f"/api/uploads/{up.json()['id']}/import", params={"source_id": q1},
                      headers=ex)
    assert res.status_code == 200
    end = time.time() + 40
    while time.time() < end:
        if server.get(f"/api/imports/{res.json()['import_id']}").json()["status"] == "succeeded":
            break
        time.sleep(0.2)
    assert importer.WORK_LOCK.acquire(timeout=40)               # the worker is done with it
    importer.WORK_LOCK.release()
    assert calls, "the review queue was not rebuilt after the import"