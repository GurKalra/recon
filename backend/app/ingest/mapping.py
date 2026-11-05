"""The mapping spec: how one partner's file layout becomes our canonical rows.

A spec is plain JSON stored in source_mappings.mapping. It is validated here so a bad or
hand-edited spec fails with a clear message before it can touch any data.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.ai.mask import is_sensitive_header
from app.ingest.normalize import norm_header

LineType = Literal[
    "sale", "refund", "commission", "fixed_fee", "shipping_fee", "return_fee", "pg_fee",
    "freight", "return_freight", "cod_fee", "tcs", "tds", "adjustment",
]
DateFormat = Literal["iso", "dmy", "dmony"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FileFormat(_Strict):
    kind: Literal["csv", "xlsx"]
    sheet: int = 0
    header_row: int | Literal["auto"] = "auto"  # 0-based row of the header, or find it
    footer_markers: tuple[str, ...] = ()  # first-cell values that mark a footer (e.g. "Total")


class DateCol(_Strict):
    column: str
    format: DateFormat


class Emitter(_Strict):
    """Turns one amount column into ledger lines.

    Wide files have one emitter per amount column (fixed line_type). Long files have a single
    emitter whose line type is looked up from one or more columns (line_type_from + map).
    `sign` flips deductions that the partner prints as positive numbers.
    """

    amount: str
    sign: Literal[1, -1] = 1
    skip_zero: bool = False
    line_type: LineType | None = None
    line_type_from: tuple[str, ...] = ()
    line_type_map: dict[str, LineType] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _exactly_one_type_source(self) -> Emitter:
        fixed, looked_up = self.line_type is not None, bool(self.line_type_from)
        if fixed == looked_up:
            raise ValueError("give either line_type or line_type_from (+ line_type_map)")
        if looked_up and not self.line_type_map:
            raise ValueError("line_type_from needs a non-empty line_type_map")
        return self


class NetCheck(_Strict):
    """The partner prints a net column; our lines must add up to it (catches wrong signs)."""

    column: str
    sign: Literal[1, -1] = 1
    tolerance_paise: int = Field(default=100, ge=0)


_EXTRA_KEY = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
MAX_EXTRAS = 10


def _check_extras(extras: dict[str, str]) -> dict[str, str]:
    """Optional extra columns kept as-is (SKU, city, remarks...): {key in our data: column}."""
    if len(extras) > MAX_EXTRAS:
        raise ValueError(f"at most {MAX_EXTRAS} extra columns can be kept")
    for key, column in extras.items():
        if not _EXTRA_KEY.match(key):
            raise ValueError(f"extra column key {key!r} must be lower-case letters, digits "
                             "and underscores")
        if is_sensitive_header(key) or is_sensitive_header(column):
            raise ValueError(f"column {column!r} looks like personal data (a name, phone, "
                             "email, address or account number). It is never stored.")
    return extras


class LedgerFields(_Strict):
    order_ref: str | None = None
    awb: str | None = None
    reference: str | None = None
    date: DateCol
    emitters: tuple[Emitter, ...] = Field(min_length=1)
    net_check: NetCheck | None = None
    extras: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _extras_are_valid(self) -> Self:
        _check_extras(self.extras)
        return self


class OrderFields(_Strict):
    order_ref: str
    channel: str
    ordered_on: DateCol
    payment_mode: str
    value: str
    delivery_status: str
    pincode: str | None = None
    weight_g: str | None = None
    length_cm: str | None = None
    width_cm: str | None = None
    height_cm: str | None = None
    courier: str | None = None
    awb: str | None = None
    delivered_on: DateCol | None = None
    returned_on: DateCol | None = None
    extras: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _extras_are_valid(self) -> Self:
        _check_extras(self.extras)
        return self


class ReceiptFields(_Strict):
    order_ref: str
    received_on: DateCol
    condition: str | None = None


class MappingSpec(_Strict):
    spec_version: Literal[1] = 1
    kind: Literal["orders", "ledger", "receipts"]
    format: FileFormat
    orders: OrderFields | None = None
    ledger: LedgerFields | None = None
    receipts: ReceiptFields | None = None

    @model_validator(mode="after")
    def _block_matches_kind(self) -> MappingSpec:
        blocks = {"orders": self.orders, "ledger": self.ledger, "receipts": self.receipts}
        present = [name for name, block in blocks.items() if block is not None]
        if present != [self.kind]:
            raise ValueError(f"kind={self.kind!r} needs exactly that block, found {present}")
        return self

    def referenced_columns(self) -> list[str]:
        """Normalised names of every column this spec reads (must exist in the file)."""
        found: list[str] = []

        def add(name: str | None) -> None:
            if name:
                found.append(norm_header(name))

        if self.ledger:
            ledger = self.ledger
            add(ledger.order_ref)
            add(ledger.awb)
            add(ledger.reference)
            add(ledger.date.column)
            for e in ledger.emitters:
                add(e.amount)
                for col in e.line_type_from:
                    add(col)
            if ledger.net_check:
                add(ledger.net_check.column)
            for col in ledger.extras.values():
                add(col)
        elif self.orders:
            for name, value in self.orders.model_dump().items():
                if name == "extras":
                    for col in value.values():
                        add(col)
                elif isinstance(value, str):
                    add(value)
                elif isinstance(value, dict):
                    add(value["column"])
        elif self.receipts:
            add(self.receipts.order_ref)
            add(self.receipts.received_on.column)
            add(self.receipts.condition)
        return list(dict.fromkeys(found))  # unique, in order


def header_fingerprint(headers: Iterable[object]) -> str:
    """Stable hash of a file's header row. A change means the partner changed their format."""
    joined = "|".join(norm_header(h) for h in headers)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()