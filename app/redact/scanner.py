"""Scanner (milestone M5): find identifiers that are still present after tokenization.

`scan` walks the same structures as the tokenizer and reports
- any registry identifier still present (same matching rules as `app.redact.tokenizer`), and
- generic patterns that are PHI regardless of the registry: SSNs, e-mail addresses, US phone
  numbers with separators, and dates that sit next to "DOB" / "date of birth".

Findings never carry the full value: `value_preview` keeps the first two characters only.
The guard raises `RedactionError` on any finding; its message carries counts and kinds only.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterator
from datetime import date, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel

from app.enums import IdentifierKind
from app.redact.registry import IdentifierRegistry
from app.redact.tokenizer import TOKEN_RE, compile_registry
from app.schemas import RedactionFinding

PREVIEW_VISIBLE_CHARS = 2
PREVIEW_MAX_LENGTH = 32

_DATE_ALTERNATIVES = (
    r"\d{1,2}/\d{1,2}/\d{2,4}"
    r"|\d{1,2}-\d{1,2}-\d{2,4}"
    r"|\d{1,2}\.\d{1,2}\.\d{2,4}"
    r"|\d{4}-\d{2}-\d{2}"
    r"|\d{4}/\d{2}/\d{2}"
    r"|(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])(?:19|20)\d{2}"
    r"|(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])"
    r"|[A-Za-z]{3,9}\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{4}"
    r"|\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]{3,9}\.?,?\s+\d{4}"
)

# (kind, pattern, group holding the sensitive value). Group 0 = the whole match.
GENERIC_PATTERNS: list[tuple[str, re.Pattern[str], int]] = [
    ("SSN", re.compile(r"(?<![\dA-Za-z-])\d{3}-\d{2}-\d{4}(?![\dA-Za-z-])"), 0),
    ("EMAIL", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"), 0),
    # US phone numbers need visible separators so 10-digit NPIs and control numbers never match.
    (
        "PHONE",
        re.compile(
            r"(?<![\dA-Za-z])(?:\+?1[-.\s]?)?(?:\(\d{3}\)\s?|\d{3}[-.\s])\d{3}[-.\s]\d{4}(?![\dA-Za-z])"
        ),
        0,
    ),
    (
        "DOB",
        re.compile(
            r"(?<![A-Za-z])(?:dob|d\.o\.b\.?|date\s+of\s+birth|birth\s*date|born\s+on)(?![A-Za-z])"
            r"[^\n\d]{0,24}?(" + _DATE_ALTERNATIVES + r")(?!\d)",
            re.IGNORECASE,
        ),
        1,
    ),
]

_IDENTIFIER_KINDS: frozenset[str] = frozenset(kind.value for kind in IdentifierKind)


def mask_value(value: str) -> str:
    """Keep the first two characters, mask the rest, cap the length."""
    visible = value[:PREVIEW_VISIBLE_CHARS]
    hidden = max(len(value) - PREVIEW_VISIBLE_CHARS, 0)
    return (visible + "*" * hidden)[:PREVIEW_MAX_LENGTH]


def is_registry_kind(kind: str) -> bool:
    return kind in _IDENTIFIER_KINDS


class RedactionError(Exception):
    """Raised by the guard when a payload still contains identifiers. str() has counts and kinds only."""

    def __init__(self, findings: list[RedactionFinding]) -> None:
        self.findings = list(findings)
        counts = Counter(f.kind for f in self.findings)
        summary = ", ".join(f"{kind}={n}" for kind, n in sorted(counts.items()))
        super().__init__(f"redaction scan found {len(self.findings)} finding(s): {summary}")

    @property
    def counts(self) -> dict[str, int]:
        return dict(Counter(f.kind for f in self.findings))


def _token_spans(text: str) -> list[tuple[int, int]]:
    return [m.span() for m in TOKEN_RE.finditer(text)]


def _inside_token(span: tuple[int, int], token_spans: list[tuple[int, int]]) -> bool:
    start, end = span
    return any(start < t_end and end > t_start for t_start, t_end in token_spans)


def _iter_strings(value: Any, location: str) -> Iterator[tuple[str, str]]:
    """Yield (text, location) for every string nested in the value (keys included)."""
    if isinstance(value, str):
        yield value, location
    elif isinstance(value, BaseModel):
        yield from _iter_strings(value.model_dump(mode="json"), location)
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                yield key, f"{location}.<key>"
            yield from _iter_strings(item, f"{location}.{key}")
    elif isinstance(value, (list, tuple, set, frozenset)):
        items = sorted(value, key=str) if isinstance(value, (set, frozenset)) else value
        for i, item in enumerate(items):
            yield from _iter_strings(item, f"{location}[{i}]")
    elif isinstance(value, (datetime, date)):
        yield value.isoformat(), location
    elif isinstance(value, Enum):
        yield from _iter_strings(value.value, location)


def scan_text(
    text: str, registry: IdentifierRegistry | None, *, generic: bool = True, location: str = "$"
) -> list[RedactionFinding]:
    """Scan one string. Matches that fall inside `[[KIND_n]]` tokens are ignored."""
    findings: list[RedactionFinding] = []
    if not text:
        return findings
    token_spans = _token_spans(text)
    compiled = compile_registry(registry)
    if compiled.pattern is not None:
        for match in compiled.pattern.finditer(text):
            if _inside_token(match.span(), token_spans):
                continue
            ident = compiled.identifier_for(match.group(0))
            kind = ident.kind.value if ident is not None else "UNKNOWN"
            findings.append(
                RedactionFinding(kind=kind, value_preview=mask_value(match.group(0)), location=location)
            )
    if generic:
        for kind, pattern, group in GENERIC_PATTERNS:
            for match in pattern.finditer(text):
                span = match.span(group)
                if _inside_token(span, token_spans):
                    continue
                findings.append(
                    RedactionFinding(
                        kind=kind, value_preview=mask_value(match.group(group)), location=location
                    )
                )
    return findings


def scan(
    value: Any, registry: IdentifierRegistry | None, *, generic: bool = True, root: str = "$"
) -> list[RedactionFinding]:
    """Deep-walk `value` and report every identifier still present (see module docstring)."""
    findings: list[RedactionFinding] = []
    for text, location in _iter_strings(value, root):
        findings.extend(scan_text(text, registry, generic=generic, location=location))
    return findings


MASK_PREFIX = "MASKED_"


def mask_generic(text: str) -> tuple[str, dict[str, int]]:
    """Replace generic-pattern matches (SSN, e-mail, phone, DOB-adjacent dates) with `[[MASKED_KIND_n]]`.

    Defence in depth for values the registry cannot know about (another person's phone number
    quoted in a letter). The masks are not restorable and their names never collide with registry
    tokens; counts per `MASKED_<KIND>` are returned.
    """
    if not text:
        return text, {}
    token_spans = _token_spans(text)
    spans: list[tuple[int, int, str]] = []
    for kind, pattern, group in GENERIC_PATTERNS:
        for match in pattern.finditer(text):
            span = match.span(group)
            if span[0] == span[1] or _inside_token(span, token_spans):
                continue
            spans.append((span[0], span[1], kind))
    if not spans:
        return text, {}
    spans.sort()
    counts: dict[str, int] = {}
    out: list[str] = []
    cursor = 0
    for start, end, kind in spans:
        if start < cursor:  # overlapping match already masked
            continue
        mask_kind = MASK_PREFIX + kind
        counts[mask_kind] = counts.get(mask_kind, 0) + 1
        out.append(text[cursor:start])
        out.append(f"[[{mask_kind}_{counts[mask_kind]}]]")
        cursor = end
    out.append(text[cursor:])
    return "".join(out), counts


__all__ = [
    "GENERIC_PATTERNS",
    "MASK_PREFIX",
    "RedactionError",
    "is_registry_kind",
    "mask_generic",
    "mask_value",
    "scan",
    "scan_text",
]
