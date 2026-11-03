"""Shared fixtures. DB tests create a throwaway database and skip if Postgres is unreachable."""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from app.migrate import migrate

ADMIN_DSN = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://recon:recon@localhost:5432/postgres"
)


@pytest.fixture(scope="session")
def admin_dsn() -> str:
    try:
        with psycopg.connect(ADMIN_DSN, connect_timeout=3):
            pass
    except psycopg.OperationalError as exc:
        pytest.skip(f"Postgres not reachable for DB tests: {exc}")
    return ADMIN_DSN


@pytest.fixture
def empty_db(admin_dsn: str) -> Iterator[str]:
    """DSN of a brand-new empty database, dropped afterwards."""
    name = f"recon_test_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(admin_dsn, autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    dsn = make_conninfo(**{**conninfo_to_dict(admin_dsn), "dbname": name})
    try:
        yield dsn
    finally:
        with psycopg.connect(admin_dsn, autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def migrated_db(empty_db: str) -> str:
    migrate(empty_db)
    return empty_db


@pytest.fixture
def dict_conn(migrated_db: str) -> Iterator[psycopg.Connection]:
    """A connection on a fresh migrated database, returning dict rows like the app's pool."""
    with psycopg.connect(migrated_db, row_factory=dict_row) as conn:
        yield conn