"""CARC / RARC catalog access (docs/SPEC.md section 4.3).

The catalog lives in ``codes.yaml`` next to this module: two maps, ``carc`` and ``rarc``, from
code to a short human-readable text paraphrased from the public X12 code lists. It is loaded
once per process. Codes are normalized before lookup (whitespace stripped, upper-cased, numeric
codes without leading zeros) so ``"n4"``, ``" N4 "`` and ``"N4"`` all resolve.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.schemas import DenialReason

CODES_PATH = Path(__file__).resolve().parent / "codes.yaml"


def normalize_code(code: Any) -> str:
    """Canonical form of a CARC/RARC: stripped, upper-cased, no leading zeros on numeric codes.

    Only ASCII digit strings count as numeric (``"016"`` -> ``"16"``, ``"000"`` -> ``"0"``).
    Anything else, including the Unicode digits a hostile file could carry, is returned as text
    so it never aliases a real code and never raises.
    """
    text = str(code).strip().upper() if code is not None else ""
    if text.isascii() and text.isdigit():
        text = text.lstrip("0") or "0"
    return text


@lru_cache(maxsize=1)
def _catalog() -> dict[str, dict[str, str]]:
    """Load codes.yaml once. Returns ``{"carc": {code: text}, "rarc": {code: text}}``."""
    with CODES_PATH.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ValueError("codes.yaml must be a mapping with 'carc' and 'rarc' keys")
    catalog: dict[str, dict[str, str]] = {}
    for kind in ("carc", "rarc"):
        entries = raw.get(kind) or {}
        if not isinstance(entries, dict):
            raise ValueError(f"codes.yaml: '{kind}' must be a mapping of code -> text")
        table: dict[str, str] = {}
        for code, text in entries.items():
            key = normalize_code(code)
            if not key:
                raise ValueError(f"codes.yaml: empty {kind} code")
            if not isinstance(text, str) or not text.strip():
                raise ValueError(f"codes.yaml: {kind} {key} has no text")
            table[key] = text.strip()
        catalog[kind] = table
    return catalog


def carc_text(code: str) -> str | None:
    """Human-readable text for a CARC, or None when the catalog does not know the code."""
    return _catalog()["carc"].get(normalize_code(code))


def rarc_text(code: str) -> str | None:
    """Human-readable text for a RARC, or None when the catalog does not know the code."""
    return _catalog()["rarc"].get(normalize_code(code))


def all_codes() -> dict[str, frozenset[str]]:
    """Every catalogued code, by kind: ``{"carc": frozenset[...], "rarc": frozenset[...]}``."""
    return {kind: frozenset(table) for kind, table in _catalog().items()}


def describe(reason: DenialReason) -> DenialReason:
    """Return a copy of ``reason`` with ``carc_text`` and ``rarc_texts`` filled from the catalog.

    Existing texts are kept; the catalog only fills gaps. Unknown codes stay without text.
    The input is not mutated.
    """
    carc = reason.carc_text or carc_text(reason.carc)
    rarc_texts = dict(reason.rarc_texts)
    for code in reason.rarcs:
        key = str(code)
        if rarc_texts.get(key):
            continue
        text = rarc_text(key)
        if text:
            rarc_texts[key] = text
    return reason.model_copy(update={"carc_text": carc, "rarc_texts": rarc_texts})


def enrich_reasons(reasons: list[DenialReason]) -> list[DenialReason]:
    """``describe`` every reason, preserving order. Returns a new list; inputs are not mutated."""
    return [describe(r) for r in reasons]


__all__ = [
    "CODES_PATH",
    "all_codes",
    "carc_text",
    "describe",
    "enrich_reasons",
    "normalize_code",
    "rarc_text",
]
