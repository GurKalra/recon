"""Reader and transform, on the generated partner files and on deliberately broken ones."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import fastexcel
import gen_data as g
import polars as pl
import pytest

from app.ingest.catalog import SOURCES
from app.ingest.mapping import FileFormat, MappingSpec
from app.ingest.reader import FileReadError, MissingColumnsError, read_table
from app.ingest.transform import transform

TYPES = {p.code: p.type for p in g.PARTNERS.values()}


@pytest.fixture(scope="module")
def data(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("ingest")
    g.Generator(path, 1500, seed=3, today=date(2026, 11, 2), use_xlsx=True).run()
    return path


def run_source(data: Path, filename: str):
    source = next(s for s in SOURCES if s.filename == filename)
    spec = source.spec()
    table = read_table(data / filename, spec.format, spec.referenced_columns())
    return table, transform(table, spec, TYPES)


# ---- reader -------------------------------------------------------------------------------


def test_xlsx_header_is_found_under_title_rows_and_row_numbers_match_excel(data: Path):
    table, _ = run_source(data, "settlement_m1.xlsx")
    assert table.header_row == 4  # three title rows and one blank row above it
    assert table.df["row_no"][0] == 6  # Excel shows the header on row 5, first data on row 6
    raw = fastexcel.read_excel(str(data / "settlement_m1.xlsx")).load_sheet(0, header_row=None)
    first_ref = table.df["order id"][0]
    assert raw.to_polars().row(table.df["row_no"][0] - 1)[0] == first_ref


def test_xlsx_footer_total_row_is_removed(data: Path):
    table, _ = run_source(data, "settlement_m1.xlsx")
    assert "total" not in [str(v).lower() for v in table.df["order id"].to_list() if v]


def test_csv_bom_does_not_leak_into_the_first_header(data: Path):
    table, _ = run_source(data, "settlement_q1.csv")
    assert table.header[0] == "order no"


def test_csv_with_a_preamble_still_finds_its_header(tmp_path: Path):
    path = tmp_path / "f.csv"
    path.write_text("Report generated 2026-11-02\n\nOrder No,Amt\nA1,10.00\nA2,20.00\n")
    table = read_table(path, FileFormat(kind="csv"), ["order no", "amt"])
    assert table.header_row == 2
    assert table.df["row_no"].to_list() == [4, 5]


def test_missing_columns_are_reported_by_name(tmp_path: Path):
    path = tmp_path / "f.csv"
    path.write_text("Order No,Amt\nA1,10.00\n")
    with pytest.raises(MissingColumnsError) as err:
        read_table(path, FileFormat(kind="csv"), ["order no", "net payout"])
    assert err.value.missing == ["net payout"]


def test_unreadable_excel_is_a_clear_error(tmp_path: Path):
    path = tmp_path / "broken.xlsx"
    path.write_bytes(b"this is not a workbook")
    with pytest.raises(FileReadError, match="could not read the Excel file"):
        read_table(path, FileFormat(kind="xlsx"), [])


def test_blank_lines_are_ignored(tmp_path: Path):
    path = tmp_path / "f.csv"
    path.write_text("Order No,Amt\nA1,10.00\n,\n\nA2,20.00\n")
    table = read_table(path, FileFormat(kind="csv"), ["order no"])
    assert table.df["order no"].drop_nulls().to_list() == ["A1", "A2"]


# ---- transform: every real partner file ---------------------------------------------------------


@pytest.mark.parametrize("source", SOURCES, ids=lambda s: s.filename)
def test_every_partner_file_transforms_without_a_single_reject(data: Path, source):
    table, out = run_source(data, source.filename)
    assert out.rejects_total == 0, out.rejects[:3]
    assert out.good.height >= table.df.height  # wide files explode into more lines


def test_wide_row_becomes_lines_that_add_up_to_the_partners_net(data: Path):
    table, out = run_source(data, "settlement_m2.csv")
    net = sum(g_paise(v) for v in table.df["net_payout"].to_list())
    assert out.good["amount_paise"].sum() == net


def test_xlsx_lines_add_up_to_the_footer_total(data: Path):
    _, out = run_source(data, "settlement_m1.xlsx")
    raw = fastexcel.read_excel(str(data / "settlement_m1.xlsx")).load_sheet(0, header_row=None)
    footer = raw.to_polars().rows()[-1]
    assert footer[0] == "Total"
    assert out.good["amount_paise"].sum() == round(float(footer[3]) * 100)


def test_unsigned_deductions_are_negated_and_brackets_are_negative(data: Path):
    _, m2 = run_source(data, "settlement_m2.csv")
    _, q2 = run_source(data, "settlement_q2.csv")
    for out in (m2, q2):
        commissions = out.good.filter(pl.col("line_type") == "commission")["amount_paise"]
        assert commissions.max() <= 0 and commissions.min() < 0
        assert out.good.filter(pl.col("line_type") == "sale")["amount_paise"].min() > 0


def test_mangled_awbs_are_nulled_and_reported(data: Path):
    _, out = run_source(data, "cod_remittance_cb.csv")
    assert out.good["awb"].null_count() == out.good.height
    assert any(w["code"] == "awb_mangled_by_excel" for w in out.warnings)


def test_rows_without_an_order_id_are_kept_and_reported(data: Path):
    _, out = run_source(data, "settlement_m2.csv")
    blanks = out.good.filter(pl.col("order_ref").is_null())
    assert blanks.height > 0
    assert any(w["code"] == "rows_without_order_ref" for w in out.warnings)


def g_paise(text: str) -> int:
    sign = -1 if text.startswith("-") else 1
    rupees, cents = text.lstrip("-").split(".")
    return sign * (int(rupees) * 100 + int(cents))


# ---- transform: bad rows are rejected with a reason, good rows survive ---------------------


def _spec(**ledger) -> MappingSpec:
    return MappingSpec.model_validate({
        "kind": "ledger", "format": {"kind": "csv"},
        "ledger": {"order_ref": "Order No", "date": {"column": "Date", "format": "dmy"},
                   **ledger}})


def test_bad_rows_are_rejected_with_actionable_reasons(tmp_path: Path):
    path = tmp_path / "f.csv"
    path.write_text(
        "Order No,Date,Amt,Net\n"
        "A1,03/10/2026,100.00,100.00\n"      # fine
        "A2,99/99/2026,100.00,100.00\n"      # impossible date
        "A3,03/10/2026,abc,100.00\n"         # not an amount
        "A4,03/10/2026,100.00,90.00\n"       # lines do not add up to the net column
        "A5,03/10/2026,100.001,100.00\n"     # more than two decimals
    )
    spec = _spec(emitters=[{"amount": "Amt", "line_type": "sale"}],
                 net_check={"column": "Net", "tolerance_paise": 0})
    table = read_table(path, spec.format, spec.referenced_columns())
    out = transform(table, spec, TYPES)
    assert out.good["order_ref"].to_list() == ["A1"]
    reasons = {r["row_no"]: r["reason"] for r in out.rejects}
    assert reasons[3].startswith("invalid or missing date")
    assert reasons[4] == "invalid amount in 'Amt'"
    assert reasons[5].startswith("lines add up to 10000 paise but 'Net' says 9000")
    assert reasons[6] == "invalid amount in 'Amt'"
    assert out.rejects[0]["raw"]["order no"] == "A2"  # the original row is kept as evidence


def test_a_wrong_sign_is_caught_by_the_net_check(tmp_path: Path):
    path = tmp_path / "f.csv"
    path.write_text("Order No,Date,Sale,Fee,Net\nA1,03/10/2026,100.00,10.00,90.00\n")
    wrong = _spec(emitters=[{"amount": "Sale", "line_type": "sale"},
                            {"amount": "Fee", "line_type": "commission"}],  # forgot sign: -1
                  net_check={"column": "Net"})
    table = read_table(path, wrong.format, wrong.referenced_columns())
    assert transform(table, wrong, TYPES).rejects_total == 1
    right = _spec(emitters=[{"amount": "Sale", "line_type": "sale"},
                            {"amount": "Fee", "line_type": "commission", "sign": -1}],
                  net_check={"column": "Net"})
    assert transform(table, right, TYPES).rejects_total == 0


def test_unmapped_line_types_are_rejected_not_guessed(tmp_path: Path):
    path = tmp_path / "f.csv"
    path.write_text(
        "Order No,Date,Kind,Amt\nA1,03/10/2026,Sale,100.00\nA2,03/10/2026,Mystery,5.00\n")
    spec = _spec(emitters=[{"amount": "Amt", "line_type_from": ["Kind"],
                            "line_type_map": {"Sale": "sale"}}])
    out = transform(read_table(path, spec.format, spec.referenced_columns()), spec, TYPES)
    assert out.good.height == 1 and out.rejects_total == 1
    assert out.rejects[0]["reason"] == "unmapped line type in ['Kind']"


def test_order_rows_that_would_break_the_database_are_rejected_first(data: Path, tmp_path: Path):
    lines = (data / "orders.csv").read_text().splitlines()
    header, good = lines[0], lines[1]
    cols = header.split(",")
    def with_(**changes: str) -> str:
        cells = good.split(",")
        for name, value in changes.items():
            cells[cols.index(name)] = value
        return ",".join(cells)
    bad_rows = [
        with_(Channel="ZZ"), with_(**{"Payment Mode": "barter"}), with_(Pincode="12"),
        with_(**{"Order Value": "-5.00"}), with_(**{"Order Date": "2999-01-01",
                                                      "Delivered On": "2026-01-01"}),
        with_(Courier="XX"), with_(**{"Weight (g)": "0"}),
    ]
    path = tmp_path / "orders.csv"
    path.write_text("\n".join([header, good, *bad_rows]) + "\n")
    source = next(s for s in SOURCES if s.kind == "orders")
    spec = source.spec()
    out = transform(read_table(path, spec.format, spec.referenced_columns()), spec, TYPES)
    assert out.good.height == 1
    assert [r["reason"] for r in out.rejects] == [
        "unknown channel", "payment mode must be prepaid or cod", "pincode must be 6 digits",
        "negative order value", "delivered before it was ordered", "unknown courier",
        "invalid weight or dimensions"]