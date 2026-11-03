"""Suggest a column mapping for a partner file nobody has mapped yet.

    1. Profile the file (header row, footer, what each column looks like).
    2. Rules propose a mapping (see rules.py) and the real importer is run on the first rows
       to prove it works: the arithmetic of the file, not the model, is the judge.
    3. If the rules are unsure or the check fails, the model is asked, shown the masked profile,
       the rules' attempt with its errors, and the closest mappings people already approved.
       Its answer goes through the same check, with up to two repair rounds that quote the
       errors back to it. The best verified candidate wins; ties go to the rules.

Whatever comes out is only a SUGGESTION: a person previews it on 20 rows and approves it.
Without an API key, or if the service is down, step 3 is skipped and the rules answer stands.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from app.ai.llm import LLMBadOutput, LLMClient, LLMUnavailable
from app.ai.profile import FileProfile, profile_file
from app.ai.rules import Check, Draft, Hint, Note, suggest_with_rules, verify
from app.ingest.catalog import SOURCES
from app.ingest.mapping import MappingSpec, header_fingerprint

MAX_REPAIRS = 2
MAPPING_ANSWER_TOKENS = 2500   # a mapping is a few hundred tokens; do not reserve more

SYSTEM = """You map a business partner's spreadsheet export onto a fixed schema so a
reconciliation tool can import it. You are given a profile of the file (column names exactly as
the tool spells them, what the values look like, a few masked sample values) and sometimes an
earlier attempt with the errors it produced. Answer with ONE JSON object and nothing else.

Answer format:
{"spec": <mapping>, "notes": [{"field": str, "value": str, "confidence": "high|medium|low",
 "reason": str}], "unmapped": [column names you deliberately left out]}

Mapping format. Every column name must be copied exactly from the profile (they are lower-case).
{"kind": "ledger" | "orders" | "receipts",
 "format": {"kind": "csv" | "xlsx", "header_row": <0-based int>,
            "footer_markers": [<first-cell text of footer rows such as "Total">], "sheet": 0},
 "ledger": {"order_ref": col | omit, "awb": col | omit, "reference": col | omit (UTR/invoice no.),
            "date": {"column": col, "format": "iso" | "dmy" | "dmony"},
            "emitters": [ ... ], "net_check": {"column": col, "sign": 1 | -1} | omit},
 "orders": {"order_ref","channel","ordered_on":{"column","format"},"payment_mode","value",
            "delivery_status" (required); "pincode","weight_g","length_cm","width_cm",
            "height_cm","courier","awb","delivered_on":{"column","format"},
            "returned_on":{"column","format"} (optional)},
 "receipts": {"order_ref", "received_on": {"column","format"}, "condition" (optional)}}
Only the block named by "kind" is present. Dates: iso = 2026-11-02 (a time after it is fine),
dmy = 02/11/2026, dmony = 02-Nov-2026.

Ledger emitters turn amount columns into money lines. Two shapes:
 wide: one emitter per amount column: {"amount": col, "line_type": T, "sign": 1|-1,
       "skip_zero": bool}
 long: one amount column whose meaning is in label column(s):
       {"amount": col, "sign": 1, "line_type_from": [col, ...],
        "line_type_map": {"<label>": T}}  (with two label columns the key is "<a>|<b>"; copy
       labels exactly as they appear)
Line types T: sale, refund, commission, fixed_fee, shipping_fee, return_fee, pg_fee, freight,
return_freight, cod_fee, tcs, tds, adjustment.

