"""The upload wizard and everything about imports: stage a file, suggest / preview / approve a
mapping, run the import in the background, watch it, read the rejects, manage mapping versions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, HTTPException, Query, Request, Response, UploadFile
from pydantic import BaseModel, Field

from app import audit, mappings
from app.ai.mask import is_sensitive_header
from app.ai.profile import profile_file
from app.api.deps import Anyone, Conn, Exec, client_ip
from app.claim_evidence import csv_bytes
from app.config import get_settings
from app.importer import NoApprovedMapping
from app.ingest.files import UploadRejected, stage_upload
from app.ingest.reader import FileReadError
from app.ratelimit import SlidingWindow

router = APIRouter(prefix="/api", tags=["imports"])
_ai_limit: SlidingWindow | None = None
_STALE_HOURS = 24


def ai_limiter() -> SlidingWindow:
    global _ai_limit
    if _ai_limit is None:
        _ai_limit = SlidingWindow(get_settings().ai_suggestions_per_10min, 600)
    return _ai_limit


class SuggestBody(BaseModel):
    partner_id: int
    source_kind: str
    use_ai: bool = True


class PreviewBody(BaseModel):
    mapping: dict
    source_kind: str | None = None


class ApproveBody(BaseModel):
    partner_id: int
    source_kind: str
    name: str = Field(min_length=1, max_length=80)
    mapping: dict
    note: str | None = Field(default=None, max_length=300)


def _upload(conn, upload_id: int) -> dict:
    row = conn.execute("SELECT * FROM uploads WHERE id = %s", (upload_id,)).fetchone()
    if row is None or row["status"] != "staged":
        raise HTTPException(404, "No such upload (it may have been imported or discarded).")
    return row


def _path(row: dict) -> Path:
    path = Path(get_settings().upload_dir) / row["stored_name"]
    if not path.exists():
        raise HTTPException(410, "The uploaded file is no longer available. Upload it again.")
    return path


def _public(row: dict) -> dict:
    return {k: row[k] for k in ("id", "filename", "kind", "file_bytes", "file_sha256", "status",
                                "profile", "detected", "created_at", "import_id")}


def _sweep_stale(conn, upload_dir: Path) -> None:
    """Files nobody went on to import are removed after a day."""
    rows = conn.execute(
        "UPDATE uploads SET status = 'discarded' WHERE status = 'staged' "
        "AND created_at < now() - make_interval(hours => %s) RETURNING stored_name",
        (_STALE_HOURS,)).fetchall()
    for r in rows:
        (upload_dir / r["stored_name"]).unlink(missing_ok=True)
    conn.commit()


# ---- staging a file ----------------------------------------------------------------------------


@router.post("/uploads", status_code=201)
def upload(request: Request, conn: Conn, user: Exec,
           file: Annotated[UploadFile, File()]) -> dict:
    settings = get_settings()
    cap = settings.max_upload_mb << 20
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > cap + (1 << 20):
        raise HTTPException(413, f"The file is larger than {settings.max_upload_mb} MB.")
    upload_dir = Path(settings.upload_dir)
    _sweep_stale(conn, upload_dir)
    try:
        staged = stage_upload(file.file, file.filename or "", upload_dir, cap,
                              settings.max_unzipped_mb << 20)
    except UploadRejected as exc:
        raise HTTPException(422, str(exc)) from exc
    try:
        profile = profile_file(staged.path)
    except FileReadError as exc:
        staged.path.unlink(missing_ok=True)
        raise HTTPException(422, str(exc)) from exc
    except Exception as exc:
        staged.path.unlink(missing_ok=True)
        raise HTTPException(422, "That file could not be read as a table.") from exc
    summary = mappings.profile_summary(profile)
    detected = mappings.detect_sources(conn, profile.table.fingerprint)
    row = conn.execute(
        """INSERT INTO uploads (filename, stored_name, kind, file_bytes, file_sha256, profile,
                                detected, created_by)
           VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s) RETURNING *""",
        (staged.original_name, staged.path.name, staged.kind, staged.size, staged.sha256,
         json.dumps(summary), json.dumps(detected, default=str), user["id"])).fetchone()
    conn.commit()
    audit.record(conn, user["id"], "upload.stage", "upload", row["id"],
                 {"file": staged.original_name, "bytes": staged.size,
                  "detected": [d["source_id"] for d in detected]}, client_ip(request))
    return _public(row)


@router.get("/uploads/{upload_id}")
def get_upload(upload_id: int, conn: Conn, _: Exec) -> dict:
    return _public(_upload(conn, upload_id))


@router.delete("/uploads/{upload_id}")
def discard_upload(upload_id: int, request: Request, conn: Conn, user: Exec) -> dict:
    row = _upload(conn, upload_id)
    (Path(get_settings().upload_dir) / row["stored_name"]).unlink(missing_ok=True)
    conn.execute("UPDATE uploads SET status = 'discarded' WHERE id = %s", (upload_id,))
    conn.commit()
    audit.record(conn, user["id"], "upload.discard", "upload", upload_id, ip=client_ip(request))
    return {"ok": True}


# ---- the mapping wizard ------------------------------------------------------------------------


@router.post("/uploads/{upload_id}/suggest")
def suggest(upload_id: int, body: SuggestBody, request: Request, conn: Conn, user: Exec) -> dict:
    row = _upload(conn, upload_id)
    path = _path(row)
    if body.use_ai and not ai_limiter().allow(user["id"]):
        raise HTTPException(429, "Too many AI suggestions. Try again shortly, or use the rules "
                                 "only.", headers={"Retry-After": str(
                                     ai_limiter().retry_after(user["id"]))})
    try:
        result = mappings.suggest(conn, path, body.partner_id, body.source_kind, body.use_ai)
    except mappings.MappingInvalid as exc:
        raise HTTPException(422, str(exc)) from exc
    except FileReadError as exc:
        raise HTTPException(422, str(exc)) from exc
    audit.record(conn, user["id"], "mapping.suggest", "upload", upload_id,
                 {"produced_by": result["produced_by"], "model": result["model"],
                  "tokens": result["tokens"], "ok": result["check"]["ok"]}, client_ip(request))
    return result


@router.post("/uploads/{upload_id}/preview")
def preview(upload_id: int, body: PreviewBody, conn: Conn, _: Exec) -> dict:
    path = _path(_upload(conn, upload_id))
    try:
        return mappings.preview(path, body.mapping, mappings.partner_types(conn),
                                body.source_kind)
    except mappings.MappingInvalid as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/uploads/{upload_id}/approve")
def approve(upload_id: int, body: ApproveBody, request: Request, conn: Conn,
            user: Exec) -> dict:
    """A person approves the mapping; it is saved as a new version and the file is imported."""
    row = _upload(conn, upload_id)
    path = _path(row)
    try:
        done = mappings.approve(conn, user["id"], path, body.partner_id, body.source_kind,
                                body.name, body.mapping, mappings.partner_types(conn), body.note)
    except mappings.MappingInvalid as exc:
        raise HTTPException(422, str(exc)) from exc
    audit.record(conn, user["id"], "mapping.approve", "source", done.source_id,
                 {"version": done.version, "new_version": done.created, "upload": upload_id},
                 client_ip(request))
    return _start_import(request, conn, user, done.source_id, row, path) \
        | {"source_id": done.source_id, "version": done.version}


@router.post("/uploads/{upload_id}/import")
def import_known(upload_id: int, source_id: Annotated[int, Query()], request: Request,
                 conn: Conn, user: Exec) -> dict:
    """Import a file for a source that already has an approved mapping (no wizard). If the
    partner changed their layout, nothing is imported and the change is described."""
    row = _upload(conn, upload_id)
    path = _path(row)
    try:
        change = mappings.drift(conn, source_id, path)
    except mappings.MappingInvalid as exc:
        raise HTTPException(422, str(exc)) from exc
    if change is not None and not change["still_works"]:
        raise HTTPException(409, detail={
            "message": "This file's columns differ from the approved mapping and some it needs "
                       "are missing.", "drift": change})
    queued = _start_import(request, conn, user, source_id, row, path)
    return queued | ({"drift": change} if change else {})


def _start_import(request: Request, conn, user: dict, source_id: int, upload: dict,
                  path: Path) -> dict:
    runner = request.app.state.imports
    try:
        queued = runner.submit(conn, source_id, path, user["id"], upload["id"])
    except NoApprovedMapping as exc:
        raise HTTPException(422, str(exc)) from exc
    audit.record(conn, user["id"], "import.start", "import", queued["import_id"],
                 {"source_id": source_id, "status": queued["status"], "file": upload["filename"]},
                 client_ip(request))
    if queued["status"] == "duplicate":
        conn.execute("UPDATE uploads SET status = 'imported', import_id = %s WHERE id = %s",
                     (queued["import_id"], upload["id"]))
        conn.commit()
        path.unlink(missing_ok=True)
    return queued


# ---- watching imports --------------------------------------------------------------------------


@router.get("/imports")
def list_imports(conn: Conn, _: Anyone, source_id: int | None = None,
                 limit: Annotated[int, Query(ge=1, le=200)] = 50,
                 offset: Annotated[int, Query(ge=0)] = 0) -> list[dict]:
    return conn.execute(
        """SELECT i.id, i.filename, i.status, i.stage, i.progress, i.rows_total, i.rows_loaded,
                  i.rows_rejected, i.rows_duplicate, i.created_at, i.finished_at,
                  s.name AS source_name, p.code AS partner_code, u.full_name AS uploaded_by
           FROM imports i JOIN sources s ON s.id = i.source_id
           JOIN partners p ON p.id = s.partner_id LEFT JOIN users u ON u.id = i.created_by
           WHERE (%(s)s::bigint IS NULL OR i.source_id = %(s)s)
           ORDER BY i.id DESC LIMIT %(l)s OFFSET %(o)s""",
        {"s": source_id, "l": limit, "o": offset}).fetchall()


def _import(conn, import_id: int) -> dict:
    row = conn.execute(
        """SELECT i.*, s.name AS source_name, s.kind AS source_kind, p.code AS partner_code
           FROM imports i JOIN sources s ON s.id = i.source_id
           JOIN partners p ON p.id = s.partner_id WHERE i.id = %s""", (import_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "No such import.")
    return row


@router.get("/imports/{import_id}")
def get_import(import_id: int, conn: Conn, _: Anyone) -> dict:
    row = _import(conn, import_id)
    row.pop("file_sha256", None)
    return row


def _safe_raw(raw: dict) -> dict:
    return {k: v for k, v in raw.items() if not is_sensitive_header(k)}


@router.get("/imports/{import_id}/rejects")
def rejects(import_id: int, conn: Conn, _: Anyone,
            limit: Annotated[int, Query(ge=1, le=1000)] = 100,
            offset: Annotated[int, Query(ge=0)] = 0) -> dict:
    _import(conn, import_id)
    rows = conn.execute(
        "SELECT row_no, reason, raw FROM import_rejects WHERE import_id = %s "
        "ORDER BY row_no LIMIT %s OFFSET %s", (import_id, limit, offset)).fetchall()
    total = conn.execute("SELECT count(*) AS n FROM import_rejects WHERE import_id = %s",
                         (import_id,)).fetchone()["n"]
    return {"total": total, "rows": [{**r, "raw": _safe_raw(r["raw"])} for r in rows]}


@router.get("/imports/{import_id}/rejects.csv")
def rejects_csv(import_id: int, request: Request, conn: Conn, user: Anyone) -> Response:
    """The reject report a finance person can hand back to a partner. Personal columns left out."""
    imp = _import(conn, import_id)
    rows = conn.execute("SELECT row_no, reason, raw FROM import_rejects WHERE import_id = %s "
                        "ORDER BY row_no", (import_id,)).fetchall()
    body = csv_bytes(["row_in_file", "why_it_was_refused", "row_values"],
                     [[r["row_no"], r["reason"], json.dumps(_safe_raw(r["raw"]),
                                                            ensure_ascii=False)] for r in rows])
    audit.record(conn, user["id"], "rejects.download", "import", import_id,
                 {"rows": len(rows)}, client_ip(request))
    name = f"rejects-{import_id}-{imp['source_name']}.csv".replace(" ", "_")
    return Response(body, media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="{name}"', "Cache-Control": "no-store"})


# ---- sources and their mapping history ---------------------------------------------------------


@router.get("/sources")
def sources(conn: Conn, _: Anyone) -> list[dict]:
    return mappings.list_sources(conn)


@router.get("/sources/{source_id}/mappings")
def mapping_history(source_id: int, conn: Conn, _: Anyone) -> list[dict]:
    return mappings.history(conn, source_id)


@router.post("/sources/{source_id}/mappings/{version}/rollback")
def rollback(source_id: int, version: int, request: Request, conn: Conn, user: Exec) -> dict:
    try:
        done = mappings.rollback(conn, source_id, version, user["id"])
    except mappings.MappingInvalid as exc:
        raise HTTPException(422, str(exc)) from exc
    audit.record(conn, user["id"], "mapping.rollback", "source", source_id,
                 {"to_version": version, "new_version": done.version}, client_ip(request))
    return {"version": done.version}