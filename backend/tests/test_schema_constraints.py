"""The database itself must refuse bad data; the app is not the only line of defence."""

from __future__ import annotations

import hashlib

import psycopg
import pytest


@pytest.fixture
def conn(migrated_db: str):
    with psycopg.connect(migrated_db, autocommit=True) as c:
        yield c


def _partner(conn, code="M1", type_="marketplace") -> int:
    return conn.execute(
        "INSERT INTO partners (code, name, type) VALUES (%s, %s, %s) RETURNING id",
        (code, code, type_),
    ).fetchone()[0]


def _import(conn, partner_id: int, sha: str = "a" * 64, kind="settlement") -> int:
    source_id = conn.execute(
        "INSERT INTO sources (partner_id, kind, name) VALUES (%s, %s, 'main') RETURNING id",
        (partner_id, kind),
    ).fetchone()[0]
    return conn.execute(
        "INSERT INTO imports (source_id, filename, file_sha256, file_bytes) "
        "VALUES (%s, 'f.csv', %s, 1) RETURNING id",
        (source_id, sha),
    ).fetchone()[0]


def _line(conn, import_id, partner_id, *, amount=100, seq=0, ref="ORD1", kind="sale"):
    h = hashlib.sha256(f"{ref}|{kind}|{amount}".encode()).hexdigest()
    conn.execute(
        "INSERT INTO ledger_lines (import_id, partner_id, order_ref, line_type, amount_paise, "
        "occurred_on, line_hash, dup_seq) VALUES (%s, %s, %s, %s, %s, '2026-10-01', %s, %s)",
        (import_id, partner_id, ref, kind, amount, h, seq),
    )


def test_partner_code_format_enforced(conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        _partner(conn, code="bad code")


def test_only_one_approved_mapping_per_source(conn):
    pid = _partner(conn)
    sid = conn.execute(
        "INSERT INTO sources (partner_id, kind, name) VALUES (%s, 'settlement', 's') RETURNING id",
        (pid,),
    ).fetchone()[0]
    insert = (
        "INSERT INTO source_mappings (source_id, version, mapping, header_fingerprint, status, "
        "approved_at) VALUES (%s, %s, '{}', 'fp', 'approved', now())"
    )
    conn.execute(insert, (sid, 1))
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(insert, (sid, 2))


def test_approved_mapping_requires_approval_timestamp(conn):
    pid = _partner(conn)
    sid = conn.execute(
        "INSERT INTO sources (partner_id, kind, name) VALUES (%s, 'settlement', 's') RETURNING id",
        (pid,),
    ).fetchone()[0]
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO source_mappings (source_id, version, mapping, header_fingerprint, "
            "status) VALUES (%s, 1, '{}', 'fp', 'approved')",
            (sid,),
        )


def test_same_file_cannot_be_imported_twice_but_a_failed_one_can_be_retried(conn):
    pid = _partner(conn)
    imp = _import(conn, pid)
    source_id = conn.execute("SELECT source_id FROM imports WHERE id=%s", (imp,)).fetchone()[0]
    dup = (
        "INSERT INTO imports (source_id, filename, file_sha256, file_bytes) "
        "VALUES (%s, 'again.csv', %s, 1)"
    )
    with pytest.raises(psycopg.errors.UniqueViolation):
        conn.execute(dup, (source_id, "a" * 64))
    conn.execute("UPDATE imports SET status='failed' WHERE id=%s", (imp,))
    conn.execute(dup, (source_id, "a" * 64))  # allowed once the first attempt failed


def test_ledger_line_idempotency_and_in_file_duplicates(conn):
    pid = _partner(conn)
    imp = _import(conn, pid)
    _line(conn, imp, pid, seq=0)
    with pytest.raises(psycopg.errors.UniqueViolation):  # re-upload of the same row
        _line(conn, imp, pid, seq=0)
    _line(conn, imp, pid, seq=1)  # verbatim repeat inside a file is stored, flagged by seq


def test_order_ref_must_be_canonical(conn):
    pid = _partner(conn, "WEB", "website")
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "INSERT INTO orders (channel_partner_id, order_ref, order_ref_raw, ordered_on, "
            "payment_mode, value_paise, delivery_status) "
            "VALUES (%s, 'ord-w-1', 'ord-w-1', '2026-10-01', 'cod', 149900, 'placed')",
            (pid,),
        )


def test_order_dates_and_money_are_validated(conn):
    pid = _partner(conn, "WEB", "website")
    base = (
        "INSERT INTO orders (channel_partner_id, order_ref, order_ref_raw, ordered_on, "
        "delivered_on, payment_mode, value_paise, delivery_status) "
        "VALUES (%s, 'ORDW1', 'ORD-W-1', '2026-10-05', %s, 'cod', %s, 'delivered')"
    )
    with pytest.raises(psycopg.errors.CheckViolation):  # delivered before it was ordered
        conn.execute(base, (pid, "2026-10-01", 149900))
    with pytest.raises(psycopg.errors.CheckViolation):  # negative order value
        conn.execute(base, (pid, "2026-10-07", -1))
    conn.execute(base, (pid, "2026-10-07", 149900))