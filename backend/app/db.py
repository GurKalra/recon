"""Database access: one sync connection pool, one transaction per request.

The workload is bulk SQL (COPY + set-based statements), so sync psycopg 3 with FastAPI's
threadpool is simpler and faster than an async driver here.
"""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Request
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool


def create_pool(dsn: str, min_size: int, max_size: int) -> ConnectionPool:
    return ConnectionPool(
        dsn,
        min_size=min_size,
        max_size=max_size,
        kwargs={"row_factory": dict_row},
        open=False,
        name="recon",
    )


def get_conn(request: Request) -> Iterator[Connection]:
    """FastAPI dependency. Commits when the request succeeds, rolls back on an exception."""
    pool: ConnectionPool = request.app.state.pool
    with pool.connection() as conn:
        yield conn