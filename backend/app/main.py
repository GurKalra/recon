"""FastAPI entry point."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from psycopg import Connection

from app.api import routes_auth, routes_claims, routes_data, routes_recon
from app.claims import ClaimError
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
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "X-CSRF-Token"],
)
for router in (routes_auth.router, routes_data.router, routes_claims.router,
               routes_recon.router):
    app.include_router(router)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


@app.exception_handler(ClaimError)
async def claim_error(_: Request, exc: ClaimError) -> JSONResponse:
    """A refused claim action: 404 when the thing does not exist, else 409 (wrong state)."""
    status = 404 if str(exc).startswith("No such") else 409
    return JSONResponse({"detail": str(exc)}, status_code=status)


@app.get("/api/health")
def health(conn: Annotated[Connection, Depends(get_conn)]) -> dict[str, object]:
    row = conn.execute("SELECT 1 AS ok").fetchone()
    return {"status": "ok", "db": bool(row and row["ok"] == 1)}