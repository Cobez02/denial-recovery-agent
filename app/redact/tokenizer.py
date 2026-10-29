"""Tokenizer (milestone M5): replace registry identifiers with `[[KIND_n]]` tokens and restore them.

Matching rules
- Names, provider names and address parts: case-insensitive, bounded by non-alphanumerics on both
  sides, tolerant of repeated whitespace inside the value ("Smith,  John").
- Member ids, account numbers, claim control numbers and dates: literal, case-insensitive
  substring match (a claim number inside "CLM-000123-R1" is still a claim number).
- Longest identifiers win: every identifier is compiled into one alternation ordered longest
  first and the text is rewritten in a single pass, so tokens are never re-scanned and
  "Smith, John" is one PATIENT_NAME token rather than two.

The same original (kind + value, case-insensitive) always maps to the same token within one
`redact` call. Tokens use only `[`, `]`, `_`, uppercase letters and digits, so they survive JSON,
YAML and Jinja rendering unchanged.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel

from app.enums import IdentifierKind
from app.redact.registry import IdentifierRegistry, normalize
from app.schemas import Identifier, RedactionMap

# Kinds matched as whole words/phrases rather than as raw substrings.
WORD_BOUNDED_KINDS: frozenset[IdentifierKind] = frozenset(
    {IdentifierKind.PATIENT_NAME, IdentifierKind.PROVIDER_NAME, IdentifierKind.ADDRESS}
)

TOKEN_RE = re.compile(r"\[\[([A-Z][A-Z_]*?)_(\d+)\]\]")

_LEFT_BOUNDARY = r"(?<![A-Za-z0-9])"
_RIGHT_BOUNDARY = r"(?![A-Za-z0-9])"


def token_for(kind: IdentifierKind | str, index: int) -> str:
    return f"[[{IdentifierKind(kind).value}_{index}]]"


def identifier_regex(identifier: Identifier) -> str:
    """Regex source for one identifier (compiled with re.IGNORECASE by the caller)."""
    if identifier.kind in WORD_BOUNDED_KINDS:
        parts = [re.escape(part) for part in identifier.value.split()]
        return _LEFT_BOUNDARY + r"\s+".join(parts) + _RIGHT_BOUNDARY
    return re.escape(identifier.value)


class CompiledRegistry:
    """One alternation over every identifier of a registry, longest first."""

    def __init__(self, identifiers: list[Identifier]) -> None:
        self.identifiers = identifiers
        self.pattern: re.Pattern[str] | None = None
        self._lookup: dict[str, Identifier] = {}
        self._fallback: list[tuple[re.Pattern[str], Identifier]] = []
        if identifiers:
            self.pattern = re.compile("|".join(identifier_regex(i) for i in identifiers), re.IGNORECASE)
            for ident in identifiers:
                self._lookup.setdefault(normalize(ident.value), ident)
                self._fallback.append((re.compile(identifier_regex(ident), re.IGNORECASE), ident))

    def identifier_for(self, matched: str) -> Identifier | None:
        ident = self._lookup.get(normalize(matched))
        if ident is not None:
            return ident
        for pattern, candidate in self._fallback:  # exotic casing; the alternation matched, so one fits
            if pattern.fullmatch(matched):
                return candidate
        return None


_COMPILE_CACHE: dict[tuple[tuple[str, str], ...], CompiledRegistry] = {}
_COMPILE_CACHE_LIMIT = 64


def compile_registry(registry: IdentifierRegistry | None) -> CompiledRegistry:
    if registry is None:
        return CompiledRegistry([])
    key = registry.fingerprint()
    compiled = _COMPILE_CACHE.get(key)
    if compiled is None:
        if len(_COMPILE_CACHE) >= _COMPILE_CACHE_LIMIT:
            _COMPILE_CACHE.clear()
        compiled = CompiledRegistry(registry.iter_longest_first())
        _COMPILE_CACHE[key] = compiled
    return compiled


class _Redactor:
    def __init__(self, compiled: CompiledRegistry) -> None:
        self._compiled = compiled
        self._tokens_by_identity: dict[tuple[str, str], str] = {}
        self._counters: dict[str, int] = {}
        self.map = RedactionMap()

    def _token(self, ident: Identifier) -> str:
        key = (ident.kind.value, normalize(ident.value))
        token = self._tokens_by_identity.get(key)
        if token is None:
            n = self._counters.get(ident.kind.value, 0) + 1
            self._counters[ident.kind.value] = n
            token = token_for(ident.kind, n)
            self._tokens_by_identity[key] = token
            self.map.tokens[token] = ident.value
        return token

    def _replace(self, match: re.Match[str]) -> str:
        ident = self._compiled.identifier_for(match.group(0))
        if ident is None:  # pragma: no cover - the alternation only matches registry values
            return match.group(0)
        self.map.counts[ident.kind.value] = self.map.counts.get(ident.kind.value, 0) + 1
        return self._token(ident)

    def __call__(self, text: str) -> str:
        pattern = self._compiled.pattern
        if pattern is None or not text:
            return text
        return pattern.sub(self._replace, text)


def walk(value: Any, fn: Callable[[str], str], *, coerce: bool = True) -> Any:
    """Apply `fn` to every string nested in dicts, lists, tuples and Pydantic models.

    With `coerce` (used by `redact`), Pydantic models are dumped in JSON mode, dates become ISO
    strings (so they can be tokenized), Decimals become strings and enums become their values.
    Dict keys are left untouched.
    """
    if isinstance(value, str):
        return fn(value)
    if isinstance(value, BaseModel):
        return walk(value.model_dump(mode="json"), fn, coerce=coerce)
    if isinstance(value, dict):
        return {key: walk(item, fn, coerce=coerce) for key, item in value.items()}
    if isinstance(value, list):
        return [walk(item, fn, coerce=coerce) for item in value]
    if isinstance(value, tuple):
        return tuple(walk(item, fn, coerce=coerce) for item in value)
    if not coerce:
        return value
    if isinstance(value, (set, frozenset)):
        return [walk(item, fn, coerce=coerce) for item in sorted(value, key=str)]
    if isinstance(value, (datetime, date)):
        return fn(value.isoformat())
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Enum):
        return walk(value.value, fn, coerce=coerce)
    return value


def redact(value: Any, registry: IdentifierRegistry | None) -> tuple[Any, RedactionMap]:
    """Tokenize every registry identifier inside `value` (str / dict / list / tuple / Pydantic).

    Returns the redacted copy and the RedactionMap (token -> original, counts per kind).
    """
    redactor = _Redactor(compile_registry(registry))
    redacted = walk(value, redactor, coerce=True)
    return redacted, redactor.map


tokenize = redact


def restore(value: Any, redaction_map: RedactionMap | None) -> Any:
    """Put the originals back for every known token nested in strings, dicts, lists and tuples."""
    if redaction_map is None or not redaction_map.tokens:
        return value
    tokens = redaction_map.tokens

    def _restore_text(text: str) -> str:
        return TOKEN_RE.sub(lambda m: tokens.get(m.group(0), m.group(0)), text)

    return walk(value, _restore_text, coerce=False)


def find_tokens(text: str) -> list[str]:
    return [m.group(0) for m in TOKEN_RE.finditer(text)]


__all__ = [
    "TOKEN_RE",
    "WORD_BOUNDED_KINDS",
    "CompiledRegistry",
    "compile_registry",
    "find_tokens",
    "identifier_regex",
    "redact",
    "restore",
    "token_for",
    "tokenize",
    "walk",
]
