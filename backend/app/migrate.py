"""Minimal SQL-first migration runner.

    python -m app.migrate

Applies migrations/NNNN_name.sql in order, each in its own transaction, recording a checksum.
An advisory lock makes concurrent runs safe; editing an applied migration is refused.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
from pathlib import Path

import psycopg

from app.config import get_settings

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")
_LOCK_KEY = 727_001


def discover(directory: Path = MIGRATIONS_DIR) -> list[tuple[str, Path]]:
    found: list[tuple[str, Path]] = []
    for path in sorted(directory.glob("*.sql")):
        if not _NAME.match(path.name):
            raise ValueError(f"Bad migration file name: {path.name} (expected NNNN_name.sql)")
        found.append((path.stem, path))
    return found


def migrate(dsn: str, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply pending migrations. Returns the versions applied by this call."""
    applied_now: list[str] = []
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("SELECT pg_advisory_lock(%s)", (_LOCK_KEY,))
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version    TEXT PRIMARY KEY,
                    checksum   TEXT        NOT NULL,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            done = dict(conn.execute("SELECT version, checksum FROM schema_migrations").fetchall())
            for version, path in discover(directory):
                sql = path.read_text(encoding="utf-8")
                checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
                if version in done:
                    if done[version] != checksum:
                        raise RuntimeError(
                            f"Migration {version} was edited after it was applied. "
                            "Write a new migration instead."
                        )
                    continue
                with conn.transaction():
                    conn.execute(sql)  # no params -> multi-statement script is allowed
                    conn.execute(
                        "INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)",
                        (version, checksum),
                    )
                applied_now.append(version)
        finally:
            conn.execute("SELECT pg_advisory_unlock(%s)", (_LOCK_KEY,))
    return applied_now


def main() -> int:
    # Migrations may need more rights than the running app has (it cannot create tables), so a
    # separate address can be given for them.
    applied = migrate(os.environ.get("MIGRATE_DATABASE_URL") or get_settings().database_url)
    print(f"Applied: {', '.join(applied)}" if applied else "Database is up to date.")
    return 0


if __name__ == "__main__":
    sys.exit(main())