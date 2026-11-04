"""Users, sign-in, sessions. No web code here: the API wraps these functions.

Sign-in is slowed down, not just checked: five failures for one e-mail (or thirty from one
address) in fifteen minutes lock further attempts until the oldest of them ages out.
"""

from __future__ import annotations

import secrets
from datetime import timedelta

from psycopg import Connection
from psycopg.errors import UniqueViolation

from app.security import (
    hash_password,
    needs_rehash,
    new_token,
    token_digest,
    verify_password,
)

ROLES = ("finance_exec", "cofounder")
WINDOW = timedelta(minutes=15)
MAX_FAILS_PER_EMAIL = 5
MAX_FAILS_PER_IP = 30
SESSION_LIFETIME = timedelta(hours=12)
SESSION_IDLE = timedelta(hours=2)
_TOUCH_EVERY = timedelta(minutes=1)


class AuthError(Exception):
    """Wrong e-mail or password (the message never says which)."""


class Locked(AuthError):
    def __init__(self, retry_after_s: int):
        super().__init__("Too many attempts. Try again later.")
        self.retry_after_s = retry_after_s


class UserExists(Exception):
    pass


def create_user(conn: Connection, email: str, password: str, full_name: str, role: str) -> int:
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    email = email.strip()
    if "@" not in email or not full_name.strip():
        raise ValueError("Give a valid e-mail and a name.")
    pw_hash = hash_password(password)
    try:
        row = conn.execute(
            "INSERT INTO users (email, password_hash, full_name, role) VALUES (%s, %s, %s, %s) "
            "RETURNING id", (email, pw_hash, full_name.strip(), role)).fetchone()
    except UniqueViolation as exc:
        conn.rollback()
        raise UserExists(email) from exc
    conn.commit()
    return row["id"]


def set_password(conn: Connection, user_id: int, password: str) -> None:
    conn.execute("UPDATE users SET password_hash = %s WHERE id = %s",
                 (hash_password(password), user_id))
    conn.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))   # sign out everywhere
    conn.commit()


def _recent_failures(conn: Connection, email: str, ip: str | None) -> tuple[int, int, float]:
    row = conn.execute(
        """SELECT count(*) FILTER (WHERE email = %(e)s) AS by_email,
                  count(*) FILTER (WHERE ip = %(ip)s) AS by_ip,
                  coalesce(extract(epoch FROM now() - min(at)), 0) AS oldest_age
           FROM login_attempts
           WHERE NOT success AND at > now() - %(w)s AND (email = %(e)s OR ip = %(ip)s)""",
        {"e": email, "ip": ip, "w": WINDOW}).fetchone()
    return row["by_email"], row["by_ip"], float(row["oldest_age"])


def authenticate(conn: Connection, email: str, password: str, ip: str | None = None) -> dict:
    """The user (without the hash) or AuthError / Locked."""
    email = email.strip().lower()
    by_email, by_ip, oldest = _recent_failures(conn, email, ip)
    if by_email >= MAX_FAILS_PER_EMAIL or (ip and by_ip >= MAX_FAILS_PER_IP):
        conn.rollback()
        raise Locked(max(1, int(WINDOW.total_seconds() - oldest)))
    user = conn.execute(
        "SELECT id, email, full_name, role, is_active, password_hash FROM users "
        "WHERE lower(email) = %s", (email,)).fetchone()
    ok = verify_password(user["password_hash"] if user else None, password)
    ok = ok and bool(user and user["is_active"])
    conn.execute("INSERT INTO login_attempts (email, ip, success) VALUES (%s, %s, %s)",
                 (email, ip, ok))
    if not ok:
        conn.commit()
        raise AuthError("Wrong e-mail or password.")
    if needs_rehash(user["password_hash"]):
        conn.execute("UPDATE users SET password_hash = %s WHERE id = %s",
                     (hash_password(password), user["id"]))
    conn.execute("DELETE FROM login_attempts WHERE email = %s AND NOT success", (email,))
    conn.commit()
    user.pop("password_hash")
    return user


def start_session(conn: Connection, user_id: int, ip: str | None = None,
                  user_agent: str | None = None) -> tuple[str, str]:
    """(cookie token, csrf token). Only the hash of the cookie token is stored."""
    token, csrf = new_token(), secrets.token_urlsafe(24)
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, csrf_token, expires_at, ip, user_agent) "
        "VALUES (%s, %s, %s, now() + %s, %s, %s)",
        (token_digest(token), user_id, csrf, SESSION_LIFETIME, ip, (user_agent or "")[:300]))
    conn.execute("DELETE FROM sessions WHERE expires_at < now()")   # tidy as we go
    conn.commit()
    return token, csrf


def session_user(conn: Connection, token: str | None) -> dict | None:
    """The signed-in user for a cookie token, or None (unknown, expired, idle, or deactivated)."""
    if not token:
        return None
    row = conn.execute(
        """SELECT s.id AS session_id, s.csrf_token, s.last_seen_at,
                  u.id, u.email, u.full_name, u.role
           FROM sessions s JOIN users u ON u.id = s.user_id
           WHERE s.token_hash = %s AND s.expires_at > now() AND u.is_active
             AND s.last_seen_at > now() - %s""", (token_digest(token), SESSION_IDLE)).fetchone()
    if row is None:
        conn.rollback()
        return None
    conn.execute("UPDATE sessions SET last_seen_at = now() WHERE id = %s AND "
                 "last_seen_at < now() - %s", (row["session_id"], _TOUCH_EVERY))
    conn.commit()
    return row


def end_session(conn: Connection, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token_hash = %s", (token_digest(token),))
        conn.commit()