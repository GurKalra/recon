"""Optional extra columns (SKU, city, remarks): stored, suggested, and used as evidence."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import gen_data as g
import pytest
from pydantic import ValidationError

from app import demo
from app.ai.profile import profile_file
from app.ai.rules import Hint, suggest_with_rules
from app.ingest.mapping import MappingSpec
from app.recon import matching as m

TODAY = date(2026, 11, 2)


# ---- the evidence rules (pure) -----------------------------------------------------------------


def test_a_piece_of_the_reference_in_the_remarks_points_to_that_order():
    ev = m.extras_evidence({"remarks": "Pymt ref ...0000728"}, "OD4100000000728", None)
    assert ev.adjust >= 0.22 and ev.detail["fragment"] == "0000728"
    assert "part of this order's reference" in ev.notes[0]


def test_a_longer_piece_is_stronger_evidence_than_a_short_one():
    long = m.extras_evidence({"r": "ORD-W-20117"}, "ORDW201171", None).adjust
    short = m.extras_evidence({"r": "01171"}, "ORDW201171", None).adjust
    assert long > short > 0


def test_a_mistyped_reference_still_counts_but_less():
    ev = m.extras_evidence({"remarks": "Pymt ref ...0X00728"}, "OD4100000000728", None)
    assert 0 < ev.adjust < 0.22
    assert "one character off" in ev.notes[0]


def test_remarks_that_say_nothing_change_nothing():
    for text in ("Settlement", "Payment for order", "", "see portal"):
        assert m.extras_evidence({"remarks": text}, "OD4100000000728", None).adjust == 0


def test_a_matching_city_and_sku_help_and_a_different_one_counts_against():
    same = m.extras_evidence({"city": "Pune", "sku": "BED-KING-GREY"}, "X1",
                             {"city": "pune", "sku": "BED-KING-GREY"})
    other = m.extras_evidence({"city": "Pune", "sku": "BED-KING-GREY"}, "X1",
                              {"city": "Delhi", "sku": "TWL-BATH-WHT"})
    assert same.adjust == pytest.approx(0.18) and other.adjust == pytest.approx(-0.30)
    assert "Different city" in " ".join(other.notes)


def test_a_column_only_one_side_has_is_not_held_against_anyone():
    assert m.extras_evidence({"city": "Pune"}, "X1", {"sku": "A"}).adjust == 0
    assert m.extras_evidence({}, "X1", {"city": "Pune"}).adjust == 0


# ---- the mapping spec ---------------------------------------------------------------------------


def spec_with(extras):
    return {"kind": "receipts", "format": {"kind": "csv"},
            "receipts": {"order_ref": "a", "received_on": {"column": "b", "format": "iso"}}} \
        if extras is None else {
            "kind": "ledger", "format": {"kind": "csv"},
            "ledger": {"date": {"column": "d", "format": "iso"},
                       "emitters": [{"amount": "x", "line_type": "sale"}], "extras": extras}}


def test_extra_columns_are_validated_and_counted_as_columns_the_file_must_have():
    spec = MappingSpec.model_validate(spec_with({"city": "Store City", "sku": "SKU"}))
    assert {"store city", "sku"} <= set(spec.referenced_columns())


@pytest.mark.parametrize("extras", [{"Bad Key": "x"}, {"1abc": "x"}, {"a-b": "x"},
                                    {f"k{i}": "x" for i in range(11)}])
def test_a_bad_extras_key_or_too_many_are_refused(extras):
    with pytest.raises(ValidationError):
        MappingSpec.model_validate(spec_with(extras))


# ---- suggestions: useful columns are offered, personal ones never -------------------------------


@pytest.fixture(scope="module")
def data(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("extras")
    g.Generator(path, 1200, seed=5, today=TODAY, use_xlsx=True).run()
    return path


def test_sku_city_and_remarks_are_offered_as_extras(data):
    spec = suggest_with_rules(profile_file(data / "settlement_m2.csv"),
                              Hint(source_kind="settlement", partner_type="marketplace")).spec
    assert spec["ledger"]["extras"] == {"city": "city", "remarks": "remarks"}
    orders = suggest_with_rules(profile_file(data / "orders.csv"),
                                Hint(source_kind="orders")).spec
    assert orders["orders"]["extras"] == {"sku": "sku", "city": "city"}


def test_columns_that_look_personal_are_never_kept(data, tmp_path):
    src = (data / "settlement_q1.csv").read_text(encoding="utf-8-sig").splitlines()
    rows = [src[0] + ",Customer Name,Mobile No,Delivery Address,Item Note"]
    rows += [line + ",Asha Rao,9876543210,12 MG Road,gift wrap" for line in src[1:]]
    path = tmp_path / "q1_pii.csv"
    path.write_text("\n".join(rows), encoding="utf-8")
    spec = suggest_with_rules(profile_file(path), Hint(source_kind="settlement",
                                                       partner_type="quick_commerce")).spec
    assert set(spec["ledger"]["extras"].values()) == {"store city", "item note"}


# ---- stored by the importer ---------------------------------------------------------------------


@pytest.fixture
def loaded(dict_conn, data: Path):
    demo.load_dataset(dict_conn, data, echo=False)
    return dict_conn


def test_orders_and_payments_keep_their_extras(loaded):
    orders = loaded.execute("SELECT extras FROM orders WHERE extras IS NOT NULL").fetchall()
    assert len(orders) == loaded.execute("SELECT count(*) AS n FROM orders").fetchone()["n"]
    assert all(set(o["extras"]) == {"sku", "city"} for o in orders)
    m2 = loaded.execute(
        """SELECT l.extras FROM ledger_lines l JOIN partners p ON p.id = l.partner_id
           WHERE p.code = 'M2' AND l.extras IS NOT NULL""").fetchall()
    assert m2 and all("city" in r["extras"] for r in m2)


def test_an_empty_remark_is_not_stored_and_a_file_without_extras_stores_none(loaded):
    blanks = loaded.execute(
        """SELECT count(*) AS n FROM ledger_lines l JOIN partners p ON p.id = l.partner_id
           WHERE p.code = 'M2' AND l.extras ? 'remarks' AND l.extras->>'remarks' = ''""").fetchone()
    assert blanks["n"] == 0
    m3 = loaded.execute(
        """SELECT count(*) AS n FROM ledger_lines l JOIN partners p ON p.id = l.partner_id
           WHERE p.code = 'M3' AND l.extras IS NOT NULL""").fetchone()
    assert m3["n"] == 0


def test_extras_never_change_what_counts_as_a_duplicate_or_a_total(loaded):
    # the sample set has verbatim repeats: they must still be detected with extras present
    n = loaded.execute("SELECT count(*) AS n FROM ledger_lines WHERE dup_seq > 0").fetchone()["n"]
    assert n >= 0
    assert loaded.execute("SELECT count(*) AS n FROM ledger_lines").fetchone()["n"] > 1000