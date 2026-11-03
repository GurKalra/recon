"""Mapping suggestions: rules on the sample files, on disguised copies of them, and the model
step with a fake service (no network)."""

from __future__ import annotations

import copy
import csv
import json
from datetime import date
from pathlib import Path

import gen_data as g
import polars as pl
import pytest

from app.ai import evaluate as ev
from app.ai.llm import LLMClient
from app.ai.profile import detect_header_row, profile_file
from app.ai.rules import Hint, classify_header, classify_value, suggest_with_rules, verify
from app.ai.suggest import SYSTEM, nearest_examples, suggest_mapping
from app.ingest.catalog import SOURCES
from app.ingest.normalize import norm_header

TYPES = {"WEB": "website", "M1": "marketplace", "M2": "marketplace", "M3": "marketplace",
         "Q1": "quick_commerce", "Q2": "quick_commerce", "CA": "courier", "CB": "courier"}


@pytest.fixture(scope="module")
def data(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("ai")
    g.Generator(path, 1500, seed=3, today=date(2026, 11, 2), use_xlsx=True).run()
    return path


def source(name: str):
    return next(s for s in SOURCES if s.filename == name)


# ---- small pure pieces --------------------------------------------------------------------------


@pytest.mark.parametrize("header,partner,expected", [
    ("commission", "marketplace", "commission"), ("platform fee", "quick_commerce", "commission"),
    ("fee (inr)", "website", "pg_fee"), ("shipping", "courier", "freight"),
    ("shipping fee", "marketplace", "shipping_fee"), ("rto/return charge", "courier",
                                                      "return_freight"),
    ("return shipping fee", "marketplace", "return_fee"), ("cod charges", "courier", "cod_fee"),
    ("fixed closing fee", "marketplace", "fixed_fee"), ("tcs", None, "tcs"),
    ("order amount", "quick_commerce", "sale"), ("cod collected", "courier", "sale"),
    ("net payout", None, None), ("remitted", None, None), ("utr", None, None),
])
def test_headers_are_read_the_way_a_finance_person_would(header, partner, expected):
    assert classify_header(header, partner) == expected


def test_a_pair_of_labels_is_classified_together():
    assert classify_value(("Refund", "Principal")) == "refund"
    assert classify_value(("ItemPrice", "Principal")) == "sale"
    assert classify_value(("ItemFees", "ReturnShippingFee")) == "return_fee"
    assert classify_value(("settlement",)) == "sale"
    assert classify_value(("Mystery",)) is None


def test_the_header_is_found_below_title_lines_and_blank_rows():
    rows = [["Seller Settlement Report"], ["Seller: Acme"], [], ["Order ID", "Date", "Amount"],
            ["A1", "01/01/2026", "10.00"], ["A2", "02/01/2026", "20.00"]]
    assert detect_header_row(rows) == 3
    assert detect_header_row([["a", "b"], ["1", "2"]]) == 0


# ---- the rules on the real sample files --------------------------------------------------------


def test_rules_alone_read_every_sample_file_like_the_approved_mapping(data):
    rows = ev.evaluate(data, use_llm=False)
    assert [r.file for r in rows if not r.identical] == []


def test_a_footer_total_and_title_rows_are_handled_without_help(data):
    p = profile_file(data / "settlement_m1.xlsx")
    assert p.header_row == 4 and p.format.footer_markers == ("Total",)


def test_the_net_column_is_found_by_arithmetic_not_by_name(data):
    p = profile_file(data / "settlement_q1.csv")
    spec = suggest_with_rules(p, Hint(partner_type="quick_commerce")).spec
    assert spec["ledger"]["net_check"] == {"column": "net payout", "sign": 1}
    signs = {e["line_type"]: e["sign"] for e in spec["ledger"]["emitters"]}
    assert signs == {"sale": 1, "commission": -1, "tcs": -1, "tds": -1}


def test_an_existing_order_column_beats_one_with_a_misleading_name(data, tmp_path):
    # 'Ref' (Q2's order column) is found because its values are orders we already have
    refs = ev.known_refs(data, TYPES)
    p = profile_file(data / "settlement_q2.csv")
    with_help = suggest_with_rules(p, Hint(known_refs=refs)).spec["ledger"]["order_ref"]
    assert with_help == "ref"


# ---- disguised copies: same data, different names, order, junk ----------------------------------


def disguise(src: Path, dst: Path, rename: dict[str, str], junk: bool = True) -> dict[str, str]:
    """Rewrite a CSV with renamed headers, shuffled columns, a junk column and title rows.
    Returns {old normalised name: new normalised name}."""
    with src.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    header, body = rows[0], rows[1:]
    new_header = [rename.get(h, h) for h in header]
    order = list(reversed(range(len(header))))
    out = [["Exported by ACME portal v2"], []] if junk else []
    cols = [new_header[i] for i in order] + (["Internal Code"] if junk else [])
    out.append(cols)
    for r in body:
        out.append([r[i] for i in order] + (["see portal"] if junk else []))
    with dst.open("w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(out)
    return {norm_header(h): norm_header(rename.get(h, h)) for h in header}


def renamed_spec(gold: dict, mapping: dict[str, str]) -> dict:
    """The approved mapping, rewritten to use the disguised column names."""
    spec = copy.deepcopy(gold)
    spec["format"] = {"kind": "csv", "header_row": 2}

    def walk(node):
        if isinstance(node, dict):
            return {k: (walk(v) if k != "line_type_map" else v) for k, v in node.items()}
        if isinstance(node, list):
            return [walk(v) for v in node]
        if isinstance(node, str):
            return mapping.get(norm_header(node), node)
        return node

    return walk(spec)


def outcome(path: Path, spec: dict) -> pl.DataFrame:
    from app.ingest.mapping import MappingSpec
    s = MappingSpec.model_validate(spec)
    from app.ingest.reader import read_table
    from app.ingest.transform import transform
    good = transform(read_table(path, s.format, s.referenced_columns()), s, TYPES).good
    return good.filter(pl.col("amount_paise") != 0).drop("row_no", "emit_idx").sort(
        "order_ref", "line_type", "amount_paise", "occurred_on")


EASY = {
    "settlement_q1.csv": {"Order No": "Order Number", "Payout Date": "Settlement Date",
                          "Sale Value": "Gross Sale", "Platform Fee": "Commission",
                          "Net Payout": "Net Amount"},
    "cod_remittance_ca.csv": {"Order Ref": "Client Order Id", "COD Collected": "COD Amount",
                              "Freight Charged": "Freight", "Remitted On": "Payout Date",
                              "Net Remitted": "Remitted"},
    "freight_invoice_cb.csv": {"Client Order No": "Order Ref", "Bill Date": "Invoice Date",
                               "Forward Charge": "Freight", "RTO/Return Charge":
                                   "Return Freight", "Amount": "Total"},
}


@pytest.mark.parametrize("name", sorted(EASY))
def test_a_disguised_file_with_ordinary_names_still_reads_correctly(data, tmp_path, name):
    gold = source(name)
    dst = tmp_path / name
    mapping = disguise(data / name, dst, EASY[name])
    s = suggest_mapping(dst, Hint(source_kind=gold.kind, partner_type=TYPES[gold.partner]),
                        use_llm=False, partner_types=TYPES)
    want = outcome(dst, renamed_spec(gold.mapping, mapping))
    assert s.spec is not None and s.check.ok
    assert outcome(dst, s.spec).equals(want)


HARD = {  # names no keyword table knows
    "settlement_m2.csv": {"gross_sale": "item total", "order_id": "seller order no",
                          "settled_on": "payment date", "utr": "bank ref"},
}


def hard_case(data, tmp_path):
    name = "settlement_m2.csv"
    gold = source(name)
    dst = tmp_path / name
    mapping = disguise(data / name, dst, HARD[name])
    return gold, dst, mapping


def test_when_the_rules_cannot_tell_they_say_so_instead_of_guessing_silently(data, tmp_path):
    gold, dst, mapping = hard_case(data, tmp_path)
    s = suggest_mapping(dst, Hint(source_kind="settlement", partner_type="marketplace"),
                        use_llm=False, partner_types=TYPES)
    want = outcome(dst, renamed_spec(gold.mapping, mapping))
    wrong = s.spec is None or not outcome(dst, s.spec).equals(want)
    assert wrong, "this case is supposed to be beyond the rules"
    assert s.warnings, "a wrong answer must come with a warning so the model is asked"


# ---- the model step, with a fake service -------------------------------------------------------


def fake_llm(*answers: dict | str):
    calls: list[dict] = []
    seq = iter(answers)

    def transport(url, headers, body, timeout):
        calls.append(json.loads(body))
        answer = next(seq)
        content = answer if isinstance(answer, str) else json.dumps(answer)
        return 200, json.dumps({"model": "fake", "choices": [{"message": {"content": content}}],
                                "usage": {"total_tokens": 100}})

    return LLMClient("key", "fake", "https://x.test/v1", transport=transport,
                     sleep=lambda s: None), calls


def test_the_model_rescues_a_file_the_rules_cannot_read(data, tmp_path):
    gold, dst, mapping = hard_case(data, tmp_path)
    right = renamed_spec(gold.mapping, mapping)
    llm, calls = fake_llm({"spec": right, "notes": [
        {"field": "emit:item total", "value": "sale", "confidence": "high", "reason": "gross"}]})
    s = suggest_mapping(dst, Hint(source_kind="settlement", partner_type="marketplace"),
                        llm=llm, partner_types=TYPES)
    assert s.source == "llm" and s.check.ok and s.llm_tokens == 100
    assert outcome(dst, s.spec).equals(outcome(dst, right))
    assert len(calls) == 1
    system, user = calls[0]["messages"][0]["content"], calls[0]["messages"][1]["content"]
    assert system == SYSTEM
    assert "FILE PROFILE" in user and "item total" in user and "EXAMPLE" in user


def test_a_wrong_answer_is_sent_back_with_the_errors_and_repaired(data, tmp_path):
    gold, dst, mapping = hard_case(data, tmp_path)
    right = renamed_spec(gold.mapping, mapping)
    wrong = copy.deepcopy(right)
    wrong["ledger"]["emitters"][0]["amount"] = "gross sale"   # a column that is not in the file
    llm, calls = fake_llm({"spec": wrong}, {"spec": right})
    s = suggest_mapping(dst, Hint(source_kind="settlement", partner_type="marketplace"),
                        llm=llm, partner_types=TYPES)
    assert s.source == "llm_repaired" and s.check.ok
    assert len(calls) == 2
    assert "gross sale" in calls[1]["messages"][1]["content"]
    assert "columns not in the file" in calls[1]["messages"][1]["content"]
    assert any("model round 1" in a for a in s.attempts)


def test_an_unusable_reply_is_retried_then_the_rules_answer_stands(data, tmp_path):
    gold, dst, _ = hard_case(data, tmp_path)
    llm, calls = fake_llm("I cannot help", "still no", "nope")
    s = suggest_mapping(dst, Hint(source_kind="settlement", partner_type="marketplace"),
                        llm=llm, partner_types=TYPES)
    assert s.source in ("rules", "none") and len(calls) == 3
    assert s.warnings


def test_the_model_cannot_make_things_worse(data, tmp_path):
    """Rules found a clean answer: the model is not even asked."""
    name = "settlement_q1.csv"
    llm, calls = fake_llm({"spec": {"kind": "receipts"}})
    s = suggest_mapping(data / name, Hint(source_kind="settlement", partner_type="quick_commerce"),
                        llm=llm, partner_types=TYPES)
    assert s.source == "rules" and calls == []


def test_a_model_answer_that_is_worse_than_the_rules_is_discarded(data, tmp_path):
    gold, dst, _ = hard_case(data, tmp_path)
    llm, _ = fake_llm({"spec": {"kind": "receipts"}}, {"spec": {"kind": "receipts"}},
                      {"spec": {"kind": "receipts"}})
    rules_only = suggest_mapping(dst, Hint(source_kind="settlement",
                                           partner_type="marketplace"), use_llm=False,
                                 partner_types=TYPES)
    s = suggest_mapping(dst, Hint(source_kind="settlement", partner_type="marketplace"),
                        llm=llm, partner_types=TYPES)
    assert s.spec == rules_only.spec


def test_no_key_means_rules_only_and_no_error(data, tmp_path):
    gold, dst, _ = hard_case(data, tmp_path)
    llm = LLMClient("", "m", "https://x.test")
    s = suggest_mapping(dst, Hint(source_kind="settlement", partner_type="marketplace"),
                        llm=llm, partner_types=TYPES)
    assert s.source in ("rules", "none")
    assert any("no API key" in a for a in s.attempts)


def test_the_service_being_down_degrades_gracefully(data, tmp_path):
    gold, dst, _ = hard_case(data, tmp_path)

    def down(url, headers, body, timeout):
        return 503, "unavailable"

    llm = LLMClient("key", "m", "https://x.test", transport=down, sleep=lambda s: None)
    s = suggest_mapping(dst, Hint(source_kind="settlement", partner_type="marketplace"),
                        llm=llm, partner_types=TYPES)
    assert any("unavailable" in w for w in s.warnings)


def test_examples_never_include_the_answer_itself(data):
    p = profile_file(data / "settlement_q1.csv")
    for cols, _ in nearest_examples(p, Hint(source_kind="settlement")):
        assert {norm_header(c) for c in cols} != set(p.names()) or len(cols) != len(p.names())


def test_a_mapping_that_names_a_missing_column_is_caught_before_any_data_is_touched(data):
    p = profile_file(data / "settlement_q1.csv")
    spec = copy.deepcopy(source("settlement_q1.csv").mapping)
    spec["ledger"]["order_ref"] = "no such column"
    check = verify(p, spec)
    assert not check.ok and "columns not in the file" in check.errors[0]