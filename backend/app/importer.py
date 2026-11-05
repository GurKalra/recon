"""Run imports in the background, one at a time, and let the screen watch their progress.

One worker thread means two imports never write at the same time. Reconciliation takes the same
lock, so a run never sees half an import. (One process only; a bigger deployment would move this
to a proper job queue, which the TDD lists under "next".)
"""

from __future__ import annotations

import hashlib
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from psycopg import Connection
from psycopg_pool import ConnectionPool

from app.ingest.pipeline import import_file
from app.recon import matching

log = logging.getLogger("recon.importer")

WORK_LOCK = threading.Lock()     # held while an import or a reconciliation run is writing
PROGRESS = {"read": 0.1, "transform": 0.35, "load": 0.6, "link": 0.9}


class NoApprovedMapping(ValueError):
    pass


def mark_interrupted(conn: Connection) -> int:
    """After a restart: imports that were queued or running will never finish."""
    rows = conn.execute(
        "UPDATE imports SET status = 'failed', error = 'Interrupted by a server restart.', "
        "finished_at = now() WHERE status IN ('queued', 'running') RETURNING id").fetchall()
    conn.commit()
    return len(rows)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


class ImportRunner:
    def __init__(self, pool: ConnectionPool):
        self.pool = pool
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="import")

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    def submit(self, conn: Connection, source_id: int, path: Path, user_id: int | None,
               upload_id: int | None = None) -> dict:
        """Queue an import and return at once. Returns {import_id, status}; status is 'duplicate'
        (nothing queued) when this exact file was already imported for this source."""
        approved = conn.execute(
            "SELECT id FROM source_mappings WHERE source_id = %s AND status = 'approved'",
            (source_id,)).fetchone()
        if approved is None:
            raise NoApprovedMapping("This source has no approved mapping yet.")
        sha, size = _sha256(path), path.stat().st_size
        existing = conn.execute(
            "SELECT id FROM imports WHERE source_id = %s AND file_sha256 = %s "
            "AND status <> 'failed'", (source_id, sha)).fetchone()
        if existing:
            return {"import_id": existing["id"], "status": "duplicate"}
        import_id = conn.execute(
            "INSERT INTO imports (source_id, mapping_id, filename, file_sha256, file_bytes, "
            "status, created_by) VALUES (%s, %s, %s, %s, %s, 'queued', %s) RETURNING id",
            (source_id, approved["id"], path.name, sha, size, user_id)).fetchone()["id"]
        if upload_id is not None:
            conn.execute("UPDATE uploads SET import_id = %s WHERE id = %s", (import_id, upload_id))
            conn.execute("UPDATE imports SET filename = u.filename FROM uploads u "
                         "WHERE u.id = %s AND imports.id = %s", (upload_id, import_id))
        conn.commit()
        self._executor.submit(self._run, source_id, path, user_id, import_id, upload_id)
        return {"import_id": import_id, "status": "queued"}

    # ---- the worker side ----

    def _stage(self, import_id: int, stage: str) -> None:
        try:
            with self.pool.connection() as conn:
                conn.execute("UPDATE imports SET stage = %s, progress = %s WHERE id = %s",
                             (stage, PROGRESS.get(stage, 0), import_id))
        except Exception:   # progress is a courtesy; never fail the import over it
            log.exception("could not record progress")

    def _rebuild_review_queue(self, import_id: int) -> None:
        """New payments without an order ID join the review queue; suggestions are refreshed.
        The import itself already succeeded, so a problem here is logged, not fatal."""
        try:
            with self.pool.connection() as conn:
                matching.refresh(conn)
        except Exception:
            log.exception("could not refresh the review queue after import %s", import_id)

    def _run(self, source_id: int, path: Path, user_id: int | None, import_id: int,
             upload_id: int | None) -> None:
        with WORK_LOCK:
            try:
                with self.pool.connection() as conn:
                    result = import_file(conn, source_id, path, user_id, import_id=import_id,
                                         on_stage=self._stage)
                if result.status == "succeeded":
                    path.unlink(missing_ok=True)
                    self._rebuild_review_queue(import_id)
                    if upload_id is not None:
                        with self.pool.connection() as conn:
                            conn.execute("UPDATE uploads SET status = 'imported' WHERE id = %s",
                                         (upload_id,))
            except Exception as exc:
                log.exception("import %s failed", import_id)
                with self.pool.connection() as conn:
                    conn.execute(
                        "UPDATE imports SET status = 'failed', error = coalesce(error, %s), "
                        "finished_at = coalesce(finished_at, now()) WHERE id = %s "
                        "AND status <> 'succeeded'",
                        (f"{type(exc).__name__}: {exc}"[:500], import_id))