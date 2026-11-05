"""Serve the built React app from the API itself.

Used on hosts that give one web service and no reverse proxy (Render, Railway). The browser then
talks to a single origin, so there is no CORS and the session cookie works as it does behind nginx.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

CSP = ("default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; "
       "frame-ancestors 'none'")


def mount_web(app: FastAPI, dist: Path, *, hsts: bool) -> None:
    root = dist.resolve()
    index = root / "index.html"
    app.mount("/assets", StaticFiles(directory=root / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def screen(path: str) -> FileResponse:
        if path == "api" or path.startswith("api/"):
            raise HTTPException(404, "Not found")
        target = (root / path).resolve()
        if path and target.is_file() and root in target.parents:
            return FileResponse(target)
        return FileResponse(index)     # every other path is a screen of the React app

    @app.middleware("http")
    async def site_headers(request: Request, call_next) -> Response:
        response = await call_next(request)
        if not request.url.path.startswith("/api/"):
            response.headers.setdefault("Content-Security-Policy", CSP)
            response.headers.setdefault("Permissions-Policy",
                                        "camera=(), microphone=(), geolocation=()")
            if request.url.path.startswith("/assets/"):
                response.headers.setdefault("Cache-Control", "public, max-age=31536000, immutable")
            else:
                response.headers.setdefault("Cache-Control", "no-cache")
        if hsts:
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
        return response