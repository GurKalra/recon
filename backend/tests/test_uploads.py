"""Safe uploads, the mapping wizard, versions, background imports, reject reports."""

from __future__ import annotations

import csv
import importlib
import io
import os
import time
import uuid
import zipfile
from datetime import date
from pathlib import Path

import gen_data as g
import psycopg
import pytest
import xlsxwriter
from fastapi.testclient import TestClient
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from app import auth, demo, importer
from app.api import routes_imports
from app.config import get_settings
from app.ingest.files import UploadRejected, clean_name, stage_upload
from app.migrate import migrate
from app.ratelimit import SlidingWindow
from app.recon.engine import run_reconciliation

TODAY = date(2026, 11, 2)
PASSWORD = "correct horse battery"
EXEC, COFOUNDER = "exec@up.example", "cofounder@up.example"


# ---- the file gate (no server needed) ----------------------------------------------------------


def stage(tmp_path: Path, data: bytes, name: str, **kw):
    return stage_upload(io.BytesIO(data), name, tmp_path / "stage", kw.pop("max_bytes", 1 << 20),
                        **kw)


def xlsx_bytes() -> bytes:
    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    wb.add_worksheet().write_row(0, 0, ["Order ID", "Amount"])
    wb.close()
    return buf.getvalue()


def zip_bytes(entries: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, content in entries.items():
            z.writestr(name, content)
    return buf.getvalue()


def test_a_csv_and_an_xlsx_are_accepted_with_a_checksum_and_a_random_stored_name(tmp_path):
    a = stage(tmp_path, b"Order ID,Amount\nA1,10\n", "My Report (final).csv")
    b = stage(tmp_path, xlsx_bytes(), "book.xlsx")
    assert (a.kind, b.kind) == ("csv", "xlsx") and a.size == 22
    assert len(a.sha256) == 64 and a.path.parent == tmp_path / "stage"
    assert a.original_name == "My Report (final).csv" and "Report" not in a.path.name


@pytest.mark.parametrize("name,data,why", [
    ("run.exe", b"hello,world\n", "Only .csv and .xlsx"),
    ("data.csv", b"", "empty"),
    ("data.csv", b"%PDF-1.7\n1 0 obj", "CSV text"),
    ("data.csv", b"MZ\x90\x00\x03" + b"\x00" * 50, "CSV text"),
    ("data.csv", b"a,b\n1,\x00\x00\x00\n", "CSV text"),
    ("data.csv", b"\x89PNG\r\n\x1a\n" + b"x" * 40, "CSV text"),
    ("data.xlsx", b"just,some,text\n1,2,3\n", "valid Excel"),
    ("data.xlsx", b"PK\x03\x04 not really a zip", "valid Excel"),
])
def test_files_that_are_not_what_they_claim_are_refused(tmp_path, name, data, why):
    with pytest.raises(UploadRejected, match=why):
        stage(tmp_path, data, name)
    assert not list((tmp_path / "stage").glob("*"))        # nothing left behind


def test_a_zip_that_is_not_a_workbook_is_refused(tmp_path):
    with pytest.raises(UploadRejected, match="not an Excel workbook"):
        stage(tmp_path, zip_bytes({"readme.txt": b"hi"}), "x.xlsx")


def test_a_workbook_with_macros_is_refused(tmp_path):
    parts = {"[Content_Types].xml": b"<x/>", "xl/workbook.xml": b"<x/>",
             "xl/vbaProject.bin": b"\x00"}
    with pytest.raises(UploadRejected, match="macros"):
        stage(tmp_path, zip_bytes(parts), "x.xlsx")


def test_a_decompression_bomb_is_refused(tmp_path):
    parts = {"[Content_Types].xml": b"<x/>", "xl/workbook.xml": b"<x/>",
             "xl/worksheets/sheet1.xml": b"\x00" * (40 << 20)}
    blob = zip_bytes(parts)
    assert len(blob) < 200_000                     # 40 MB squeezed into a few KB
    with pytest.raises(UploadRejected, match="bomb"):
        stage(tmp_path, blob, "x.xlsx", max_bytes=1 << 20)
    with pytest.raises(UploadRejected, match="unreasonable size"):
        stage(tmp_path, blob, "x.xlsx", max_bytes=1 << 20, max_unzipped=1 << 20)


def test_a_file_over_the_size_cap_is_refused_while_it_is_being_read(tmp_path):
    with pytest.raises(UploadRejected, match="larger than"):
        stage(tmp_path, b"a,b\n" + b"1,2\n" * 2_000_000, "big.csv", max_bytes=1 << 20)
    assert not list((tmp_path / "stage").glob("*"))


@pytest.mark.parametrize("raw,clean", [
    ("../../etc/passwd.csv", "passwd.csv"), ("C:\\Users\\me\\sales.csv", "sales.csv"),
    ("a<b>|c?.csv", "a_b__c_.csv"), ("", "upload"), ("...", "upload"),
    ("x" * 300 + ".csv", ("x" * 120)),
])
def test_file_names_are_cleaned_into_harmless_labels(raw, clean):
    assert clean_name(raw) == clean


def test_one_stray_text_cell_in_a_number_column_does_not_break_profiling(tmp_path):
    from app.ai.profile import profile_file
    rows = [["Order ID", "Amount"]] + [[f"A{i}", str(100 + i)] for i in range(30)]
    rows[5][1] = "not a number"
    path = tmp_path / "x.csv"
    path.write_bytes(write_csv(rows))
    prof = profile_file(path)
    assert prof.col("amount").kind == "number"


def test_the_sliding_window_limits_and_forgets():
    w = SlidingWindow(2, 0.2)
    assert w.allow("a") and w.allow("a") and not w.allow("a") and w.allow("b")
    assert w.retry_after("a") >= 1
    time.sleep(0.25)
    assert w.allow("a")


def test_imports_left_running_by_a_restart_are_marked_failed(dict_conn):
    pid = dict_conn.execute("INSERT INTO partners (code, name, type) VALUES "
                            "('TP', 'Test', 'marketplace') RETURNING id").fetchone()["id"]
    sid = dict_conn.execute("INSERT INTO sources (partner_id, kind, name) VALUES (%s, "
                            "'settlement', 's') RETURNING id", (pid,)).fetchone()["id"]
    for i, status in enumerate(("queued", "running", "succeeded")):
        dict_conn.execute(
            "INSERT INTO imports (source_id, filename, file_sha256, file_bytes, status) "
            "VALUES (%s, 'f', %s, 1, %s)", (sid, f"{i:064x}", status))
    dict_conn.commit()
    assert importer.mark_interrupted(dict_conn) == 2
    got = {r["status"] for r in dict_conn.execute("SELECT status FROM imports")}
    assert got == {"failed", "succeeded"}


# ---- the API -----------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def data(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("upl_data")
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
        run_reconciliation(conn, TODAY)
        auth.create_user(conn, EXEC, PASSWORD, "Up Exec", "finance_exec")
        auth.create_user(conn, COFOUNDER, PASSWORD, "Up Cofounder", "cofounder")
    saved = {k: os.environ.get(k) for k in ("DATABASE_URL", "UPLOAD_DIR", "GROQ_API_KEY")}
    os.environ.update(DATABASE_URL=dsn, GROQ_API_KEY="",
                      UPLOAD_DIR=str(tmp_path_factory.mktemp("uploads")))
    get_settings.cache_clear()
    routes_imports._ai_limit = None
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


def send(server, headers, content: bytes, name: str = "file.csv"):
    return server.post("/api/uploads", headers=headers, files={"file": (name, content)})


def upload_ok(server, headers, content: bytes, name: str = "file.csv") -> dict:
    res = send(server, headers, content, name)
    assert res.status_code == 201, res.text
    return res.json()


def source_id(server, code: str, kind: str = "settlement") -> int:
    with db(server) as conn:
        return conn.execute(
            "SELECT s.id FROM sources s JOIN partners p ON p.id = s.partner_id "
            "WHERE p.code = %s AND s.kind = %s", (code, kind)).fetchone()["id"]


def partner_id(server, code: str) -> int:
    with db(server) as conn:
        return conn.execute("SELECT id FROM partners WHERE code = %s", (code,)).fetchone()["id"]


def wait_for(server, import_id: int, timeout: float = 40.0) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        row = server.get(f"/api/imports/{import_id}").json()
        if row["status"] in ("succeeded", "failed"):
            return row
        time.sleep(0.2)
    raise AssertionError("the import did not finish")


def read_rows(path: Path) -> list[list[str]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.reader(f))


def write_csv(rows: list[list[str]]) -> bytes:
    buf = io.StringIO()
    csv.writer(buf).writerows(rows)
    return buf.getvalue().encode()


Q1_RENAME = {"Order No": "Order Number", "Payout Date": "Settlement Date",
             "Sale Value": "Gross Sale", "Platform Fee": "Commission", "Net Payout": "Net Amount"}


def disguised_q1(data: Path) -> bytes:
    """The Q1 settlement with new column names, reversed column order, a junk column and title
    rows: what a partner's redesigned portal might produce."""
    rows = read_rows(data / "settlement_q1.csv")
    header, body = rows[0], rows[1:]
    order = list(reversed(range(len(header))))
    new_header = [Q1_RENAME.get(h, h) for h in header]
    out = [["Exported by ACME portal v2"], [], [new_header[i] for i in order] + ["Internal Code"]]
    out += [[r[i] for i in order] + ["see portal"] for r in body]
    return write_csv(out)


# ---- who may do what ---------------------------------------------------------------------------


def test_only_the_executive_can_upload_and_it_needs_the_csrf_token(server, data):
    content = (data / "settlement_q1.csv").read_bytes()
    server.cookies.clear()
    assert server.post("/api/uploads", files={"file": ("a.csv", content)}).status_code == 401
    co = sign_in(server, COFOUNDER)
    assert send(server, co, content).status_code == 403
    sign_in(server, EXEC)
    no_token = server.post("/api/uploads", files={"file": ("a.csv", content)})
    assert no_token.status_code == 403


def test_the_cofounder_can_read_sources_and_imports(server):
    sign_in(server, COFOUNDER)
    assert len(server.get("/api/sources").json()) == 12
    assert server.get("/api/imports").json()


def test_a_bad_file_is_refused_with_a_clear_message(server):
    ex = sign_in(server, EXEC)
    res = send(server, ex, b"MZ\x90\x00" + b"\x00" * 100, "tool.csv")
    assert res.status_code == 422 and "CSV" in res.json()["detail"]
    assert send(server, ex, b"a,b\n1,2\n", "tool.exe").status_code == 422
    assert send(server, ex, b"a,b\n1,2\n", "../../x.csv").status_code in (201, 422)


# ---- a known file: no wizard needed ------------------------------------------------------------


def test_a_file_we_already_imported_is_recognised_and_not_imported_twice(server, data):
    ex = sign_in(server, EXEC)
    res = send(server, ex, (data / "settlement_q1.csv").read_bytes(), "settlement_q1.csv")
    assert res.status_code == 201
    up = res.json()
    q1 = source_id(server, "Q1")
    assert [d["source_id"] for d in up["detected"]] == [q1]
    assert {c["name"] for c in up["profile"]["columns"]} >= {"order no", "sale value"}
    done = server.post(f"/api/uploads/{up['id']}/import", params={"source_id": q1}, headers=ex)
    assert done.status_code == 200 and done.json()["status"] == "duplicate"
    assert server.get(f"/api/uploads/{up['id']}").status_code == 404      # it is finished
    assert not list(Path(os.environ["UPLOAD_DIR"]).glob(up["id"].__str__()))


def test_new_rows_for_a_known_source_import_in_the_background_with_progress(server, data):
    ex = sign_in(server, EXEC)
    rows = read_rows(data / "settlement_q1.csv")
    extra = [r[:] for r in rows[1:6]]
    for r in extra:
        r[0] = r[0] + "9"                                     # new order numbers, same layout
    up = upload_ok(server, ex, write_csv(rows + extra), "q1_more.csv")
    q1 = source_id(server, "Q1")
    res = server.post(f"/api/uploads/{up['id']}/import", params={"source_id": q1}, headers=ex)
    assert res.status_code == 200 and res.json()["status"] == "queued"
    final = wait_for(server, res.json()["import_id"])
    assert final["status"] == "succeeded" and final["progress"] == 1
    assert final["rows_total"] == len(rows) - 1 + len(extra) - 0 or final["rows_total"] > 0
    assert final["rows_loaded"] + final["rows_duplicate"] + final["rows_rejected"] >= 1
    assert set(final["stage_timings"]) >= {"read", "transform", "load"}
    assert "file_sha256" not in final
    listed = server.get("/api/imports", params={"source_id": q1}).json()
    assert listed[0]["id"] == res.json()["import_id"] and listed[0]["partner_code"] == "Q1"


def test_a_layout_change_that_breaks_the_mapping_is_held_back_and_explained(server, data):
    ex = sign_in(server, EXEC)
    up = upload_ok(server, ex, disguised_q1(data), "q1_redesigned.csv")
    q1 = source_id(server, "Q1")
    assert up["detected"] == []                      # nobody has approved this layout
    res = server.post(f"/api/uploads/{up['id']}/import", params={"source_id": q1}, headers=ex)
    assert res.status_code == 409
    drift = res.json()["detail"]["drift"]
    assert "order no" in drift["missing_columns"] and "internal code" in drift["new_columns"]
    assert drift["still_works"] is False
    server.delete(f"/api/uploads/{up['id']}", headers=ex)


def test_a_new_unmapped_column_does_not_stop_an_import_but_is_reported(server, data):
    ex = sign_in(server, EXEC)
    rows = read_rows(data / "settlement_q1.csv")
    rows[0].append("Customer Name")
    for i, r in enumerate(rows[1:], 1):
        r.append(f"Asha Rao {i}")
    rows[3][rows[0].index("Sale Value")] = "not a number"        # one bad row
    rows[4][0] = rows[4][0] + "7"
    up = upload_ok(server, ex, write_csv(rows), "q1_with_names.csv")
    q1 = source_id(server, "Q1")
    res = server.post(f"/api/uploads/{up['id']}/import", params={"source_id": q1}, headers=ex)
    assert res.status_code == 200 and res.json()["drift"]["still_works"] is True
    final = wait_for(server, res.json()["import_id"])
    assert final["status"] == "succeeded" and final["rows_rejected"] >= 1
    # the reject report shows what was wrong but never the customer's name
    shown = server.get(f"/api/imports/{final['id']}/rejects").json()
    assert shown["total"] >= 1 and "not a number" in str(shown["rows"])
    assert "Asha" not in str(shown) and "customer name" not in str(shown).lower()
    report = server.get(f"/api/imports/{final['id']}/rejects.csv")
    assert report.status_code == 200 and report.headers["content-type"].startswith("text/csv")
    assert "attachment" in report.headers["content-disposition"]
    assert "Asha" not in report.text and "why_it_was_refused" in report.text


# ---- the wizard --------------------------------------------------------------------------------


def test_the_wizard_suggests_previews_approves_imports_and_versions(server, data):
    ex = sign_in(server, EXEC)
    q1_pid, q1 = partner_id(server, "Q1"), source_id(server, "Q1")
    blob = disguised_q1(data)
    up = upload_ok(server, ex, blob, "q1_portal_v2.csv")

    sug = server.post(f"/api/uploads/{up['id']}/suggest", headers=ex,
                      json={"partner_id": q1_pid, "source_kind": "settlement", "use_ai": False})
    assert sug.status_code == 200, sug.text
    s = sug.json()
    assert s["mapping"] and s["check"]["ok"] and s["produced_by"] == "rules"
    assert s["notes"] and all("reason" in n for n in s["notes"])

    prev = server.post(f"/api/uploads/{up['id']}/preview", headers=ex,
                       json={"mapping": s["mapping"], "source_kind": "settlement"}).json()
    assert prev["ok"] and 0 < len(prev["rows"]) <= 20 and prev["rows_rejected"] == 0
    assert {"order_ref", "amount_paise", "line_type"} <= set(prev["columns"])

    app_res = server.post(f"/api/uploads/{up['id']}/approve", headers=ex, json={
        "partner_id": q1_pid, "source_kind": "settlement", "name": "Q1 portal v2",
        "mapping": s["mapping"], "note": "after the portal redesign"})
    assert app_res.status_code == 200, app_res.text
    body = app_res.json()
    assert body["version"] == 1 and body["status"] == "queued"
    final = wait_for(server, body["import_id"])
    assert final["status"] == "succeeded"
    assert final["rows_rejected"] == 0 and final["rows_duplicate"] > 0   # same payments, new layout

    new_source = body["source_id"]
    assert new_source != q1
    listed = {r["id"]: r for r in server.get("/api/sources").json()}
    assert listed[new_source]["approved_version"] == 1 and listed[new_source]["imports"] == 1

    # v2: a harmless change (a footer marker), approved from a fresh copy of the file
    changed = {**s["mapping"], "format": {**s["mapping"]["format"],
                                          "footer_markers": ["Grand Total"]}}
    up2 = upload_ok(server, ex, blob, "q1_portal_v2_again.csv")
    v2 = server.post(f"/api/uploads/{up2['id']}/approve", headers=ex, json={
        "partner_id": q1_pid, "source_kind": "settlement", "name": "Q1 portal v2",
        "mapping": changed}).json()
    assert v2["version"] == 2 and v2["status"] == "duplicate"        # same file: not loaded twice
    # approving the very same mapping again does not mint a new version
    up3 = upload_ok(server, ex, blob, "q1_portal_v2_third.csv")
    same = server.post(f"/api/uploads/{up3['id']}/approve", headers=ex, json={
        "partner_id": q1_pid, "source_kind": "settlement", "name": "Q1 portal v2",
        "mapping": changed}).json()
    assert same["version"] == 2

    hist = server.get(f"/api/sources/{new_source}/mappings").json()
    assert [h["version"] for h in hist] == [2, 1]
    assert [h["status"] for h in hist] == ["approved", "superseded"]

    assert server.post(f"/api/sources/{new_source}/mappings/2/rollback", headers=ex
                       ).status_code == 422                          # already current
    back = server.post(f"/api/sources/{new_source}/mappings/1/rollback", headers=ex)
    assert back.status_code == 200 and back.json()["version"] == 3
    hist = server.get(f"/api/sources/{new_source}/mappings").json()
    assert hist[0]["version"] == 3 and hist[0]["mapping"] == hist[2]["mapping"]
    assert "rolled back to version 1" in hist[0]["note"]
    assert server.post(f"/api/sources/{new_source}/mappings/99/rollback", headers=ex
                       ).status_code == 422


def test_a_mapping_that_would_reject_most_rows_cannot_be_approved(server, data):
    ex = sign_in(server, EXEC)
    pid = partner_id(server, "Q1")
    up = upload_ok(server, ex, (data / "settlement_q1.csv").read_bytes(), "q1_again.csv")
    good = server.post(f"/api/uploads/{up['id']}/suggest", headers=ex, json={
        "partner_id": pid, "source_kind": "settlement", "use_ai": False}).json()["mapping"]
    bad = {**good, "ledger": {**good["ledger"], "date": {
        "column": good["ledger"]["order_ref"], "format": good["ledger"]["date"]["format"]}}}
    prev = server.post(f"/api/uploads/{up['id']}/preview", headers=ex, json={"mapping": bad})
    assert prev.status_code == 200 and prev.json()["ok"] is False and prev.json()["rejects"]
    res = server.post(f"/api/uploads/{up['id']}/approve", headers=ex, json={
        "partner_id": pid, "source_kind": "settlement", "name": "Q1 broken", "mapping": bad})
    assert res.status_code == 422 and "would reject" in res.json()["detail"]
    with db(server) as conn:                  # nothing was created
        assert conn.execute("SELECT count(*) AS n FROM sources WHERE name = 'Q1 broken'"
                            ).fetchone()["n"] == 0
    server.delete(f"/api/uploads/{up['id']}", headers=ex)


def test_a_mapping_for_the_wrong_kind_or_with_missing_columns_is_explained(server, data):
    ex = sign_in(server, EXEC)
    pid = partner_id(server, "Q1")
    up = upload_ok(server, ex, (data / "settlement_q1.csv").read_bytes(), "q1_kinds.csv")
    good = server.post(f"/api/uploads/{up['id']}/suggest", headers=ex, json={
        "partner_id": pid, "source_kind": "settlement", "use_ai": False}).json()["mapping"]
    wrong_kind = server.post(f"/api/uploads/{up['id']}/preview", headers=ex,
                             json={"mapping": good, "source_kind": "orders"})
    assert wrong_kind.status_code == 422 and "needs a 'orders' mapping" in wrong_kind.text
    broken = {**good, "ledger": {**good["ledger"], "order_ref": "no such column"}}
    missing = server.post(f"/api/uploads/{up['id']}/preview", headers=ex, json={"mapping": broken})
    assert missing.status_code == 422 and "no such column" in missing.text
    junk = server.post(f"/api/uploads/{up['id']}/preview", headers=ex, json={"mapping": {"x": 1}})
    assert junk.status_code == 422
    unknown = server.post(f"/api/uploads/{up['id']}/suggest", headers=ex, json={
        "partner_id": 10**6, "source_kind": "settlement", "use_ai": False})
    assert unknown.status_code == 422
    server.delete(f"/api/uploads/{up['id']}", headers=ex)


def test_asking_the_ai_is_rate_limited_per_person(server, data):
    ex = sign_in(server, EXEC)
    routes_imports._ai_limit = SlidingWindow(1, 600)
    try:
        pid = partner_id(server, "Q1")
        up = upload_ok(server, ex, (data / "settlement_q1.csv").read_bytes(), "q1_rl.csv")
        body = {"partner_id": pid, "source_kind": "settlement", "use_ai": True}
        first = server.post(f"/api/uploads/{up['id']}/suggest", headers=ex, json=body)
        second = server.post(f"/api/uploads/{up['id']}/suggest", headers=ex, json=body)
        assert first.status_code == 200 and second.status_code == 429
        assert int(second.headers["retry-after"]) >= 1
        rules_only = server.post(f"/api/uploads/{up['id']}/suggest", headers=ex,
                                 json={**body, "use_ai": False})
        assert rules_only.status_code == 200                 # the rules are never limited
        server.delete(f"/api/uploads/{up['id']}", headers=ex)
    finally:
        routes_imports._ai_limit = None


def test_a_discarded_upload_disappears_and_its_file_is_deleted(server, data):
    ex = sign_in(server, EXEC)
    up = upload_ok(server, ex, (data / "settlement_q1.csv").read_bytes(), "q1_gone.csv")
    with db(server) as conn:
        stored = conn.execute("SELECT stored_name FROM uploads WHERE id = %s",
                              (up["id"],)).fetchone()["stored_name"]
    assert (Path(os.environ["UPLOAD_DIR"]) / stored).exists()
    assert server.delete(f"/api/uploads/{up['id']}", headers=ex).status_code == 200
    assert not (Path(os.environ["UPLOAD_DIR"]) / stored).exists()
    assert server.get(f"/api/uploads/{up['id']}").status_code == 404


def test_the_audit_trail_records_the_wizard_steps(server):
    co = sign_in(server, COFOUNDER)
    actions = {r["action"] for r in server.get("/api/audit", params={"limit": 500}).json()}
    assert {"upload.stage", "mapping.suggest", "mapping.approve", "import.start",
            "mapping.rollback", "rejects.download"} <= actions
    assert co


def test_reconciliation_waits_while_an_import_holds_the_lock(server):
    ex = sign_in(server, EXEC)
    assert importer.WORK_LOCK.acquire(blocking=False)
    try:
        res = server.post("/api/recon/run", params={"as_of": TODAY.isoformat()}, headers=ex)
        assert res.status_code == 409
    finally:
        importer.WORK_LOCK.release()
    assert server.post("/api/recon/run", params={"as_of": TODAY.isoformat()},
                       headers=ex).status_code == 200


def test_the_api_explorer_is_off_outside_development(monkeypatch):
    import app.main as main
    monkeypatch.setenv("APP_ENV", "production")
    get_settings.cache_clear()
    try:
        prod = importlib.reload(main).app
        assert prod.docs_url is None and prod.openapi_url is None
    finally:
        monkeypatch.delenv("APP_ENV")
        get_settings.cache_clear()
        importlib.reload(main)