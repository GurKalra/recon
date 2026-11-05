"""The demo workspace: load the sample data in one click, and download the sample files."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from app import audit, demo_workspace
from app.api.deps import Anyone, Conn, Exec, client_ip
from app.config import get_settings
from app.importer import WORK_LOCK

router = APIRouter(prefix="/api/demo", tags=["demo"])


def _dir() -> Path:
    return Path(get_settings().sample_data_dir)


@router.get("/status")
def get_status(conn: Conn, _: Anyone) -> dict:
    """Is the workspace empty, are the sample files installed, which day the data is dated."""
    return demo_workspace.status(conn, _dir())


@router.post("/load")
def load(request: Request, conn: Conn, user: Exec) -> dict:
    if not WORK_LOCK.acquire(blocking=False):
        raise HTTPException(409, "An import or a reconciliation run is in progress. Try again in "
                                 "a moment.")
    try:
        result = demo_workspace.load(conn, _dir())
    except demo_workspace.DemoUnavailable as exc:
        conn.rollback()
        raise HTTPException(409, str(exc)) from exc
    finally:
        WORK_LOCK.release()
    audit.record(conn, user["id"], "demo.load", "workspace", None, result, client_ip(request))
    return result


@router.get("/files/{name}")
def download(name: str, _: Anyone) -> FileResponse:
    """One sample partner file, to upload through the wizard. Only the listed files."""
    if name not in demo_workspace.sample_files():
        raise HTTPException(404, "No such sample file.")
    path = _dir() / name
    if not path.is_file():
        raise HTTPException(404, "No such sample file.")
    return FileResponse(path, filename=name, headers={"Cache-Control": "no-store"})