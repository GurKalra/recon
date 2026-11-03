from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from app.ingest import normalize as nz


def run(expr_or_pair, values: list[str | None]):
    """Evaluate expression(s) built for column 'x' over `values`."""
    frame = pl.DataFrame({"x": values}, schema={"x": pl.Utf8})
    exprs = expr_or_pair if isinstance(expr_or_pair, tuple) else (expr_or_pair,)
    out = frame.select([e.alias(f"c{i}") for i, e in enumerate(exprs)])
    return [out[c].to_list() for c in out.columns]


@pytest.mark.parametrize("raw, paise", [
    ("1499.00", 149_900), ("1,499.00", 149_900), ("1,08,000.00", 10_800_000),
    ("₹499", 49_900), ("-46.00", -4_600), ("(46.00)", -4_600), ("(₹1,046.50)", -104_650),
    (" 12 ", 1_200), ("-0.5", -50), ("99999999.99", 9_999_999_999), ("-311.88", -31_188),
])
def test_money_parses_every_partner_style_to_integer_paise(raw, paise):
    value, bad = run(nz.money("x"), [raw])
    assert value == [paise] and bad == [False]


@pytest.mark.parametrize("raw", ["abc", "12.345", "nan", "inf", "1.2.3"])
def test_money_flags_garbage_and_sub_paise_values(raw):
    value, bad = run(nz.money("x"), [raw])
    assert value == [None] and bad == [True]


def test_money_leaves_empty_cells_null_and_not_bad():
    value, bad = run(nz.money("x"), ["", "   ", None])
    assert value == [None] * 3 and bad == [False] * 3


def test_float_artefacts_from_excel_round_to_the_right_paise():
    assert run(nz.money("x"), ["-311.88", "1499.0", "2.67"])[0] == [-31_188, 149_900, 267]


@pytest.mark.parametrize("fmt, raw", [
    ("iso", "2026-10-03"), ("iso", "2026-10-03T10:15:00+05:30"), ("dmy", "03/10/2026"),
    ("dmony", "03-Oct-2026"),
])
def test_dates_parse_in_every_format(fmt, raw):
    value, bad = run(nz.date("x", fmt), [raw])
    assert value == [date(2026, 10, 3)] and bad == [False]


@pytest.mark.parametrize("fmt, raw", [("iso", "2026-13-45"), ("dmy", "31/02/2026"),
                                      ("dmy", "2026-10-03"), ("dmony", "03-Foo-2026")])
def test_impossible_dates_are_flagged(fmt, raw):
    value, bad = run(nz.date("x", fmt), [raw])
    assert value == [None] and bad == [True]


def test_empty_dates_are_null_not_bad():
    assert run(nz.date("x", "iso"), ["", None]) == [[None, None], [False, False]]


def test_canonical_ref_makes_partner_spellings_comparable():
    values = ["od4100000000013 ", "OD4100000000013", "ORD-W-204118", "402-1000002-9062250",
              "  ", "-", None]
    assert run(nz.canonical_ref("x"), values)[0] == [
        "OD4100000000013", "OD4100000000013", "ORDW204118", "40210000029062250",
        None, None, None]


def test_awb_mangled_by_excel_becomes_null_and_is_flagged():
    clean, mangled = run(nz.awb("x"), ["100000000012", "1.00000E+14", "ab-12", None])
    assert clean == ["100000000012", None, "AB12", None]
    assert mangled == [False, True, False, False]


def test_whole_int_enforces_range_and_whole_numbers():
    value, bad = run(nz.whole_int("x", 1, 100), ["12", "12.0", "0", "101", "12.5", "abc", ""])
    assert value == [12, 12, None, None, None, None, None]
    assert bad == [False, False, True, True, True, True, False]