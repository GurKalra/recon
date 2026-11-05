"""Is the reconciliation out of date? True when something that feeds it changed after the last
successful run: a file was imported, a payment was linked or unlinked, or a rate card was saved.
The screen shows a "run again" prompt from this."""

from __future__ import annotations

from psycopg import Connection


def recon_status(conn: Connection) -> dict:
    last = conn.execute(
        """SELECT id, as_of, finished_at FROM recon_runs WHERE status = 'succeeded'
           ORDER BY finished_at DESC, id DESC LIMIT 1""").fetchone()
    since = last["finished_at"] if last else None
    row = conn.execute(
        """SELECT
             EXISTS (SELECT 1 FROM imports WHERE status = 'succeeded'
                     AND (%(t)s::timestamptz IS NULL OR finished_at > %(t)s)) AS imports,
             EXISTS (SELECT 1 FROM match_groups WHERE decided_at IS NOT NULL
                     AND status IN ('linked', 'open')
                     AND (%(t)s::timestamptz IS NULL OR decided_at > %(t)s)) AS review,
             EXISTS (SELECT 1 FROM (SELECT created_at FROM channel_cards
                                    UNION ALL SELECT created_at FROM courier_cards) c
                     WHERE %(t)s::timestamptz IS NOT NULL AND c.created_at > %(t)s) AS rate_cards
           """, {"t": since}).fetchone()
    reasons = [name for name in ("imports", "review", "rate_cards") if row[name]]
    return {"stale": bool(reasons) or last is None, "reasons": reasons, "never_run": last is None,
            "last_run": last}