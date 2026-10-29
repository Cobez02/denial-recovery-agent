"""Fact check for model-rewritten text: every number, amount, code and date in the output must
already exist in the inputs. Anything else is an invented fact and the template text is used instead.

`extract_facts` normalizes so that "1,200.00", "1200.00" and "$1200" are the same fact, and dates in
common formats (2026-05-01, 05/01/2026, May 1, 2026, 1 May 2026, 20260501) become ISO dates.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4, "april": 4,
    "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8, "august": 8, "sep": 9, "sept": 9,
    "september": 9, "oct": 10, "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}  # fmt: skip
_MONTH_RE = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"

_DATE_ISO = re.compile(r"(?<![\w.])(\d{4})-(\d{1,2})-(\d{1,2})(?![\w])")
_DATE_MDY = re.compile(r"(?<![\w.])(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})(?![\w])")
_DATE_MONTH_DY = re.compile(
    rf"(?<![\w.])({_MONTH_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})(?![\w])", re.IGNORECASE
)
_DATE_D_MONTH_Y = re.compile(
    rf"(?<![\w.])(\d{{1,2}})\s+({_MONTH_RE})\.?,?\s+(\d{{4}})(?![\w])", re.IGNORECASE
)
_DATE_CCYYMMDD = re.compile(r"(?<![\w.])((?:19|20)\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?![\w])")

_CODE_WITH_MODIFIER = re.compile(r"(?<![\w.])(\d{5}|[A-Z]\d{4})-([A-Z0-9]{2})(?![\w])")
_ICD10 = re.compile(r"(?<![\w.])([A-Z]\d{2}(?:\.\d{1,4})?)(?![\w])")
_CPT_HCPCS = re.compile(r"(?<![\w.])(\d{5}|[A-Z]\d{4})(?![\w])")
_NUMBER = re.compile(r"(?<![\w.])\d(?:[\d,./-]*\d)?(?![\w])")


def _safe_date(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def normalize_number(token: str) -> str:
    """'1,200.00' -> '1200'; '150.50' -> '150.5'; tokens that are not plain numbers are kept as-is."""
    cleaned = token.replace(",", "").strip()
    try:
        return format(Decimal(cleaned).normalize(), "f")
    except (InvalidOperation, ValueError):
        return cleaned


def extract_facts(text: str) -> set[str]:
    """Numbers and amounts, CPT/HCPCS codes, ICD-10 codes, dates (as ISO) and code-attached modifiers."""
    facts: set[str] = set()
    if not text:
        return facts
    work = text

    def blank(match: re.Match[str]) -> str:
        return " " * len(match.group(0))

    # Dates first, so their digits are not re-read as plain numbers.
    def iso(m: re.Match[str]) -> str:
        d = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            facts.add(d.isoformat())
            facts.add(str(d.year))
            return blank(m)
        return m.group(0)

    def mdy(m: re.Match[str]) -> str:
        year = int(m.group(3))
        year = year + 2000 if year < 100 else year
        d = _safe_date(year, int(m.group(1)), int(m.group(2)))
        if d:
            facts.add(d.isoformat())
            facts.add(str(d.year))
            return blank(m)
        return m.group(0)

    def month_dy(m: re.Match[str]) -> str:
        d = _safe_date(int(m.group(3)), _MONTHS[m.group(1).lower()], int(m.group(2)))
        if d:
            facts.add(d.isoformat())
            facts.add(str(d.year))
            return blank(m)
        return m.group(0)

    def d_month_y(m: re.Match[str]) -> str:
        d = _safe_date(int(m.group(3)), _MONTHS[m.group(2).lower()], int(m.group(1)))
        if d:
            facts.add(d.isoformat())
            facts.add(str(d.year))
            return blank(m)
        return m.group(0)

    def ccyymmdd(m: re.Match[str]) -> str:
        d = _safe_date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        if d:
            facts.add(d.isoformat())
            facts.add(str(d.year))
            return blank(m)
        return m.group(0)

    work = _DATE_ISO.sub(iso, work)
    work = _DATE_MDY.sub(mdy, work)
    work = _DATE_MONTH_DY.sub(month_dy, work)
    work = _DATE_D_MONTH_Y.sub(d_month_y, work)
    work = _DATE_CCYYMMDD.sub(ccyymmdd, work)

    def code_mod(m: re.Match[str]) -> str:
        facts.add(m.group(1))
        facts.add(m.group(2))
        facts.add(f"{m.group(1)}-{m.group(2)}")
        return blank(m)

    work = _CODE_WITH_MODIFIER.sub(code_mod, work)

    def icd(m: re.Match[str]) -> str:
        facts.add(m.group(1))
        return blank(m)

    work = _ICD10.sub(icd, work)

    def cpt(m: re.Match[str]) -> str:
        facts.add(m.group(1))
        return blank(m)

    work = _CPT_HCPCS.sub(cpt, work)

    for m in _NUMBER.finditer(work):
        facts.add(normalize_number(m.group(0)))
    return facts


def verify(output: str, inputs: str) -> list[str]:
    """Facts present in `output` but absent from `inputs`, sorted. Empty means the output is faithful."""
    return sorted(extract_facts(output) - extract_facts(inputs))


__all__ = ["extract_facts", "normalize_number", "verify"]
