"""Identifier registry (milestone M5): every value that must never reach a model for one claim.

The registry only *collects* values and their kinds. Matching rules (word boundaries for names,
literal matching for ids and dates) live in `app.redact.tokenizer`; `app.redact.scanner` reuses
them so the tokenizer and the scanner never disagree about what counts as a leftover.

Build one registry per claim with `IdentifierRegistry.from_context(ctx)` (pipeline) or
`IdentifierRegistry.from_patient(patient, claims, remittance_claims=...)` (CLI / re-processing).
Both register the payer's own spelling of the patient and member id from the 835 (NM1*QC) next to
the clinic's record: a payer letter uses the payer's spelling, and a maiden name or an alternate
member id must be tokenized just like the one the clinic exported.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from app.enums import IdentifierKind
from app.schemas import Identifier

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app import models
    from app.schemas import DenialContext

# Values shorter than these are never registered. Ids, dates and address parts need three
# characters (a two-letter state code or "12" would shred ordinary text). Names are matched as
# whole words, so two-letter names are kept: Li, Wu, He, Ng, Ed or Al are real surnames and first
# names, and a word-bounded match of "li" or "ed" costs little.
MIN_VALUE_LENGTH = 3
MIN_NAME_LENGTH = 2
NAME_KINDS: frozenset[IdentifierKind] = frozenset({IdentifierKind.PATIENT_NAME, IdentifierKind.PROVIDER_NAME})

MONTH_NAMES: tuple[str, ...] = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
MONTH_ABBREVIATIONS: tuple[str, ...] = tuple(m[:3] for m in MONTH_NAMES)

# Tie-break when two identifiers have the same length: the more sensitive kind is tried first.
KIND_PRIORITY: tuple[IdentifierKind, ...] = (
    IdentifierKind.PATIENT_NAME,
    IdentifierKind.MEMBER_ID,
    IdentifierKind.DOB,
    IdentifierKind.ACCOUNT,
    IdentifierKind.CLAIM_ID,
    IdentifierKind.ADDRESS,
    IdentifierKind.PROVIDER_NAME,
    IdentifierKind.DATE,
)
_KIND_RANK: dict[IdentifierKind, int] = {kind: i for i, kind in enumerate(KIND_PRIORITY)}

_WHITESPACE = re.compile(r"\s+")


def normalize(value: str) -> str:
    """Comparison form shared by the registry, tokenizer and scanner: trimmed, single-spaced, lower."""
    return _WHITESPACE.sub(" ", str(value).strip()).lower()


def date_formats(d: date) -> list[str]:
    """Every spelling of a date that may appear in claim exports, 835 files, letters and notes.

    Covers the formats required by docs/SPEC.md section 8 (YYYY-MM-DD, MM/DD/YYYY, M/D/YYYY,
    MMDDYYYY, CCYYMMDD, 'Month D, YYYY') plus a few that payer letters and notes commonly use,
    including the two-digit-year spellings MM/DD/YY and M/D/YY.
    """
    if isinstance(d, datetime):
        d = d.date()
    month_name = MONTH_NAMES[d.month - 1]
    month_abbr = MONTH_ABBREVIATIONS[d.month - 1]
    forms = [
        d.isoformat(),  # YYYY-MM-DD
        f"{d.month:02d}/{d.day:02d}/{d.year}",  # MM/DD/YYYY
        f"{d.month}/{d.day}/{d.year}",  # M/D/YYYY
        f"{d.month:02d}/{d.day:02d}/{d.year % 100:02d}",  # MM/DD/YY
        f"{d.month}/{d.day}/{d.year % 100:02d}",  # M/D/YY
        f"{d.month:02d}{d.day:02d}{d.year}",  # MMDDYYYY
        f"{d.year}{d.month:02d}{d.day:02d}",  # CCYYMMDD
        f"{month_name} {d.day}, {d.year}",  # Month D, YYYY
        f"{d.month:02d}-{d.day:02d}-{d.year}",  # MM-DD-YYYY
        f"{d.month}-{d.day}-{d.year}",  # M-D-YYYY
        f"{d.year}/{d.month:02d}/{d.day:02d}",  # YYYY/MM/DD
        f"{d.day} {month_name} {d.year}",  # D Month YYYY
        f"{month_abbr} {d.day}, {d.year}",  # Mon D, YYYY
    ]
    seen: set[str] = set()
    unique: list[str] = []
    for form in forms:
        if form not in seen:
            seen.add(form)
            unique.append(form)
    return unique


def name_variants(first: str | None, last: str | None) -> list[str]:
    """'First Last', 'Last, First', 'Last First' plus each part alone (matching is case-insensitive)."""
    first = _WHITESPACE.sub(" ", (first or "").strip())
    last = _WHITESPACE.sub(" ", (last or "").strip())
    variants: list[str] = []
    if first and last:
        variants.extend([f"{first} {last}", f"{last}, {first}", f"{last} {first}"])
    if first:
        variants.append(first)
    if last:
        variants.append(last)
    return variants


_HONORIFIC_RE = re.compile(r"^(?:dr\.?|doctor|mr\.?|mrs\.?|ms\.?|prof\.?)\s+", re.IGNORECASE)
# Trailing credentials and generational suffixes, with or without a comma: ", MD", " PA-C", ", M.D.", " Jr."
_CREDENTIAL_RE = re.compile(
    r"[\s,]+(?:M\.?D|D\.?O|N\.?P|P\.?A(?:-C)?|D\.?P\.?M|R\.?N|F\.?N\.?P|A\.?P\.?R\.?N|D\.?N\.?P|Ph\.?D"
    r"|M\.?B\.?B\.?S|C\.?N\.?P|L\.?P\.?N|M\.?P\.?H|F\.?A\.?A\.?D|F\.?A\.?C\.?S|Jr|Sr|II|III|IV)\.?\s*$",
    re.IGNORECASE,
)


def _strip_credentials(text: str) -> str:
    while True:
        stripped = _CREDENTIAL_RE.sub("", text)
        if stripped == text:
            return text.strip(" ,")
        text = stripped


def provider_name_variants(name: str | None) -> list[str]:
    """Every spelling of a provider name worth tokenizing.

    "Priya Thornquist, MD", "Thornquist, Priya MD" and "Dr. Priya Thornquist" all yield the raw
    string, the name without honorific and credentials, 'First Last', 'Last, First' (with and
    without middle names) and the surname alone, so "Dr. Thornquist" and "per Thornquist" in a
    note are caught too. Matching is word-bounded and case-insensitive, so the surname alone never
    matches inside another word.
    """
    raw = _WHITESPACE.sub(" ", (name or "").strip())
    if not raw:
        return []
    variants: list[str] = [raw]

    def _add(value: str) -> None:
        value = _WHITESPACE.sub(" ", value.strip(" ,"))
        if value and value.lower() not in {v.lower() for v in variants}:
            variants.append(value)

    core = _strip_credentials(_HONORIFIC_RE.sub("", raw))
    if not core:
        return variants
    _add(core)
    if "," in core:  # "Last, First [Middle]"
        last, first = (part.strip() for part in core.split(",", 1))
        first = _strip_credentials(first)
    else:  # "First [Middle] Last"
        tokens = core.split(" ")
        if len(tokens) < 2:
            return variants
        first, last = " ".join(tokens[:-1]), tokens[-1]
    if not first or not last:
        return variants
    _add(f"{first} {last}")
    _add(f"{last}, {first}")
    first_token = first.split(" ")[0]
    if first_token != first:  # drop middle names and initials
        _add(f"{first_token} {last}")
        _add(f"{last}, {first_token}")
    _add(last)
    return variants


class IdentifierRegistry:
    """Ordered, de-duplicated set of (kind, value) pairs for one claim."""

    def __init__(self, identifiers: Iterable[Identifier] | None = None) -> None:
        self._values: dict[IdentifierKind, list[str]] = {kind: [] for kind in IdentifierKind}
        self._seen: set[tuple[IdentifierKind, str]] = set()
        self._order: list[Identifier] = []
        for ident in identifiers or ():
            self.add(ident.kind, ident.value)

    # -- building -----------------------------------------------------------------------------

    def add(self, kind: IdentifierKind | str, value: Any) -> bool:
        """Register one value. Returns True when it was new. Dates are expanded to every format.

        Values shorter than MIN_VALUE_LENGTH characters (MIN_NAME_LENGTH for names) and
        duplicates (case-insensitive, whitespace-normalized) are ignored.
        """
        if value is None:
            return False
        kind = IdentifierKind(kind)
        if isinstance(value, date):
            return self.add_date(kind, value) > 0
        text = _WHITESPACE.sub(" ", str(value).strip())
        if len(text) < (MIN_NAME_LENGTH if kind in NAME_KINDS else MIN_VALUE_LENGTH):
            return False
        key = (kind, text.lower())
        if key in self._seen:
            return False
        self._seen.add(key)
        self._values[kind].append(text)
        self._order.append(Identifier(kind=kind, value=text))
        return True

    def add_date(self, kind: IdentifierKind | str, value: date | datetime | None) -> int:
        """Register every spelling of a date. Returns the number of new values."""
        if value is None:
            return 0
        kind = IdentifierKind(kind)
        if isinstance(value, datetime):
            value = value.date()
        return sum(1 for form in date_formats(value) if self.add(kind, form))

    def add_name(
        self, first: str | None, last: str | None, kind: IdentifierKind = IdentifierKind.PATIENT_NAME
    ) -> int:
        return sum(1 for variant in name_variants(first, last) if self.add(kind, variant))

    def add_provider(self, name: str | None) -> int:
        return sum(
            1 for variant in provider_name_variants(name) if self.add(IdentifierKind.PROVIDER_NAME, variant)
        )

    def add_address(
        self, line1: str | None, city: str | None, state: str | None, zip_code: str | None
    ) -> int:
        added = 0
        for part in (line1, city, zip_code):
            if self.add(IdentifierKind.ADDRESS, part):
                added += 1
        city_s = (city or "").strip()
        state_s = (state or "").strip()
        zip_s = (zip_code or "").strip()
        if "-" in zip_s:  # ZIP+4: the five-digit ZIP alone is just as identifying
            if self.add(IdentifierKind.ADDRESS, zip_s.split("-", 1)[0]):
                added += 1
        if city_s and zip_s:
            combos = [
                f"{city_s}, {state_s} {zip_s}" if state_s else f"{city_s} {zip_s}",
                f"{city_s} {state_s} {zip_s}",
            ]
            for combo in combos:
                if self.add(IdentifierKind.ADDRESS, combo):
                    added += 1
        if line1 and city_s:
            if self.add(IdentifierKind.ADDRESS, f"{str(line1).strip()}, {city_s}"):
                added += 1
        return added

    def merge(self, other: IdentifierRegistry) -> IdentifierRegistry:
        for ident in other.identifiers():
            self.add(ident.kind, ident.value)
        return self

    # -- reading ------------------------------------------------------------------------------

    def values(self, kind: IdentifierKind | str) -> list[str]:
        return list(self._values[IdentifierKind(kind)])

    def identifiers(self) -> list[Identifier]:
        return list(self._order)

    def iter_longest_first(self) -> list[Identifier]:
        """Identifiers sorted longest first; ties by kind sensitivity, then insertion order."""
        indexed = list(enumerate(self._order))
        indexed.sort(key=lambda pair: (-len(pair[1].value), _KIND_RANK[pair[1].kind], pair[0]))
        return [ident for _, ident in indexed]

    @property
    def is_empty(self) -> bool:
        return not self._order

    def fingerprint(self) -> tuple[tuple[str, str], ...]:
        """Hashable identity of the registry content (used to cache compiled patterns)."""
        return tuple((ident.kind.value, ident.value.lower()) for ident in self._order)

    def __len__(self) -> int:
        return len(self._order)

    def __iter__(self):
        return iter(self._order)

    def __contains__(self, value: object) -> bool:
        if isinstance(value, Identifier):
            return (value.kind, normalize(value.value)) in self._seen
        if isinstance(value, str):
            needle = normalize(value)
            return any(needle == key for _, key in self._seen)
        return False

    def __repr__(self) -> str:  # counts only, never values
        counts = {kind.value: len(vals) for kind, vals in self._values.items() if vals}
        return f"IdentifierRegistry({counts})"

    # -- constructors -------------------------------------------------------------------------

    @classmethod
    def from_context(cls, ctx: DenialContext) -> IdentifierRegistry:
        """Everything in a DenialContext that identifies the patient, the claim or a person."""
        registry = cls()
        claim = ctx.claim
        ids = claim.patient_identifiers
        if ids is not None:
            registry.add_name(ids.first_name, ids.last_name)
            registry.add(IdentifierKind.MEMBER_ID, ids.member_id)
            registry.add(IdentifierKind.ACCOUNT, ids.account_number)
            registry.add_address(ids.address_line1, ids.city, ids.state, ids.zip)
            registry.add_date(IdentifierKind.DOB, ids.dob)

        # The payer's spelling of the patient and member id (835 NM1*QC / NM1*IL), every variant.
        registry.add_name(ctx.remittance.patient_first_name, ctx.remittance.patient_last_name)
        registry.add(IdentifierKind.MEMBER_ID, ctx.remittance.member_id)

        registry.add(IdentifierKind.CLAIM_ID, claim.claim_control_number)
        registry.add(IdentifierKind.CLAIM_ID, ctx.remittance.payer_claim_control_number)
        for line in claim.lines:
            registry.add(IdentifierKind.CLAIM_ID, line.line_item_control_number)
        registry.add_provider(claim.rendering_provider_name)

        for d in (claim.service_date_from, claim.service_date_to, claim.original_submission_date):
            registry.add_date(IdentifierKind.DATE, d)
        for line in claim.lines:
            registry.add_date(IdentifierKind.DATE, line.service_date)
        registry.add_date(IdentifierKind.DATE, ctx.remittance.payment_date)
        registry.add_date(IdentifierKind.DATE, ctx.remittance.claim_received_date)
        for note in ctx.notes:
            registry.add_date(IdentifierKind.DATE, note.note_date)
            registry.add_provider(note.author)
        for doc in ctx.documents:
            registry.add_date(IdentifierKind.DATE, doc.received_date)
        return registry

    @classmethod
    def from_patient(
        cls,
        patient: models.Patient,
        claims: Iterable[models.Claim] | None = None,
        *,
        remittance_claims: Iterable[models.RemittanceClaim] | None = None,
    ) -> IdentifierRegistry:
        """Registry for one patient row, any of their claims and any of those claims' remittance
        claims (ORM objects); the remittance rows add the payer's spelling of the patient."""
        registry = cls()
        registry.add_name(patient.first_name, patient.last_name)
        registry.add(IdentifierKind.MEMBER_ID, patient.member_id)
        registry.add(IdentifierKind.ACCOUNT, patient.account_number)
        registry.add_address(patient.address_line1, patient.city, patient.state, patient.zip)
        registry.add_date(IdentifierKind.DOB, patient.dob)
        for rc in remittance_claims or ():
            registry.add_name(rc.patient_first_name, rc.patient_last_name)
            registry.add(IdentifierKind.MEMBER_ID, rc.member_id)
            registry.add(IdentifierKind.CLAIM_ID, rc.payer_claim_control_number)

        for claim in claims or ():
            registry.add(IdentifierKind.CLAIM_ID, claim.claim_control_number)
            registry.add_provider(claim.rendering_provider_name)
            for d in (claim.service_date_from, claim.service_date_to, claim.original_submission_date):
                registry.add_date(IdentifierKind.DATE, d)
            original = getattr(claim, "original_claim", None)
            if original is not None:
                registry.add(IdentifierKind.CLAIM_ID, original.claim_control_number)
            for line in _safe_iter(claim, "lines"):
                registry.add_date(IdentifierKind.DATE, line.service_date)
                registry.add(IdentifierKind.CLAIM_ID, line.line_item_control_number)
            for note in _safe_iter(claim, "notes"):
                registry.add_date(IdentifierKind.DATE, note.note_date)
                registry.add_provider(note.author)
            for doc in _safe_iter(claim, "documents"):
                registry.add_date(IdentifierKind.DATE, doc.received_date)
        return registry


def _safe_iter(obj: Any, attribute: str) -> list[Any]:
    """Relationship access that tolerates detached ORM objects (returns [] instead of raising)."""
    try:
        return list(getattr(obj, attribute, None) or [])
    except Exception:  # pragma: no cover - DetachedInstanceError and friends
        return []


__all__ = [
    "IdentifierRegistry",
    "KIND_PRIORITY",
    "MIN_NAME_LENGTH",
    "MIN_VALUE_LENGTH",
    "MONTH_NAMES",
    "NAME_KINDS",
    "date_formats",
    "name_variants",
    "normalize",
    "provider_name_variants",
]
