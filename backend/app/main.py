"""FastAPI entry point."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from psycopg import Connection

from app.config import get_settings
from app.db import create_pool, get_conn


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    pool = create_pool(settings.database_url, settings.db_pool_min, settings.db_pool_max)
    pool.open(wait=True, timeout=15)  # fail fast at startup if the database is unreachable
    app.state.pool = pool
    try:
        yield
    finally:
        pool.close()


app = FastAPI(title="Recon API", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health(conn: Annotated[Connection, Depends(get_conn)]) -> dict[str, object]:
    row = conn.execute("SELECT 1 AS ok").fetchone()
    return {"status": "ok", "db": bool(row and row["ok"] == 1)}