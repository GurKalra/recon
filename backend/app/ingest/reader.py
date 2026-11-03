"""Read a partner file into an all-text table with the header row found and footers removed."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

import polars as pl

from app.ingest.mapping import FileFormat, header_fingerprint
from app.ingest.normalize import norm_header

_SCAN_ROWS = 60  # how far down to look for the header row


class FileReadError(ValueError):
    """The file could not be read into a table (not a data problem; a structural one)."""


class MissingColumnsError(FileReadError):
    def __init__(self, missing: list[str], found: list[str]):
        self.missing, self.found = missing, found
        super().__init__(f"columns not found in file: {missing}. File has: {found}")


@dataclass(frozen=True)
class Table:
    df: pl.DataFrame  # every column is text; `row_no` is the 1-based row in the original file
    header: tuple[str, ...]  # normalised header names, in file order
    header_row: int  # 0-based
    fingerprint: str


def _find_header(rows: list[list[object]], wanted: set[str], fixed: int | str) -> int:
    if fixed != "auto":
        return int(fixed)
    if not wanted:
        return 0
    for i, row in enumerate(rows):
        cells = {norm_header(c) for c in row if c is not None and str(c).strip()}
        if wanted <= cells:
            return i
    best = max(
        range(len(rows)),
        key=lambda i: len(wanted & {norm_header(c) for c in rows[i] if c is not None}),
        default=0,
    )
    seen = [str(c) for c in rows[best] if c is not None] if rows else []
    raise MissingColumnsError(sorted(wanted - {norm_header(c) for c in seen}), seen)


def _unique_names(raw: list[object]) -> list[str]:
    names: list[str] = []
    for i, cell in enumerate(raw):
        name = norm_header(cell) if cell is not None and str(cell).strip() else f"_col{i}"
        while name in names:
            name += "_dup"
        names.append(name)
    return names


_Read = tuple[pl.DataFrame, list[str], int]  # (body rows, header names, header row index)


def _read_csv(path: Path, fmt: FileFormat, wanted: set[str]) -> _Read:
    with path.open(encoding="utf-8-sig", errors="replace", newline="") as f:
        preview = [row for _, row in zip(range(_SCAN_ROWS), csv.reader(f), strict=False)]
    idx = _find_header(preview, wanted, fmt.header_row)
    df = pl.read_csv(
        path, skip_rows=idx, has_header=True, infer_schema_length=0,
        encoding="utf8-lossy", truncate_ragged_lines=True,
    )
    names = _unique_names(list(df.columns))
    return df.rename(dict(zip(df.columns, names, strict=True))), names, idx


def _read_xlsx(path: Path, fmt: FileFormat, wanted: set[str]) -> _Read:
    try:
        raw = pl.read_excel(
            path, sheet_id=fmt.sheet + 1, has_header=False, infer_schema_length=0,
            drop_empty_rows=False,  # keep blank rows so row_no matches what Excel shows
        )
    except Exception as exc:  # corrupt file, wrong sheet, password-protected...
        raise FileReadError(f"could not read the Excel file: {exc}") from exc
    idx = _find_header(raw.head(_SCAN_ROWS).rows(), wanted, fmt.header_row)
    names = _unique_names(list(raw.row(idx)))
    body = raw.slice(idx + 1)
    return body.rename(dict(zip(body.columns, names, strict=True))), names, idx


def read_table(path: Path, fmt: FileFormat, wanted: list[str] | None = None) -> Table:
    """Read `path`. `wanted` are the (normalised) columns the mapping needs: they locate the
    header row and are checked to exist."""
    wanted_set = set(wanted or [])
    reader = _read_csv if fmt.kind == "csv" else _read_xlsx
    df, names, idx = reader(path, fmt, wanted_set)
    missing = sorted(wanted_set - set(names))
    if missing:
        raise MissingColumnsError(missing, names)
    data_cols = list(df.columns)
    df = df.with_columns((pl.int_range(pl.len()) + idx + 2).cast(pl.Int64).alias("row_no"))
    df = df.filter(pl.any_horizontal(pl.col(*data_cols).is_not_null()))  # drop blank rows
    if fmt.footer_markers and data_cols:
        markers = [m.strip().lower() for m in fmt.footer_markers]
        first = pl.col(data_cols[0]).str.strip_chars().str.to_lowercase()
        df = df.filter(~first.is_in(markers).fill_null(False))
    return Table(df=df, header=tuple(names), header_row=idx, fingerprint=header_fingerprint(names))