"""The whole pipeline against a real database: load, idempotency, overlap, drift, failure."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import fastexcel
import gen_data as g
import pytest

from app import demo
from app.ingest import pipeline
from app.ingest.catalog import SOURCES
from app.ingest.pipeline import import_file
from app.seed import seed


@pytest.fixture(scope="module")
def data(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("pipeline")
    g.Generator(path, 1500, seed=5, today=date(2026, 11, 2), use_xlsx=True).run()
    return path


@pytest.fixture
def loaded(dict_conn, data: Path):
    """The whole sample data set imported into a fresh database."""
    demo.load_dataset(dict_conn, data, echo=False)
    return dict_conn


def scalar(conn, sql: str, *params):
    return next(iter(conn.execute(sql, params).fetchone().values()))


def ledger_sum(conn, filename: str) -> int:
    return scalar(conn, "SELECT sum(l.amount_paise) FROM ledger_lines l "
                        "JOIN imports i ON i.id = l.import_id WHERE i.filename = %s", filename)


def paise(text: str) -> int:
    sign = -1 if text.startswith("-") else 1
    rupees, cents = text.lstrip("-").split(".")
    return sign * (int(rupees) * 100 + int(cents))


def csv_column_sum(path: Path, column: str) -> int:
    import csv
    with path.open(encoding="utf-8-sig", newline="") as f:
        return sum(paise(r[column]) for r in csv.DictReader(f))


def test_the_whole_dataset_loads_and_every_known_order_is_linked(loaded, data: Path):
    assert scalar(loaded, "SELECT count(*) FROM orders") == 1500
    assert scalar(loaded, "SELECT count(*) FROM imports WHERE status = 'succeeded'") == 12
    assert scalar(loaded, "SELECT sum(rows_rejected) FROM imports") == 0
    assert scalar(loaded, "SELECT count(*) FROM ledger_lines "
                          "WHERE order_ref IS NOT NULL AND order_id IS NULL") == 0
    assert scalar(loaded, "SELECT count(*) FROM ledger_lines WHERE order_ref IS NULL") > 0
    assert scalar(loaded, "SELECT count(*) FROM warehouse_receipts WHERE order_id IS NULL") == 0


def test_ledger_totals_match_the_totals_printed_in_the_partner_files(loaded, data: Path):
    footer = fastexcel.read_excel(str(data / "settlement_m1.xlsx")).load_sheet(
        0, header_row=None).to_polars().rows()[-1]
    assert ledger_sum(loaded, "settlement_m1.xlsx") == round(float(footer[3]) * 100)
    assert ledger_sum(loaded, "settlement_m2.csv") == csv_column_sum(
        data / "settlement_m2.csv", "net_payout")
    assert ledger_sum(loaded, "settlement_web_pg.csv") == csv_column_sum(
        data / "settlement_web_pg.csv", "Net (INR)")
    assert ledger_sum(loaded, "cod_remittance_ca.csv") == csv_column_sum(
        data / "cod_remittance_ca.csv", "Net Remitted")
    assert ledger_sum(loaded, "freight_invoice_ca.csv") == -csv_column_sum(
        data / "freight_invoice_ca.csv", "Total")


def test_courier_lines_link_through_the_courier_and_channel_lines_through_the_channel(loaded):
    wrong = scalar(loaded, """
        SELECT count(*) FROM ledger_lines l JOIN orders o ON o.id = l.order_id
        JOIN partners p ON p.id = l.partner_id
        WHERE NOT ((p.type = 'courier' AND o.courier_partner_id = l.partner_id)
                OR (p.type <> 'courier' AND o.channel_partner_id = l.partner_id))""")
    assert wrong == 0


def test_verbatim_repeats_inside_a_file_are_stored_and_flagged_not_dropped(loaded):
    repeats = scalar(loaded, "SELECT count(*) FROM ledger_lines WHERE dup_seq > 0")
    assert repeats > 0
    warned = scalar(loaded, """
        SELECT sum((w->>'rows')::int) FROM imports, jsonb_array_elements(warnings) w
        WHERE w->>'code' = 'in_file_duplicates'""")
    assert warned == repeats


def test_importing_the_same_file_twice_changes_nothing(loaded, data: Path):
    before = scalar(loaded, "SELECT count(*) FROM ledger_lines")
    source = next(s for s in SOURCES if s.filename == "settlement_m2.csv")
    source_id = scalar(loaded, "SELECT s.id FROM sources s JOIN partners p ON p.id = s.partner_id "
                               "WHERE p.code = 'M2' AND s.kind = 'settlement'")
    again = import_file(loaded, source_id, data / source.filename)
    assert again.status == "duplicate"
    assert scalar(loaded, "SELECT count(*) FROM ledger_lines") == before


def test_an_overlapping_file_only_adds_what_is_new(loaded, data: Path, tmp_path: Path):
    lines = (data / "settlement_m2.csv").read_text(encoding="utf-8").splitlines()
    overlap = tmp_path / "m2_overlap.csv"
    overlap.write_text("\n".join(lines[:101]) + "\n", encoding="utf-8")  # header + 100 old rows
    source_id = scalar(loaded, "SELECT s.id FROM sources s JOIN partners p ON p.id = s.partner_id "
                               "WHERE p.code = 'M2'")
    before = scalar(loaded, "SELECT count(*) FROM ledger_lines")
    result = import_file(loaded, source_id, overlap)
    assert result.status == "succeeded" and result.rows_loaded == 0
    assert result.rows_duplicate > 0
    assert scalar(loaded, "SELECT count(*) FROM ledger_lines") == before


def test_bad_rows_become_rejects_and_the_rest_still_loads(dict_conn, data: Path, tmp_path: Path):
    ids = seed(dict_conn, data)
    lines = (data / "settlement_m2.csv").read_text(encoding="utf-8").splitlines()
    cells = lines[1].split(",")
    cells[1] = "31-Foo-2026"  # impossible date
    lines[1] = ",".join(cells)
    path = tmp_path / "m2_bad.csv"
    path.write_text("\n".join(lines[:21]) + "\n", encoding="utf-8")
    result = import_file(dict_conn, ids["settlement_m2.csv"], path)
    assert result.status == "succeeded"
    assert result.rows_rejected == 1 and result.rows_total == 20
    reject = dict_conn.execute("SELECT row_no, reason, raw FROM import_rejects").fetchone()
    assert reject["row_no"] == 2 and reject["reason"].startswith("invalid or missing date")
    assert reject["raw"]["settled_on"] == "31-Foo-2026"
    assert scalar(dict_conn, "SELECT count(*) FROM ledger_lines") > 0


def test_a_changed_partner_format_fails_loudly_and_loads_nothing(dict_conn, data: Path,
                                                                tmp_path: Path):
    ids = seed(dict_conn, data)
    text = (data / "settlement_m2.csv").read_text(encoding="utf-8")
    path = tmp_path / "m2_changed.csv"
    path.write_text(text.replace("net_payout", "net_amount_payable", 1), encoding="utf-8")
    result = import_file(dict_conn, ids["settlement_m2.csv"], path)
    assert result.status == "failed" and "net_payout" in result.error
    assert scalar(dict_conn, "SELECT count(*) FROM ledger_lines") == 0
    row = dict_conn.execute("SELECT status, error FROM imports").fetchone()
    assert row["status"] == "failed" and "net_payout" in row["error"]


def test_a_new_unmapped_column_is_only_a_warning(dict_conn, data: Path, tmp_path: Path):
    ids = seed(dict_conn, data)
    lines = (data / "settlement_m2.csv").read_text(encoding="utf-8").splitlines()[:11]
    widened = [lines[0] + ",new_surcharge"] + [row + ",0.00" for row in lines[1:]]
    path = tmp_path / "m2_wider.csv"
    path.write_text("\n".join(widened) + "\n", encoding="utf-8")
    result = import_file(dict_conn, ids["settlement_m2.csv"], path)
    assert result.status == "succeeded"
    assert any(w["code"] == "header_fingerprint_changed" for w in result.warnings)


def test_a_failure_halfway_through_a_load_leaves_no_partial_data(dict_conn, data: Path,
                                                               monkeypatch):
    ids = seed(dict_conn, data)
    real = pipeline._load

    def load_then_explode(*args, **kwargs):
        real(*args, **kwargs)  # rows are written...
        raise RuntimeError("disk on fire")  # ...then the import dies

    monkeypatch.setattr(pipeline, "_load", load_then_explode)
    with pytest.raises(RuntimeError, match="disk on fire"):
        import_file(dict_conn, ids["orders.csv"], data / "orders.csv")
    assert scalar(dict_conn, "SELECT count(*) FROM orders") == 0
    row = dict_conn.execute("SELECT status, error FROM imports").fetchone()
    assert row["status"] == "failed" and "disk on fire" in row["error"]
    monkeypatch.undo()  # and the same file can be retried afterwards
    assert import_file(dict_conn, ids["orders.csv"], data / "orders.csv").status == "succeeded"


def test_settlements_that_arrive_before_their_orders_are_linked_when_orders_land(
        dict_conn, data: Path):
    ids = seed(dict_conn, data)
    m2 = import_file(dict_conn, ids["settlement_m2.csv"], data / "settlement_m2.csv")
    assert m2.status == "succeeded"
    assert scalar(dict_conn, "SELECT count(*) FROM ledger_lines WHERE order_id IS NOT NULL") == 0
    orders = import_file(dict_conn, ids["orders.csv"], data / "orders.csv")
    assert any(w["code"] == "linked_late" for w in orders.warnings)
    assert scalar(dict_conn, "SELECT count(*) FROM ledger_lines "
                             "WHERE order_ref IS NOT NULL AND order_id IS NULL") == 0


def test_seed_is_safe_to_run_twice(dict_conn, data: Path):
    first = seed(dict_conn, data)
    second = seed(dict_conn, data)
    assert first == second
    assert scalar(dict_conn, "SELECT count(*) FROM partners") == 8
    assert scalar(dict_conn, "SELECT count(*) FROM sources") == 12
    assert scalar(dict_conn, "SELECT count(*) FROM source_mappings WHERE status = 'approved'") == 12