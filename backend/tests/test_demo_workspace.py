"""The demo workspace: one click fills an empty workspace, never touches a full one, and the
sample files can be downloaded to try the wizard by hand."""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from app import auth, importer
from app.config import get_settings
from app.migrate import migrate

PASSWORD = "correct horse battery"
EXEC, COFOUNDER = "exec@dm.example", "cofounder@dm.example"
SAMPLES = Path(__file__).resolve().parents[2] / "sample_data" / "small"


@pytest.fixture(scope="module")
def server(admin_dsn: str, tmp_path_factory):
    if not (SAMPLES / "manifest.json").exists():
        pytest.skip("sample_data/small is not present")
    name = f"recon_test_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    dsn = make_conninfo(**{**conninfo_to_dict(admin_dsn), "dbname": name})
    migrate(dsn)
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        auth.create_user(conn, EXEC, PASSWORD, "Dm Exec", "finance_exec")
        auth.create_user(conn, COFOUNDER, PASSWORD, "Dm Cofounder", "cofounder")
    saved = {k: os.environ.get(k) for k in ("DATABASE_URL", "SAMPLE_DATA_DIR", "GROQ_API_KEY")}
    os.environ.update(DATABASE_URL=dsn, GROQ_API_KEY="", SAMPLE_DATA_DIR=str(SAMPLES))
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


def sign_in(server, email: str) -> dict:
    server.cookies.clear()
    res = server.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert res.status_code == 200, res.text
    return {"X-CSRF-Token": res.json()["csrf_token"]}


def test_status_lists_the_twelve_files_and_never_the_answer_key(server):
    server.cookies.clear()
    assert server.get("/api/demo/status").status_code == 401
    sign_in(server, COFOUNDER)
    s = server.get("/api/demo/status").json()
    assert s["loaded"] is False and s["available"] is True and s["as_of"] == "2026-11-02"
    names = {f["name"] for f in s["files"]}
    assert len(names) == 12 and "orders.csv" in names and "settlement_m1.xlsx" in names
    assert "truth.csv" not in names and "manifest.json" not in names


def test_only_the_executive_can_load_the_demo(server):
    co = sign_in(server, COFOUNDER)
    assert server.post("/api/demo/load", headers=co).status_code == 403
    sign_in(server, EXEC)
    assert server.post("/api/demo/load").status_code == 403           # no CSRF token


def test_sample_files_download_only_from_the_list(server):
    sign_in(server, COFOUNDER)
    ok = server.get("/api/demo/files/settlement_q1.csv")
    assert ok.status_code == 200 and "attachment" in ok.headers["content-disposition"]
    assert ok.content == (SAMPLES / "settlement_q1.csv").read_bytes()
    for bad in ("truth.csv", "manifest.json", "..%2Fsecret.csv", "nope.csv"):
        assert server.get(f"/api/demo/files/{bad}").status_code == 404


def test_loading_fills_an_empty_workspace_and_the_screens_have_data(server):
    ex = sign_in(server, EXEC)
    res = server.post("/api/demo/load", headers=ex)
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["orders"] == 3000 and out["as_of"] == "2026-11-02"
    assert out["money_at_risk_paise"] > 0 and out["findings_open"] > 0
    assert out["payments_to_review"] > 0 and out["ledger_lines"] > 0

    assert server.get("/api/demo/status").json()["loaded"] is True
    dash = server.get("/api/dashboard", params={"as_of": out["as_of"]}).json()
    assert dash["headline"]["money_at_risk_paise"] == out["money_at_risk_paise"]
    assert server.get("/api/review/summary").json()["by_status"]["open"]["payments"] > 0
    assert server.get("/api/orders", params={"limit": 5}).json()["total"] == 3000
    assert server.get("/api/recon/status").json()["stale"] is False
    assert len(server.get("/api/sources").json()) == 12
    audit = {a["action"] for a in (sign_in(server, COFOUNDER) and
                                   server.get("/api/audit", params={"limit": 50}).json())}
    assert "demo.load" in audit


def test_loading_twice_is_refused_and_changes_nothing(server):
    ex = sign_in(server, EXEC)
    with psycopg.connect(server.dsn, row_factory=dict_row) as conn:
        before = conn.execute("SELECT count(*) AS n FROM ledger_lines").fetchone()["n"]
    again = server.post("/api/demo/load", headers=ex)
    assert again.status_code == 409 and "already has data" in again.json()["detail"]
    with psycopg.connect(server.dsn, row_factory=dict_row) as conn:
        assert conn.execute("SELECT count(*) AS n FROM ledger_lines").fetchone()["n"] == before


def test_a_downloaded_sample_file_is_recognised_by_the_upload_wizard(server):
    ex = sign_in(server, EXEC)
    blob = server.get("/api/demo/files/settlement_q1.csv").content
    up = server.post("/api/uploads", headers=ex, files={"file": ("settlement_q1.csv", blob)})
    assert up.status_code == 201, up.text
    assert [d["partner_code"] for d in up.json()["detected"]] == ["Q1"]
    server.delete(f"/api/uploads/{up.json()['id']}", headers=ex)


def test_a_busy_server_says_so_instead_of_loading(server, tmp_path):
    ex = sign_in(server, EXEC)
    assert importer.WORK_LOCK.acquire(blocking=False)
    try:
        assert server.post("/api/demo/load", headers=ex).status_code == 409
    finally:
        importer.WORK_LOCK.release()


def test_missing_sample_files_are_reported_not_crashed(server, monkeypatch, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    shutil.copy(SAMPLES / "manifest.json", empty / "manifest.json")
    monkeypatch.setenv("SAMPLE_DATA_DIR", str(empty))
    get_settings.cache_clear()
    try:
        sign_in(server, COFOUNDER)
        s = server.get("/api/demo/status").json()
        assert s["available"] is False and s["files"] == []
    finally:
        monkeypatch.setenv("SAMPLE_DATA_DIR", str(SAMPLES))
        get_settings.cache_clear()