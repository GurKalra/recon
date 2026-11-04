"""Create users from the command line, and the two demo logins.

    python -m app.users create you@example.com --role cofounder --name "Your Name"
    python -m app.users demo            # exec@ and cofounder@ demo logins

Passwords are never taken from the command line. `create` asks for one (hidden); `demo` reads
DEMO_EXEC_PASSWORD / DEMO_COFOUNDER_PASSWORD from the environment, and invents and prints a
random one for any that is not set.
"""

from __future__ import annotations

import argparse
import getpass
import os
import secrets

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row

from app import auth
from app.config import get_settings

DEMO_USERS = (
    ("exec@isolabs.demo", "Demo Finance Exec", "finance_exec", "DEMO_EXEC_PASSWORD"),
    ("cofounder@isolabs.demo", "Demo Co-founder", "cofounder", "DEMO_COFOUNDER_PASSWORD"),
)


def ensure_demo_users(conn: Connection, environ: dict[str, str] | None = None) -> list[dict]:
    """Create any missing demo user. Returns one entry per demo user; `password` is set only for
    the ones created now (an existing user's password is left alone)."""
    environ = os.environ if environ is None else environ
    out = []
    for email, name, role, var in DEMO_USERS:
        exists = conn.execute("SELECT 1 FROM users WHERE lower(email) = %s",
                              (email,)).fetchone()
        password = None
        if not exists:
            password = environ.get(var) or secrets.token_urlsafe(12)
            auth.create_user(conn, email, password, name, role)
        out.append({"email": email, "role": role, "created": not exists, "password": password,
                    "from_env": bool(environ.get(var))})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m app.users")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create", help="create one user (asks for the password)")
    c.add_argument("email")
    c.add_argument("--role", choices=auth.ROLES, required=True)
    c.add_argument("--name", required=True)
    sub.add_parser("demo", help="create the two demo logins if they do not exist")
    args = ap.parse_args()
    with psycopg.connect(get_settings().database_url, row_factory=dict_row) as conn:
        if args.cmd == "create":
            password = getpass.getpass("Password (10+ characters): ")
            if password != getpass.getpass("Again: "):
                print("The two passwords differ.")
                return 1
            auth.create_user(conn, args.email, password, args.name, args.role)
            print(f"Created {args.email} ({args.role}).")
            return 0
        for u in ensure_demo_users(conn):
            if not u["created"]:
                print(f"  {u['email']:<26} already exists")
            elif u["from_env"]:
                print(f"  {u['email']:<26} created ({u['role']}), password from the environment")
            else:
                print(f"  {u['email']:<26} created ({u['role']}), password: {u['password']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())