"""Look at an unknown partner file and describe it: where the header is, and what each column
holds. Used by the mapping suggester, and (masked) as the evidence the model is shown.

Nothing here guesses what a column MEANS; it only measures what the values look like.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

import polars as pl

from app.ai.mask import is_sensitive_header, safe_sample
from app.ingest.mapping import FileFormat
from app.ingest.reader import FileReadError, Table, read_table

PROFILE_ROWS = 2000
_SCAN = 60
_FOOTER = re.compile(r"^(grand\s+|sub\s*)?total\b", re.IGNORECASE)
_DMY = re.compile(r"^\d{1,2}/\d{1,2}/\d{4}$")
_DMONY = re.compile(r"^\d{1,2}-[A-Za-z]{3}-\d{4}$")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}")
_NUMBER = re.compile(r"^\(?-?[₹]?\s*\d[\d,]*(\.\d+)?\)?$")


@dataclass
class ColumnProfile:
    name: str                       # normalised header, exactly as a mapping must spell it
    kind: str                       # date_iso|date_dmy|date_dmony|number|id|enum|text|empty
    filled: float                   # share of rows with a value
    distinct: int
    unique_ratio: float
    negative_share: float = 0.0     # among numbers
    zero_share: float = 0.0         # among numbers (including blanks counted as 0 by emitters)
    integer_only: bool = False
    values: list[str] = field(default_factory=list)   # enum values (all, if few)
    samples: list[str] = field(default_factory=list)  # masked, safe to show a model
    sensitive: bool = False


@dataclass
class FileProfile:
    path: str
    format: FileFormat
    header_row: int
    rows: int
    columns: list[ColumnProfile]
    table: Table = field(repr=False)

    def col(self, name: str) -> ColumnProfile:
        return next(c for c in self.columns if c.name == name)

    def names(self) -> list[str]:
        return [c.name for c in self.columns]


# ---- locating the header ------------------------------------------------------------------------


def _filled(row: list[object]) -> int:
    return sum(1 for c in row if c is not None and str(c).strip())


def _looks_numeric(cell: object) -> bool:
    return bool(_NUMBER.match(str(cell).strip()))


def detect_header_row(rows: list[list[object]]) -> int:
    """The first dense row of mostly non-numeric cells that is followed by another dense row.
    Skips title lines ('Settlement Report', 'Seller: ...') and blank rows above the table."""
    width = max((_filled(r) for r in rows), default=0)
    if width < 2:
        return 0
    need = max(2, int(0.6 * width + 0.5))
    for i, row in enumerate(rows[:-1]):
        cells = [c for c in row if c is not None and str(c).strip()]
        if len(cells) < need or _filled(rows[i + 1]) < need:
            continue
        if sum(_looks_numeric(c) for c in cells) <= len(cells) // 4:
            return i
    return 0


def _preview_rows(path: Path, kind: str, sheet: int = 0) -> list[list[object]]:
    if kind == "csv":
        with path.open(encoding="utf-8-sig", errors="replace", newline="") as f:
            return [row for _, row in zip(range(_SCAN), csv.reader(f), strict=False)]
    try:
        raw = pl.read_excel(path, sheet_id=sheet + 1, has_header=False, infer_schema_length=0,
                            drop_empty_rows=False)
    except Exception as exc:
        raise FileReadError(f"could not read the Excel file: {exc}") from exc
    return raw.head(_SCAN).rows()


def file_kind(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        return "xlsx"
    if suffix in (".csv", ".txt"):
        return "csv"
    raise FileReadError(f"unsupported file type '{suffix}' (use .csv or .xlsx)")


# ---- measuring columns --------------------------------------------------------------------------


def _classify(name: str, series: pl.Series) -> ColumnProfile:
    vals = [v.strip() for v in series.drop_nulls().to_list() if str(v).strip()]
    n = series.len()
    filled = len(vals) / n if n else 0.0
    if not vals:
        return ColumnProfile(name, "empty", 0.0, 0, 0.0)
    distinct = len(set(vals))
    prof = ColumnProfile(name, "text", filled, distinct, distinct / len(vals),
                         sensitive=is_sensitive_header(name))

    def share(pred) -> float:
        return sum(1 for v in vals if pred(v)) / len(vals)

    if share(lambda v: bool(_ISO.match(v))) >= 0.9:
        prof.kind = "date_iso"
    elif share(lambda v: bool(_DMY.match(v))) >= 0.9:
        prof.kind = "date_dmy"
    elif share(lambda v: bool(_DMONY.match(v))) >= 0.9:
        prof.kind = "date_dmony"
    elif share(lambda v: bool(_NUMBER.match(v))) >= 0.9:
        prof.kind = "number"
        nums = [x for x in (_to_float(v) for v in vals) if x is not None]
        prof.negative_share = sum(1 for x in nums if x < 0) / len(nums)
        prof.zero_share = sum(1 for x in nums if x == 0) / len(nums)
        prof.integer_only = all(x == int(x) for x in nums) and not any("." in v for v in vals)
    elif distinct <= 12 and len(vals) >= 2 * distinct:
        prof.kind = "enum"
        prof.values = sorted(set(vals))
    elif prof.unique_ratio > 0.5 and all(re.fullmatch(r"[A-Za-z0-9\-_/. ]+", v) for v in vals):
        prof.kind = "id"
    prof.samples = [safe_sample(name, v) for v in dict.fromkeys(vals)][:5]
    if prof.kind == "enum":  # enum values are labels, not personal data, unless the header says so
        prof.values = [safe_sample(name, v) for v in prof.values]
    return prof


def _to_float(v: str) -> float | None:
    """The number in a cell, or None for a stray non-number in an otherwise numeric column."""
    neg = v.startswith("(") and v.endswith(")")
    try:
        x = float(re.sub(r"[₹,()\s]", "", v) or 0)
    except ValueError:
        return None
    return -abs(x) if neg else x


def profile_file(path: Path, kind: str | None = None, sheet: int = 0) -> FileProfile:
    """Find the header row and footer, read the file as text, and measure every column."""
    path = Path(path)
    kind = kind or file_kind(path)
    header_row = detect_header_row(_preview_rows(path, kind, sheet))
    fmt = FileFormat(kind=kind, sheet=sheet, header_row=header_row)  # type: ignore[arg-type]
    table = read_table(path, fmt, None)
    # a trailing 'Total' row is a footer, not data
    tail = table.df.tail(3)
    first_col = table.df.columns[0]
    markers = tuple(dict.fromkeys(
        str(v).strip() for v in tail[first_col].to_list() if v and _FOOTER.match(str(v).strip())))
    if markers:
        fmt = FileFormat(kind=kind, sheet=sheet, header_row=header_row,  # type: ignore[arg-type]
                         footer_markers=markers)
        table = read_table(path, fmt, None)
    body = table.df.head(PROFILE_ROWS)
    cols = [_classify(c, body[c]) for c in body.columns if c != "row_no"]
    return FileProfile(str(path), fmt, header_row, table.df.height, cols, table)