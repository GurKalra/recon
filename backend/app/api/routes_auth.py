from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from app import audit, auth
from app.api.deps import Conn, User, client_ip
from app.config import get_settings

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginBody(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=256)


def _public(user: dict) -> dict:
    return {"id": user["id"], "email": user["email"], "full_name": user["full_name"],
            "role": user["role"]}


@router.post("/login")
def login(body: LoginBody, request: Request, response: Response, conn: Conn) -> dict:
    ip = client_ip(request)
    try:
        user = auth.authenticate(conn, body.email, body.password, ip)
    except auth.Locked as exc:
        audit.record(conn, None, "login.locked", detail={"email": body.email.lower()}, ip=ip)
        raise HTTPException(429, str(exc),
                            headers={"Retry-After": str(exc.retry_after_s)}) from exc
    except auth.AuthError as exc:
        audit.record(conn, None, "login.failed", detail={"email": body.email.lower()}, ip=ip)
        raise HTTPException(401, str(exc)) from exc
    token, csrf = auth.start_session(conn, user["id"], ip, request.headers.get("user-agent"))
    settings = get_settings()
    response.set_cookie(
        settings.cookie_name, token, httponly=True, samesite="lax", path="/",
        secure=settings.app_env != "development",
        max_age=int(auth.SESSION_LIFETIME.total_seconds()))
    audit.record(conn, user["id"], "login", ip=ip)
    return {"user": _public(user), "csrf_token": csrf}


@router.post("/logout")
def logout(request: Request, response: Response, conn: Conn, user: User) -> dict:
    auth.end_session(conn, request.cookies.get(get_settings().cookie_name))
    audit.record(conn, user["id"], "logout", ip=client_ip(request))
    response.delete_cookie(get_settings().cookie_name, path="/")
    return {"ok": True}


@router.get("/me")
def me(user: User) -> dict:
    return {"user": _public(user), "csrf_token": user["csrf_token"]}