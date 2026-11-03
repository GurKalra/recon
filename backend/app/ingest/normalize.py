"""Polars expression builders that turn messy partner text into clean typed values.

Every builder takes a column NAME (already header-normalised) and returns expressions.
Parsers that can fail return (value, bad): `bad` is True only when the cell had content that
could not be understood, so empty cells stay legitimately null.
"""

from __future__ import annotations

import polars as pl

SCI_NOTATION = r"^\d(\.\d+)?[Ee]\+\d+$"  # what Excel does to long numeric ids: 1.23457E+14
_DATE_FORMATS = {"dmy": "%d/%m/%Y", "dmony": "%d-%b-%Y"}


def norm_header(name: object) -> str:
    return " ".join(str(name).strip().lower().split())


def text(col: str) -> pl.Expr:
    """Trimmed string, or null when the cell is empty or blank."""
    trimmed = pl.col(col).str.strip_chars()
    return pl.when(trimmed.str.len_chars() > 0).then(trimmed)


def canonical(expr: pl.Expr) -> pl.Expr:
    """Upper-case, letters and digits only: 'od4100 ' / 'OD-4100' -> 'OD4100'."""
    cleaned = expr.str.to_uppercase().str.replace_all(r"[^A-Z0-9]", "")
    return pl.when(cleaned.str.len_chars() > 0).then(cleaned)


def canonical_ref(col: str) -> pl.Expr:
    return canonical(text(col))


def awb(col: str) -> tuple[pl.Expr, pl.Expr]:
    """(clean awb, mangled). An Excel-mangled awb lost its digits for good, so it becomes null."""
    raw = text(col)
    mangled = raw.str.contains(SCI_NOTATION).fill_null(False)
    return pl.when(~mangled).then(canonical(raw)), mangled


def money(col: str) -> tuple[pl.Expr, pl.Expr]:
    """(paise, bad). Accepts 1499.00, 1,499.00, 1,08,000.00, ₹499, -46.00 and (46.00).

    Integer paise; a value with more than two decimals is rejected rather than silently rounded.
    """
    cleaned = (
        text(col)
        .str.replace_all(r"[₹,\s]", "")
        .str.replace(r"^\((.*)\)$", "-${1}")  # accounting negatives
    )
    value = cleaned.cast(pl.Float64, strict=False)
    scaled = value * 100
    paise = scaled.round(0)
    ok = (value.is_finite() & ((scaled - paise).abs() < 1e-6)).fill_null(False)
    bad = cleaned.is_not_null() & ~ok
    return pl.when(ok).then(paise.cast(pl.Int64)), bad


def date(col: str, fmt: str) -> tuple[pl.Expr, pl.Expr]:
    """(date, bad). fmt: iso (also accepts timestamps), dmy (31/12/2026), dmony (31-Dec-2026)."""
    raw = text(col)
    if fmt == "iso":
        parsed = raw.str.slice(0, 10).str.to_date("%Y-%m-%d", strict=False)
    else:
        parsed = raw.str.to_date(_DATE_FORMATS[fmt], strict=False)
    return parsed, raw.is_not_null() & parsed.is_null()


def whole_int(col: str, lo: int, hi: int) -> tuple[pl.Expr, pl.Expr]:
    """(int, bad) for a value that must be a whole number within [lo, hi]."""
    raw = text(col)
    as_float = raw.cast(pl.Float64, strict=False)
    value = pl.when(as_float == as_float.floor()).then(as_float.cast(pl.Int64))
    ok = (value >= lo) & (value <= hi)
    return pl.when(ok).then(value), raw.is_not_null() & ~ok.fill_null(False)