from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from app.migrate import discover, migrate

EXPECTED_TABLES = {
    "users", "partners", "sources", "source_mappings", "imports", "import_rejects",
    "orders", "ledger_lines", "warehouse_receipts", "schema_migrations",
}


def _tables(dsn: str) -> set[str]:
    with psycopg.connect(dsn) as conn:
        rows = conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
        ).fetchall()
    return {r[0] for r in rows}


def test_migrations_are_numbered_without_gaps():
    versions = [v for v, _ in discover()]
    assert versions, "no migrations found"
    assert [v[:4] for v in versions] == [f"{i:04d}" for i in range(1, len(versions) + 1)]


def test_migrate_creates_schema_and_is_idempotent(empty_db: str):
    first = migrate(empty_db)
    assert first and first[0].startswith("0001")
    assert _tables(empty_db) >= EXPECTED_TABLES
    assert migrate(empty_db) == []


def test_editing_an_applied_migration_is_refused(empty_db: str, tmp_path: Path):
    (tmp_path / "0001_demo.sql").write_text("CREATE TABLE demo (id int);")
    migrate(empty_db, tmp_path)
    (tmp_path / "0001_demo.sql").write_text("CREATE TABLE demo (id bigint);")
    with pytest.raises(RuntimeError, match="edited after it was applied"):
        migrate(empty_db, tmp_path)


def test_failed_migration_rolls_back_completely(empty_db: str, tmp_path: Path):
    (tmp_path / "0001_bad.sql").write_text("CREATE TABLE ok_table (id int); SELECT 1/0;")
    with pytest.raises(psycopg.errors.DivisionByZero):
        migrate(empty_db, tmp_path)
    assert "ok_table" not in _tables(empty_db)


def test_bad_file_name_is_rejected(tmp_path: Path):
    (tmp_path / "init.sql").write_text("SELECT 1;")
    with pytest.raises(ValueError, match="Bad migration file name"):
        discover(tmp_path)