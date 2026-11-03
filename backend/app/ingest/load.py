"""Bulk-load clean frames into Postgres with COPY, then upsert set-based.

Expects a connection that returns dict rows (psycopg.rows.dict_row), like the app's pool.

No row-by-row INSERTs anywhere. Table and column names come from this module, never from
file contents, so the f-strings below are not an injection surface.
"""

from __future__ import annotations

from dataclasses import dataclass

import polars as pl
from psycopg import Connection

_CHUNK_ROWS = 50_000

_STAGING_DDL = {
    "stg_ledger": """
        row_no int, emit_idx smallint, order_ref text, order_ref_raw text, awb text,
        line_type text, amount_paise bigint, occurred_on date, reference text""",
    "stg_orders": """
        row_no int, order_ref text, order_ref_raw text, channel_code text, courier_code text,
        awb text, ordered_on date, delivered_on date, returned_on date, payment_mode text,
        value_paise bigint, weight_g int, length_cm int, width_cm int, height_cm int,
        pincode text, delivery_status text""",
    "stg_receipts": "row_no int, order_ref text, received_on date, condition text",
}


@dataclass(frozen=True)
class LoadCounts:
    loaded: int  # new rows written
    updated: int = 0  # existing rows changed (orders only)
    skipped: int = 0  # rows already present from an earlier file
    in_file_duplicates: int = 0  # verbatim repeats inside this file, stored and flagged


def _copy_frame(conn: Connection, table: str, df: pl.DataFrame) -> None:
    columns = ", ".join(df.columns)
    with conn.cursor() as cur, cur.copy(f"COPY {table} ({columns}) FROM STDIN (FORMAT csv)") as cp:
        for start in range(0, df.height, _CHUNK_ROWS):
            cp.write(df.slice(start, _CHUNK_ROWS).write_csv(include_header=False))


def _stage(conn: Connection, table: str, df: pl.DataFrame) -> None:
    conn.execute(f"CREATE TEMP TABLE {table} ({_STAGING_DDL[table]}) ON COMMIT DROP")
    _copy_frame(conn, table, df)
    conn.execute(f"ANALYZE {table}")  # temp tables are never auto-analyzed; joins need stats


def load_ledger(conn: Connection, import_id: int, partner_id: int, partner_type: str,
                df: pl.DataFrame) -> LoadCounts:
    """A courier's lines belong to the courier's orders; every other partner's to its channel's."""
    owner = "courier_partner_id" if partner_type == "courier" else "channel_partner_id"
    _stage(conn, "stg_ledger", df)
    cur = conn.execute(
        f"""
        WITH keyed AS (
            SELECT s.*, encode(sha256(convert_to(concat_ws('|',
                       s.line_type, s.amount_paise::text, s.occurred_on::text,
                       coalesce(s.order_ref, ''), coalesce(s.awb, ''), coalesce(s.reference, '')
                   ), 'UTF8')), 'hex') AS line_hash
            FROM stg_ledger s
        ), seq AS (
            SELECT k.*, row_number() OVER (PARTITION BY line_hash ORDER BY row_no, emit_idx) - 1
                        AS dup_seq
            FROM keyed k
        )
        INSERT INTO ledger_lines (import_id, partner_id, order_id, order_ref, order_ref_raw, awb,
                                  line_type, amount_paise, occurred_on, reference, line_hash,
                                  dup_seq, source_row)
        SELECT %(imp)s, %(pid)s, o.id, seq.order_ref, seq.order_ref_raw, seq.awb, seq.line_type,
               seq.amount_paise, seq.occurred_on, seq.reference, seq.line_hash, seq.dup_seq,
               seq.row_no
        FROM seq
        LEFT JOIN orders o ON o.order_ref = seq.order_ref AND o.{owner} = %(pid)s
        ON CONFLICT (partner_id, line_hash, dup_seq) DO NOTHING
        """,
        {"imp": import_id, "pid": partner_id},
    )
    loaded = cur.rowcount
    dups = conn.execute(
        "SELECT count(*) AS n FROM ledger_lines WHERE import_id = %s AND dup_seq > 0", (import_id,)
    ).fetchone()["n"]
    return LoadCounts(loaded=loaded, skipped=df.height - loaded, in_file_duplicates=dups)


