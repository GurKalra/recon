"""A fresh install can choose a partner in the upload wizard."""

from __future__ import annotations

from pathlib import Path

from app.seed import ensure_partners

SAMPLES = Path(__file__).resolve().parents[2] / "sample_data" / "small"


def test_an_empty_partners_table_gets_the_starting_partners(dict_conn):
    added = ensure_partners(dict_conn, SAMPLES)
    assert added >= 8
    n = dict_conn.execute("SELECT count(*) AS n FROM partners").fetchone()["n"]
    assert n == added
    # nothing else came with them
    for table in ("orders", "ledger_lines", "sources", "channel_cards"):
        assert dict_conn.execute(f"SELECT count(*) AS n FROM {table}").fetchone()["n"] == 0


def test_existing_partners_are_left_alone(dict_conn):
    ensure_partners(dict_conn, SAMPLES)
    dict_conn.execute(
        "UPDATE partners SET name = 'Renamed' WHERE code = (SELECT min(code) FROM partners)")
    dict_conn.commit()
    assert ensure_partners(dict_conn, SAMPLES) == 0
    renamed = dict_conn.execute(
        "SELECT count(*) AS n FROM partners WHERE name = 'Renamed'").fetchone()["n"]
    assert renamed == 1


def test_a_missing_sample_folder_is_not_an_error(dict_conn, tmp_path):
    assert ensure_partners(dict_conn, tmp_path) == 0