Signs: lines are from the BRAND's point of view. Money coming in (sale) is positive; every fee,
tax, charge and refund is negative. If the file prints a deduction as a positive number use
sign -1; if it already prints it negative use sign 1. net_check says: the sum over emitters of
sign x amount must equal net_check.sign x the net column on every row; use it whenever the file
has a net/total/remitted column, because it proves the signs. Use skip_zero true for optional
fee columns that are often 0 or blank. Do not invent columns. If something cannot be mapped,
leave it out and say so in "unmapped"."""


@dataclass
class Suggestion:
    spec: dict | None
    source: str                      # rules | llm | llm_repaired | none
    check: Check
    notes: list[Note] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    unmapped: list[str] = field(default_factory=list)
    attempts: list[str] = field(default_factory=list)   # what was tried, for the audit trail
    llm_model: str | None = None
    llm_tokens: int = 0
    # the model's own best answer, even when the rules' answer was kept (for measuring the model)
    model_candidate: Suggestion | None = field(default=None, repr=False)


# ---- prompt pieces ------------------------------------------------------------------------------


def _profile_for_prompt(p: FileProfile) -> dict:
    cols = []
    for c in p.columns:
        item: dict = {"name": c.name, "looks_like": c.kind, "filled": round(c.filled, 2),
                      "samples": c.samples[:4]}
        if c.kind == "enum":
            item["values"] = c.values
        if c.kind == "number":
            item.update(negative_share=round(c.negative_share, 2),
                        zero_share=round(c.zero_share, 2))
        cols.append(item)
    return {"header_row": p.header_row, "file_type": p.format.kind,
            "footer_markers": list(p.format.footer_markers), "rows": p.rows, "columns": cols}


def nearest_examples(profile: FileProfile, hint: Hint, k: int = 1,
                     extra: list[tuple[list[str], dict]] | None = None
                     ) -> list[tuple[list[str], dict]]:
    """The k approved mappings whose columns look most like this file's. A mapping for exactly
    this header row is never an example: it would simply be the answer."""
    mine = set(profile.names())
    pool = list(extra or [])
    for s in SOURCES:
        spec = MappingSpec.model_validate(s.mapping)
        pool.append((spec.referenced_columns(), s.mapping))
    scored = []
    for cols, spec in pool:
        if header_fingerprint(cols) == profile.table.fingerprint:
            continue
        kind = spec.get("kind")
        if hint.source_kind and ((hint.source_kind == "orders") != (kind == "orders")
                                 or (hint.source_kind == "warehouse_receipt")
                                 != (kind == "receipts")):
            continue
        union = mine | set(cols)
        scored.append((len(mine & set(cols)) / len(union) if union else 0.0, cols, spec))
    scored.sort(key=lambda t: -t[0])
    return [(cols, spec) for _, cols, spec in scored[:k]]


def _user_prompt(profile: FileProfile, hint: Hint, draft: Draft, check: Check,
                 examples: list[tuple[list[str], dict]]) -> str:
    parts = [
        "FILE PROFILE:\n" + json.dumps(_profile_for_prompt(profile), ensure_ascii=False),
        "WHAT THE UPLOADER KNOWS: " + json.dumps(
            {"source_kind": hint.source_kind, "partner_type": hint.partner_type}),
    ]
    if draft.spec:
        parts.append("AN EARLIER, RULE-BASED ATTEMPT (may be wrong or incomplete):\n"
                     + json.dumps(draft.spec))
        if check.errors:
            parts.append("WHEN THAT ATTEMPT WAS RUN ON THE FILE IT FAILED WITH:\n"
                         + "\n".join(check.errors))
    if draft.warnings:
        parts.append("OPEN QUESTIONS: " + "; ".join(draft.warnings))
    for cols, spec in examples:
        parts.append("EXAMPLE of an approved mapping for a different file with columns "
                     f"{cols}:\n" + json.dumps(spec))
    parts.append("Produce the best mapping for the FILE PROFILE above.")
    return "\n\n".join(parts)


# ---- the model step -----------------------------------------------------------------------------


def _notes_from(raw: object) -> list[Note]:
    out = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, dict):
            conf = str(item.get("confidence", "medium"))
            out.append(Note(str(item.get("field", "")), str(item.get("value", "")),
                            conf if conf in ("high", "medium", "low") else "medium",
                            str(item.get("reason", ""))[:200]))
    return out


def _unproven(spec: dict | None) -> list[str]:
    """A wide ledger mapping with no net_check has signs nobody has verified."""
    ledger = (spec or {}).get("ledger")
    if ledger and "net_check" not in ledger \
            and not any(e.get("line_type_from") for e in ledger.get("emitters", [])):
        return ["signs could not be confirmed by a net column"]
    return []


def _rank(s: Suggestion) -> tuple:
    """Lower is better: 0 verified and proven, 1 verified but with open questions, 2 failed."""
    if s.spec is None or not s.check.ok:
        return (2, s.check.reject_rate)
    return (1 if s.warnings else 0, s.check.reject_rate)


def _ask_model(llm: LLMClient, profile: FileProfile, hint: Hint, draft: Draft, check: Check,
               examples: list[tuple[list[str], dict]], partner_types: dict[str, str],
               attempts: list[str]) -> tuple[Suggestion | None, int]:
    """Up to 1 + MAX_REPAIRS model calls. Returns the best verified candidate it produced."""
    prompt = _user_prompt(profile, hint, draft, check, examples)
    best: Suggestion | None = None
    tokens = 0
    for round_no in range(MAX_REPAIRS + 1):
        try:
            result = llm.complete_json(SYSTEM, prompt, max_tokens=MAPPING_ANSWER_TOKENS)
        except LLMBadOutput as exc:
            attempts.append(f"model round {round_no + 1}: unusable reply ({exc})")
            prompt += f"\n\nYour previous reply was unusable: {exc}. Reply with one JSON object."
            continue
        tokens += int(result.usage.get("total_tokens")
                      or (result.usage.get("prompt_tokens", 0)
                          + result.usage.get("completion_tokens", 0)))
        spec = result.data.get("spec")
        spec = spec if isinstance(spec, dict) else None
        got = verify(profile, spec, partner_types=partner_types)
        attempts.append(f"model round {round_no + 1}: "
                        + ("verified" if got.ok else "; ".join(got.errors)[:200]))
        cand = Suggestion(spec, "llm" if round_no == 0 else "llm_repaired", got,
                          _notes_from(result.data.get("notes")), _unproven(spec),
                          [str(u) for u in result.data.get("unmapped", [])
                           if isinstance(u, str)], attempts, result.model, tokens)
        if best is None or _rank(cand) < _rank(best):
            best = cand
        if got.ok and not cand.warnings:
            break
        prompt += ("\n\nYOUR PREVIOUS ANSWER:\n" + json.dumps(spec)
                   + "\nFAILED WITH:\n" + "\n".join(got.errors)
                   + "\nFix it and answer again with one JSON object.")
    return best, tokens


# ---- entry point --------------------------------------------------------------------------------


def suggest_mapping(path: Path, hint: Hint | None = None, use_llm: bool = True,
                    llm: LLMClient | None = None, partner_types: dict[str, str] | None = None,
                    extra_examples: list[tuple[list[str], dict]] | None = None,
                    force_llm: bool = False) -> Suggestion:
    hint = hint or Hint()
    partner_types = partner_types or {}
    profile = profile_file(Path(path))
    draft = suggest_with_rules(profile, hint)
    check = verify(profile, draft.spec, partner_types=partner_types)
    attempts = ["rules: " + ("verified" if check.ok else "; ".join(check.errors)[:200])]
    best = Suggestion(draft.spec, "rules" if draft.spec else "none", check, draft.notes,
                      draft.warnings, draft.unmapped, attempts)

    unsure = (not check.ok) or draft.spec is None or bool(draft.warnings)
    if use_llm and (unsure or force_llm):
        client = llm or LLMClient.from_settings()
        if client.available:
            examples = nearest_examples(profile, hint, extra=extra_examples)
            try:
                cand, _ = _ask_model(client, profile, hint, draft, check, examples,
                                     partner_types, attempts)
            except LLMUnavailable as exc:
                attempts.append(f"model unavailable ({exc}); rules answer kept")
                best.warnings.append(f"the AI service was unavailable ({exc}), so this is "
                                     "rules-only")
                cand = None
            kept = best
            if cand is not None and (_rank(cand) < _rank(best)
                                     or (best.spec is None and cand.spec is not None)):
                best = cand
            elif cand is not None:
                attempts.append("rules answer kept (the model was not better)")
            best.model_candidate = cand
            if best is not kept:
                kept.model_candidate = cand
        else:
            attempts.append("no API key: rules only")
    return best