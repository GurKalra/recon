"""Personal data is never kept: a column that looks like a name, phone, email, address or account
number cannot be saved as an extra column, however the mapping was made."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.ingest.catalog import SOURCES
from app.ingest.mapping import MappingSpec

BASE = SOURCES[0].mapping   # the orders file's real mapping


def spec(extras: dict[str, str]) -> dict:
    return {**BASE, "orders": {**BASE["orders"], "extras": extras}}


@pytest.mark.parametrize("column", ["Customer Name", "Buyer Phone", "Email", "Shipping Address",
                                    "Bank Account", "PAN"])
def test_personal_looking_columns_cannot_be_kept(column):
    with pytest.raises(ValidationError, match="personal data"):
        MappingSpec.model_validate(spec({"extra": column}))


def test_a_personal_looking_key_is_refused_too():
    with pytest.raises(ValidationError, match="personal data"):
        MappingSpec.model_validate(spec({"customer_note": "Remarks"}))


def test_ordinary_extras_are_still_fine():
    ok = MappingSpec.model_validate(spec({"sku": "SKU", "city": "City"}))
    assert ok.orders.extras == {"sku": "SKU", "city": "City"}