"""Who did what, when. Called by the API for every change and every sensitive read."""

from __future__ import annotations

from psycopg import Connection
from psycopg.types.json import Jsonb


def record(conn: Connection, user_id: int | None, action: str, entity: str | None = None,
           entity_id: int | None = None, detail: dict | None = None, ip: str | None = None,
           commit: bool = True) -> None:
    conn.execute(
        "INSERT INTO audit_log (user_id, action, entity, entity_id, detail, ip) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (user_id, action, entity, entity_id, Jsonb(detail or {}), ip))
    if commit:
        conn.commit()


def recent(conn: Connection, limit: int = 100, offset: int = 0, user_id: int | None = None,
           action: str | None = None) -> list[dict]:
    return conn.execute(
        """SELECT a.id, a.at, a.action, a.entity, a.entity_id, a.detail, a.ip,
                  u.email AS user_email, u.full_name AS user_name
           FROM audit_log a LEFT JOIN users u ON u.id = a.user_id
           WHERE (%(u)s::bigint IS NULL OR a.user_id = %(u)s)
             AND (%(a)s::text IS NULL OR a.action = %(a)s)
           ORDER BY a.at DESC, a.id DESC LIMIT %(l)s OFFSET %(o)s""",
        {"u": user_id, "a": action, "l": limit, "o": offset}).fetchall()