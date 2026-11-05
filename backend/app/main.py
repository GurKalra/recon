"""FastAPI entry point."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from psycopg import Connection
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.api import (
    routes_auth,
    routes_claims,
    routes_data,
    routes_imports,
    routes_recon,
    routes_review,
)
from app.claims import ClaimError
from app.config import get_settings
from app.db import create_pool, get_conn
from app.importer import ImportRunner, mark_interrupted


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    pool = create_pool(settings.database_url, settings.db_pool_min, settings.db_pool_max)
    pool.open(wait=True, timeout=15)  # fail fast at startup if the database is unreachable
    app.state.pool = pool
    with pool.connection() as conn:
        mark_interrupted(conn)
    app.state.imports = ImportRunner(pool)
    try:
        yield
    finally:
        app.state.imports.shutdown()
        pool.close()


_settings = get_settings()
_docs = _settings.app_env == "development"   # the API explorer is for developers only
app = FastAPI(title="Recon API", version="0.1.0", lifespan=lifespan,
              docs_url="/docs" if _docs else None, redoc_url=None,
              openapi_url="/openapi.json" if _docs else None)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "X-CSRF-Token"],
)
if _settings.trusted_proxies:   # believe X-Forwarded-For only from these proxies
    app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=_settings.trusted_proxies)
for router in (routes_auth.router, routes_data.router, routes_claims.router,
               routes_imports.router, routes_review.router, routes_recon.router):
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