def load_orders(conn: Connection, import_id: int, df: pl.DataFrame) -> LoadCounts:
    _stage(conn, "stg_orders", df)
    row = conn.execute(
        """
        WITH up AS (
            INSERT INTO orders (channel_partner_id, courier_partner_id, order_ref, order_ref_raw,
                                awb, ordered_on, delivered_on, returned_on, payment_mode,
                                value_paise, weight_g, length_cm, width_cm, height_cm, pincode,
                                delivery_status, import_id)
            SELECT ch.id, co.id, s.order_ref, s.order_ref_raw, s.awb, s.ordered_on,
                   s.delivered_on, s.returned_on, s.payment_mode, s.value_paise, s.weight_g,
                   s.length_cm, s.width_cm, s.height_cm, s.pincode, s.delivery_status, %(imp)s
            FROM (SELECT DISTINCT ON (channel_code, order_ref) *
                  FROM stg_orders ORDER BY channel_code, order_ref, row_no DESC) s
            JOIN partners ch ON ch.code = s.channel_code
            LEFT JOIN partners co ON co.code = s.courier_code
            ON CONFLICT (channel_partner_id, order_ref) DO UPDATE SET
                courier_partner_id = EXCLUDED.courier_partner_id, awb = EXCLUDED.awb,
                delivered_on = EXCLUDED.delivered_on, returned_on = EXCLUDED.returned_on,
                delivery_status = EXCLUDED.delivery_status, value_paise = EXCLUDED.value_paise,
                import_id = EXCLUDED.import_id, updated_at = now()
            WHERE (orders.courier_partner_id, orders.awb, orders.delivered_on,
                   orders.returned_on, orders.delivery_status, orders.value_paise)
                  IS DISTINCT FROM
                  (EXCLUDED.courier_partner_id, EXCLUDED.awb, EXCLUDED.delivered_on,
                   EXCLUDED.returned_on, EXCLUDED.delivery_status, EXCLUDED.value_paise)
            RETURNING (xmax = 0) AS inserted
        )
        SELECT count(*) FILTER (WHERE inserted) AS inserted,
               count(*) FILTER (WHERE NOT inserted) AS updated FROM up
        """,
        {"imp": import_id},
    ).fetchone()
    inserted, updated = row["inserted"], row["updated"]
    conn.execute("ANALYZE orders")  # the next file's lines are joined against it
    return LoadCounts(loaded=inserted, updated=updated,
                      skipped=df.height - inserted - updated)


def load_receipts(conn: Connection, import_id: int, df: pl.DataFrame) -> LoadCounts:
    _stage(conn, "stg_receipts", df)
    cur = conn.execute(
        """
        INSERT INTO warehouse_receipts (import_id, order_id, order_ref, received_on, condition)
        SELECT %(imp)s, o.id, s.order_ref, s.received_on, s.condition
        FROM (SELECT DISTINCT ON (order_ref, received_on) *
              FROM stg_receipts ORDER BY order_ref, received_on, row_no) s
        LEFT JOIN orders o ON o.order_ref = s.order_ref
        ON CONFLICT (order_ref, received_on) DO NOTHING
        """,
        {"imp": import_id},
    )
    return LoadCounts(loaded=cur.rowcount, skipped=df.height - cur.rowcount)


def relink_unlinked(conn: Connection) -> dict[str, int]:
    """After an ORDERS import: attach order_id to earlier lines and receipts that arrived first.

    Ledger and receipt imports resolve order_id while inserting, so this only matters when
    settlements are uploaded before the orders file. The partial indexes keep it cheap.
    """
    linked = 0
    # Two equality joins (channel partner, then courier partner) so each can use an index.
    for owner in ("channel_partner_id", "courier_partner_id"):
        linked += conn.execute(
            f"""
            UPDATE ledger_lines l SET order_id = o.id
            FROM orders o
            WHERE l.order_id IS NULL AND l.order_ref IS NOT NULL
              AND o.order_ref = l.order_ref AND o.{owner} = l.partner_id
            """
        ).rowcount
    receipts = conn.execute(
        """
        UPDATE warehouse_receipts r SET order_id = o.id
        FROM orders o
        WHERE r.order_id IS NULL AND o.order_ref = r.order_ref
        """
    ).rowcount
    return {"ledger_lines_linked": linked, "receipts_linked": receipts}