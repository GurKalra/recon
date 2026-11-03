"""Keep personal data out of prompts.

Only column NAMES and a handful of cell values are ever sent to the model. Values in columns
that could hold personal data are replaced by their shape ('Aaaaa Aaaaa', '99999 99999'), and
anything that looks like an email, phone number, PAN or Aadhaar number is masked wherever it
appears. The mapping and matching features do not need the real values to work.
"""

from __future__ import annotations

import re

_SENSITIVE_HEADER = re.compile(
    r"name|phone|mobile|contact|email|e-mail|address|addr|street|customer|buyer|consignee|"
    r"pan\b|aadhaar|aadhar|gstin|account|ifsc|upi|card", re.IGNORECASE)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}(?!\d)")
_PAN = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
_AADHAAR = re.compile(r"(?<!\d)\d{4}\s?\d{4}\s?\d{4}(?!\d)")


def is_sensitive_header(name: str) -> bool:
    return bool(_SENSITIVE_HEADER.search(name))


def shape(value: str) -> str:
    """'Asha Rao 98765' -> 'Aaaa Aaa 99999'. Keeps length and punctuation, drops the content."""
    out = []
    for ch in value:
        out.append("A" if ch.isupper() else "a" if ch.isalpha() else "9" if ch.isdigit() else ch)
    return "".join(out)


def mask_text(value: str) -> str:
    value = _EMAIL.sub("<email>", value)
    value = _PAN.sub("<pan>", value)
    value = _PHONE.sub("<phone>", value)
    return _AADHAAR.sub("<id-number>", value)


def safe_sample(header: str, value: str, max_len: int = 40) -> str:
    """A cell value that is safe to put in a prompt."""
    if is_sensitive_header(header):
        return shape(value)[:max_len]
    return mask_text(value)[:max_len]