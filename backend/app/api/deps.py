"""What every route needs: the database, the signed-in user, their role, the date we judge by."""

from __future__ import annotations

import secrets
from datetime import date, datetime, timedelta, timezone
from typing import Annotated

from fastapi import Depends, HTTPException, Query, Request
from psycopg import Connection

from app import auth
from app.config import get_settings
from app.db import get_conn

Conn = Annotated[Connection, Depends(get_conn)]
_IST = timezone(timedelta(hours=5, minutes=30))
_UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def current_user(request: Request, conn: Conn) -> dict:
    """The signed-in user. Changes (POST, PUT, PATCH, DELETE) must also carry the CSRF token
    that sign-in returned, in the X-CSRF-Token header."""
    user = auth.session_user(conn, request.cookies.get(get_settings().cookie_name))
    if user is None:
        raise HTTPException(401, "Sign in to continue.")
    if request.method in _UNSAFE:
        sent = request.headers.get("x-csrf-token", "")
        if not secrets.compare_digest(sent.encode(), user["csrf_token"].encode()):
            raise HTTPException(403, "Missing or wrong CSRF token.")
    return user


User = Annotated[dict, Depends(current_user)]


def require(*roles: str):
    """A dependency that lets only these roles through."""
    def check(user: User) -> dict:
        if user["role"] not in roles:
            raise HTTPException(403, "Your role cannot do that.")
        return user
    return check


Anyone = Annotated[dict, Depends(require("finance_exec", "cofounder"))]
Exec = Annotated[dict, Depends(require("finance_exec"))]
Cofounder = Annotated[dict, Depends(require("cofounder"))]


def today_ist() -> date:
    return datetime.now(_IST).date()


def as_of_param(as_of: Annotated[date | None, Query(
        description="Judge deadlines as of this date (default: today, IST).")] = None) -> date:
    return as_of or today_ist()


AsOf = Annotated[date, Depends(as_of_param)]