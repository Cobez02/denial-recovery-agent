"""Free text of `denial-agent anonymize`: note excerpts and payer letters (docs/SPEC.md section 16).

For one patient (a :class:`~app.anonymize.pseudonyms.Subject`) every identifier the batch knows for
them is collected in an :class:`app.redact.registry.IdentifierRegistry`, spelled the ways the
registry spells them (``name_variants`` for every spelling met, each date of birth in every
``date_formats`` form and a few more, the same address combinations), next to the value written in
its place. The text is rewritten in one pass, longest identifier first, with the matching rules of
``app.redact.tokenizer`` (names and addresses as whole words, case-insensitive; ids as literal
substrings), except for two bounded kinds: an id shorter than six characters must stand alone (an
account number such as ``8941`` never rewrites the ``98941`` of a procedure code) and a date of birth
must not be part of a longer number or date (``1/16/1960`` is not inside ``11/16/1960``).

Replaced spans stay ``[[PSEUDONYM_n]]`` tokens until the very end, so the residual scan never mistakes
a pseudonym for a leftover. The scan has three parts, each blind to the tokens:

1. ``app.redact.scanner.scan_text`` with the patient's registry (anything it still finds was missed)
   and the scanner's generic patterns: SSN, e-mail, phone with separators, a date next to "DOB";
2. the bounded kinds, with their own boundaries;
3. the masks of ``app.logging_config`` for SSN, e-mail and phone shapes (a phone written without
   separators included), plus "DOB:", "SSN:" or "member id:" followed by a value with a digit. The
   log masks for plain dates and id-like tokens are not used: service dates and claim numbers belong
   in a note.

Numbers the batch itself names as a provider's, payer's, clinic's or claim's own (NPIs, tax ids, the
payer's phone, claim numbers) are not phone numbers: they are wrapped as ``[[KEEP_n]]`` before the
scan and written back unchanged. Whatever the scan finds is reported by kind; with
``allow_residual`` it is masked instead, with the unrestorable ``[[MASKED_<KIND>_n]]`` markers of
``app.redact.scanner.mask_generic``, and the text is scanned again to prove nothing is left.
"""

from __future__ import annotations

import itertools
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta

from app.anonymize.pseudonyms import AnonymizeError, Subject, match_case
from app.enums import IdentifierKind
from app.logging_config import _MASK_PATTERNS  # the log masks, reused as a second residual net
from app.redact.registry import KIND_PRIORITY, MONTH_NAMES, IdentifierRegistry, name_variants, normalize
from app.redact.scanner import MASK_PREFIX, mask_generic, scan_text
from app.redact.tokenizer import TOKEN_RE, compile_registry, identifier_regex
from app.schemas import Identifier

LONG_ID_LENGTH = 6
ID_KINDS: frozenset[IdentifierKind] = frozenset(
    {IdentifierKind.MEMBER_ID, IdentifierKind.ACCOUNT, IdentifierKind.CLAIM_ID}
)
_KIND_RANK = {kind: index for index, kind in enumerate(KIND_PRIORITY)}
MONTH_ABBREVIATIONS: tuple[str, ...] = tuple(month[:3] for month in MONTH_NAMES)


def _iso(d: date) -> str:
    return d.isoformat()


def _mm_dd_yyyy(d: date) -> str:
    return f"{d.month:02d}/{d.day:02d}/{d.year}"


def _m_d_yyyy(d: date) -> str:
    return f"{d.month}/{d.day}/{d.year}"


def _mm_dd_yy(d: date) -> str:
    return f"{d.month:02d}/{d.day:02d}/{d.year % 100:02d}"


def _m_d_yy(d: date) -> str:
    return f"{d.month}/{d.day}/{d.year % 100:02d}"


def _mmddyyyy(d: date) -> str:
    return f"{d.month:02d}{d.day:02d}{d.year}"


def _ccyymmdd(d: date) -> str:
    return f"{d.year}{d.month:02d}{d.day:02d}"


def _month_d_yyyy(d: date) -> str:
    return f"{MONTH_NAMES[d.month - 1]} {d.day}, {d.year}"


def _mm_dd_yyyy_dashes(d: date) -> str:
    return f"{d.month:02d}-{d.day:02d}-{d.year}"


def _m_d_yyyy_dashes(d: date) -> str:
    return f"{d.month}-{d.day}-{d.year}"


def _yyyy_mm_dd_slashes(d: date) -> str:
    return f"{d.year}/{d.month:02d}/{d.day:02d}"


def _d_month_yyyy(d: date) -> str:
    return f"{d.day} {MONTH_NAMES[d.month - 1]} {d.year}"


def _mon_d_yyyy(d: date) -> str:
    return f"{MONTH_ABBREVIATIONS[d.month - 1]} {d.day}, {d.year}"


def _month_dd_yyyy(d: date) -> str:
    return f"{MONTH_NAMES[d.month - 1]} {d.day:02d}, {d.year}"


def _mon_dd_yyyy(d: date) -> str:
    return f"{MONTH_ABBREVIATIONS[d.month - 1]} {d.day:02d}, {d.year}"


def _month_d_yyyy_no_comma(d: date) -> str:
    return f"{MONTH_NAMES[d.month - 1]} {d.day} {d.year}"


def _d_mon_yyyy(d: date) -> str:
    return f"{d.day} {MONTH_ABBREVIATIONS[d.month - 1]} {d.year}"


def _dd_mon_yyyy_dashes(d: date) -> str:
    return f"{d.day:02d}-{MONTH_ABBREVIATIONS[d.month - 1]}-{d.year}"


# Every spelling of a date of birth that is replaced in free text: the forms of
# app.redact.registry.date_formats, in its order, then zero-padded and comma-less month names.
DOB_FORMATS: tuple[Callable[[date], str], ...] = (
    _iso,
    _mm_dd_yyyy,
    _m_d_yyyy,
    _mm_dd_yy,
    _m_d_yy,
    _mmddyyyy,
    _ccyymmdd,
    _month_d_yyyy,
    _mm_dd_yyyy_dashes,
    _m_d_yyyy_dashes,
    _yyyy_mm_dd_slashes,
    _d_month_yyyy,
    _mon_d_yyyy,
    _month_dd_yyyy,
    _mon_dd_yyyy,
    _month_d_yyyy_no_comma,
    _d_mon_yyyy,
    _dd_mon_yyyy_dashes,
)


def dob_pairs(original: date, pseudonym: date) -> list[tuple[str, str]]:
    """(spelling of the original, the same spelling of the pseudonym) for every DOB_FORMATS form."""
    pairs: list[tuple[str, str]] = []
    seen: set[str] = set()
    for spell in DOB_FORMATS:
        text = spell(original)
        if text not in seen:
            seen.add(text)
            pairs.append((text, spell(pseudonym)))
    return pairs


def subject_pairs(subject: Subject) -> list[tuple[IdentifierKind, str, str]]:
    """Every (kind, original spelling, replacement) the batch knows for one patient."""
    pairs: list[tuple[IdentifierKind, str, str]] = []
    name = IdentifierKind.PATIENT_NAME
    for spelling in subject.spellings:
        originals = name_variants(spelling.first, spelling.last)
        pseudonyms = name_variants(spelling.pseudo_first, spelling.pseudo_last)
        pairs.extend((name, o, p) for o, p in zip(originals, pseudonyms, strict=True))
        if spelling.middle.strip() and spelling.first.strip() and spelling.last.strip():
            first, middle, last = spelling.first, spelling.middle, spelling.last
            p_first, p_middle, p_last = spelling.pseudo_first, spelling.pseudo_middle, spelling.pseudo_last
            pairs.append((name, f"{first} {middle} {last}", f"{p_first} {p_middle} {p_last}"))
            pairs.append((name, f"{last}, {first} {middle}", f"{p_last}, {p_first} {p_middle}"))
    pairs.extend((name, surface, pseudonym) for surface, pseudonym in subject.surfaces.items())
    for original, pseudonym in subject.ids.items():
        kind = IdentifierKind.ACCOUNT if original == subject.key else IdentifierKind.MEMBER_ID
        pairs.append((kind, original, pseudonym))
    for original, (pseudonym, _offset) in subject.dobs.items():
        pairs.extend((IdentifierKind.DOB, o, p) for o, p in dob_pairs(original, pseudonym))
    for record in subject.addresses:
        address = IdentifierKind.ADDRESS
        line1, city, state, zip_code = record.line1, record.city, record.state, record.zip
        p_line1, p_city, p_zip = record.pseudo_line1, record.pseudo_city, record.pseudo_zip
        pairs.extend((address, o, p) for o, p in ((line1, p_line1), (city, p_city), (zip_code, p_zip)) if o)
        if "," in line1:  # the street without the apartment
            pairs.append((address, line1.split(",", 1)[0], p_line1))
        if "-" in zip_code:  # ZIP+4: the five-digit ZIP alone
            pairs.append((address, zip_code.split("-", 1)[0], p_zip.split("-", 1)[0]))
        if city and zip_code:  # the combinations IdentifierRegistry.add_address registers
            if state:
                pairs.append((address, f"{city}, {state} {zip_code}", f"{p_city}, {state} {p_zip}"))
            else:
                pairs.append((address, f"{city} {zip_code}", f"{p_city} {p_zip}"))
            pairs.append((address, f"{city} {state} {zip_code}", f"{p_city} {state} {p_zip}"))
        if line1 and city:
            pairs.append((address, f"{line1}, {city}", f"{p_line1}, {p_city}"))
    pairs.extend((IdentifierKind.CLAIM_ID, o, p) for o, p in subject.claim_numbers.items())
    return pairs


# --------------------------------------------------------------------------------------
# Dates written in free text (only with --shift-dates)
# --------------------------------------------------------------------------------------

_MONTH_WORDS = sorted({*MONTH_NAMES, *MONTH_ABBREVIATIONS, "Sept"}, key=lambda word: (-len(word), word))
_MONTH_ALTERNATION = "|".join(_MONTH_WORDS)
TEXT_DATE_RE = re.compile(
    r"(?<![\w/.-])(?:"
    r"(?P<y1>\d{4})(?P<s1>[-/])(?P<m1>\d{1,2})(?P=s1)(?P<d1>\d{1,2})"
    r"|(?P<m2>\d{1,2})(?P<s2>[-/])(?P<d2>\d{1,2})(?P=s2)(?P<y2>\d{4}|\d{2})"
    rf"|(?P<mon3>{_MONTH_ALTERNATION})(?P<dot3>\.?)(?P<sp3>\s+)(?P<d3>\d{{1,2}})(?P<ord3>st|nd|rd|th)?"
    r"(?P<c3>,?)(?P<sp3b>\s+)(?P<y3>\d{4})"
    rf"|(?P<d4>\d{{1,2}})(?P<s4>[\s-])(?P<mon4>{_MONTH_ALTERNATION})(?P<dot4>\.?)(?P<c4>,?)(?P<s4b>[\s-])"
    r"(?P<y4>\d{4})"
    r")(?![\w/-])",
    re.IGNORECASE,
)


def _number(template: str, value: int) -> str:
    return f"{value:02d}" if len(template) == 2 else str(value)


def _year(template: str) -> int:
    value = int(template)
    if len(template) == 2:
        return 2000 + value if value < 70 else 1900 + value
    return value


def _render_year(template: str, value: int) -> str:
    return f"{value % 100:02d}" if len(template) == 2 else str(value)


def _month_index(word: str) -> int:
    lowered = word.lower()
    for index, month in enumerate(MONTH_NAMES):
        if lowered == month.lower() or lowered == month[:3].lower() or (lowered == "sept" and index == 8):
            return index + 1
    raise ValueError("not a month")


_FULL_MONTH_NAMES = frozenset(month.lower() for month in MONTH_NAMES)


def _month_word(template: str, month: int) -> str:
    """The month written like ``template``: a full name for a full name, else three letters."""
    full = MONTH_NAMES[month - 1]
    return match_case(template, full if template.lower() in _FULL_MONTH_NAMES else full[:3])


def _ordinal(day: int) -> str:
    if 10 <= day % 100 <= 20:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")


def _shift_date_match(match: re.Match[str], days: int) -> str | None:
    """The matched date moved by ``days``, written the way it was written; None if not a date."""
    g = match.groupdict()
    try:
        if g["y1"] is not None:
            d = date(int(g["y1"]), int(g["m1"]), int(g["d1"])) + timedelta(days=days)
            sep = g["s1"]
            return f"{d.year}{sep}{_number(g['m1'], d.month)}{sep}{_number(g['d1'], d.day)}"
        if g["m2"] is not None:
            d = date(_year(g["y2"]), int(g["m2"]), int(g["d2"])) + timedelta(days=days)
            sep = g["s2"]
            return f"{_number(g['m2'], d.month)}{sep}{_number(g['d2'], d.day)}{sep}{_render_year(g['y2'], d.year)}"
        if g["mon3"] is not None:
            d = date(int(g["y3"]), _month_index(g["mon3"]), int(g["d3"])) + timedelta(days=days)
            ordinal = match_case(g["ord3"], _ordinal(d.day)) if g["ord3"] else ""
            return (
                f"{_month_word(g['mon3'], d.month)}{g['dot3']}{g['sp3']}{_number(g['d3'], d.day)}{ordinal}"
                f"{g['c3']}{g['sp3b']}{d.year}"
            )
        d = date(int(g["y4"]), _month_index(g["mon4"]), int(g["d4"])) + timedelta(days=days)
        return (
            f"{_number(g['d4'], d.day)}{g['s4']}{_month_word(g['mon4'], d.month)}{g['dot4']}{g['c4']}"
            f"{g['s4b']}{d.year}"
        )
    except (ValueError, OverflowError):  # 02/30/2026, a year out of range: not a date, left alone
        return None


def _overlaps(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < span_end and end > span_start for span_start, span_end in spans)


def _token_spans(text: str) -> list[tuple[int, int]]:
    return [m.span() for m in TOKEN_RE.finditer(text)]


def shift_text_dates(text: str, days: int) -> tuple[str, int]:
    """Move every date written in ``text`` by ``days`` (tokens left alone); returns (text, count)."""
    if not days or not text:
        return text, 0
    tokens = _token_spans(text)
    count = 0

    def _replace(match: re.Match[str]) -> str:
        nonlocal count
        if _overlaps(*match.span(), tokens):
            return match.group(0)
        shifted = _shift_date_match(match, days)
        if shifted is None:
            return match.group(0)
        count += 1
        return shifted

    return TEXT_DATE_RE.sub(_replace, text), count


# --------------------------------------------------------------------------------------
# Numbers that are not phone numbers, and the residual scan
# --------------------------------------------------------------------------------------

# Anything the scanner's or the log masks' phone patterns could take for a phone number.
PHONE_LIKE_RE = re.compile(
    r"(?<![\dA-Za-z])(?:\+?1[-.\s]?)?(?:\(\d{3}\)\s?|\d{3}[-.\s]?)\d{3}[-.\s]?\d{4}(?![\dA-Za-z])"
)


def phone_digits(value: str) -> str:
    """Digits of a phone-like value, without a leading country code 1."""
    digits = "".join(ch for ch in value if ch.isdigit())
    return digits[1:] if len(digits) == 11 and digits.startswith("1") else digits


_LOG_MASK_KINDS: dict[str, str] = {"[SSN]": "SSN", "[EMAIL]": "EMAIL", "[PHONE]": "PHONE"}
_LOG_LABEL_KINDS: dict[str, str] = {
    "dob": "DOB",
    "date of birth": "DOB",
    "ssn": "SSN",
    "member id": "MEMBER_ID",
    "member_id": "MEMBER_ID",
}
_TRAILING_VALUE_RE = re.compile(r"\S+$")


@dataclass
class TextResult:
    text: str
    replaced: int = 0
    dates_shifted: int = 0
    residual: list[str] = field(default_factory=list)  # kinds, in text order
    masked: int = 0


class TextReplacer:
    """Rewrites one patient's free text (see the module docstring)."""

    def __init__(
        self,
        pairs: Iterable[tuple[IdentifierKind, str, str]],
        *,
        keep_numbers: frozenset[str] = frozenset(),
    ) -> None:
        self.registry = IdentifierRegistry()
        self.scan_registry = IdentifierRegistry()  # what scan_text checks: all but the bounded ones
        self.keep_numbers = keep_numbers
        self._replacements: dict[str, str] = {}
        bounded: list[Identifier] = []
        for kind, original, pseudonym in sorted(pairs, key=lambda pair: _KIND_RANK[pair[0]]):
            if not self.registry.add(kind, original):
                continue
            ident = Identifier(kind=kind, value=" ".join(original.split()))
            self._replacements.setdefault(normalize(original), pseudonym)
            if _bounded_regex(ident) is not None:
                bounded.append(ident)
            else:
                self.scan_registry.add(kind, original)
        identifiers = self.registry.iter_longest_first()
        self._identifiers = identifiers
        self._pattern = _alternation(identifiers)
        self._bounded = {normalize(ident.value): ident.kind.value for ident in bounded}
        self._bounded_pattern = _alternation(sorted(bounded, key=lambda ident: -len(ident.value)))

    def _replacement(self, matched: str) -> str | None:
        replacement = self._replacements.get(normalize(matched))
        if replacement is None:  # exotic spacing or casing: the identifier whose regex fits
            for ident in self._identifiers:
                if re.fullmatch(_regex(ident), matched, re.IGNORECASE):
                    replacement = self._replacements.get(normalize(ident.value))
                    break
        return None if replacement is None else match_case(matched, replacement)

    # -- residual scan ----------------------------------------------------------------------

    def _bounded_findings(self, text: str, tokens: list[tuple[int, int]]) -> list[tuple[int, int, str]]:
        """Bounded identifiers (short ids, dates of birth) outside tokens, with their spans."""
        found: list[tuple[int, int, str]] = []
        if self._bounded_pattern is not None:
            for match in self._bounded_pattern.finditer(text):
                if not _overlaps(*match.span(), tokens):
                    found.append((*match.span(), self._bounded.get(normalize(match.group(0)), "UNKNOWN")))
        return found

    def _extra_findings(self, text: str) -> list[tuple[int, int, str]]:
        """Bounded identifiers and log-mask hits outside tokens, with spans."""
        tokens = _token_spans(text)
        found = self._bounded_findings(text, tokens)
        for pattern, label in _MASK_PATTERNS:
            for match in pattern.finditer(text):
                if label in _LOG_MASK_KINDS:
                    start, end, kind = *match.span(), _LOG_MASK_KINDS[label]
                elif match.lastindex and match.group(1).lower() in _LOG_LABEL_KINDS:
                    value = _TRAILING_VALUE_RE.search(match.group(0))
                    if value is None or not any(ch.isdigit() for ch in value.group(0)):
                        continue  # "DOB: unknown", "member id: on file"
                    start, end = match.start() + value.start(), match.start() + value.end()
                    kind = _LOG_LABEL_KINDS[match.group(1).lower()]
                else:
                    continue  # plain dates and id-like tokens are expected in notes and letters
                if not _overlaps(start, end, tokens):
                    found.append((start, end, kind))
        return found

    def remaining(self, text: str) -> list[str]:
        """Kinds of this replacer's own identifiers still in ``text``, without the generic patterns:
        a restore uses it to prove that no pseudonym is left."""
        kinds = [finding.kind for finding in scan_text(text, self.scan_registry, generic=False)]
        kinds.extend(kind for _start, _end, kind in self._bounded_findings(text, _token_spans(text)))
        return kinds

    def residual(self, text: str) -> list[str]:
        """Kinds of everything left in ``text`` that is, or looks like, an identifier."""
        kinds = [finding.kind for finding in scan_text(text, self.scan_registry, generic=True)]
        kinds.extend(kind for _start, _end, kind in self._extra_findings(text))
        return kinds

    def _mask(self, text: str) -> tuple[str, int]:
        """Mask every residual: the scanner's generic patterns first, then the rest by span."""
        text, numbering = mask_generic(text)
        masked = sum(numbering.values())
        tokens = _token_spans(text)
        spans = list(self._extra_findings(text))
        compiled = compile_registry(self.scan_registry)
        if compiled.pattern is not None:
            for match in compiled.pattern.finditer(text):
                if not _overlaps(*match.span(), tokens):
                    ident = compiled.identifier_for(match.group(0))
                    spans.append((*match.span(), ident.kind.value if ident is not None else "UNKNOWN"))
        out: list[str] = []
        cursor = 0
        for start, end, kind in sorted(spans):
            if start < cursor:  # overlaps a span already masked
                continue
            label = MASK_PREFIX + kind
            numbering[label] = numbering.get(label, 0) + 1
            out.append(text[cursor:start])
            out.append(f"[[{label}_{numbering[label]}]]")
            cursor = end
            masked += 1
        out.append(text[cursor:])
        return "".join(out), masked

    # -- the whole rewrite --------------------------------------------------------------------

    def process(
        self, text: str, *, shift_days: int = 0, allow_residual: bool = False, scan: bool = True
    ) -> TextResult:
        """Replace identifiers, keep known numbers, shift dates, scan, mask when allowed, render.

        ``scan=False`` (a restore, where real identifiers are the point) skips the residual scan.
        """
        if not text:
            return TextResult(text=text)
        existing = {m.group(1) for m in TOKEN_RE.finditer(text)}
        pseudonym_label = _free_label("PSEUDONYM", existing)
        keep_label = _free_label("KEEP", existing)
        tokens: dict[str, str] = {}
        counter = itertools.count(1)
        replaced = 0

        def _token(label: str, value: str) -> str:
            name = f"[[{label}_{next(counter)}]]"
            tokens[name] = value
            return name

        def _identifier(match: re.Match[str]) -> str:
            nonlocal replaced
            replacement = self._replacement(match.group(0))
            if replacement is None:  # cannot happen for a registry match; the scan would report it
                return match.group(0)
            replaced += 1
            return _token(pseudonym_label, replacement)

        if self._pattern is not None:
            text = self._pattern.sub(_identifier, text)
        if self.keep_numbers:
            spans = _token_spans(text)

            def _keep(match: re.Match[str]) -> str:
                if _overlaps(*match.span(), spans) or phone_digits(match.group(0)) not in self.keep_numbers:
                    return match.group(0)
                return _token(keep_label, match.group(0))

            text = PHONE_LIKE_RE.sub(_keep, text)
        text, shifted = shift_text_dates(text, shift_days)
        residual = self.residual(text) if scan else []
        masked = 0
        if residual and allow_residual:
            text, masked = self._mask(text)
            if self.residual(text):
                raise AnonymizeError("masking residual identifiers left some behind")
        rendered = TOKEN_RE.sub(lambda m: tokens.get(m.group(0), m.group(0)), text)
        return TextResult(
            text=rendered, replaced=replaced, dates_shifted=shifted, residual=residual, masked=masked
        )


def _bounded_regex(ident: Identifier) -> str | None:
    """A short id must stand alone and a date of birth must not be part of a longer number or date
    (``1/16/1960`` inside ``11/16/1960``); every other identifier follows the tokenizer's rule."""
    if ident.kind in ID_KINDS and len(normalize(ident.value)) < LONG_ID_LENGTH:
        return r"(?<![A-Za-z0-9])" + re.escape(ident.value) + r"(?![A-Za-z0-9])"
    if ident.kind == IdentifierKind.DOB:
        return r"(?<!\d)" + re.escape(ident.value) + r"(?!\d)"
    return None


def _regex(ident: Identifier) -> str:
    bounded = _bounded_regex(ident)
    return bounded if bounded is not None else identifier_regex(ident)


def _alternation(identifiers: list[Identifier]) -> re.Pattern[str] | None:
    if not identifiers:
        return None
    return re.compile("|".join(_regex(ident) for ident in identifiers), re.IGNORECASE)


def _free_label(label: str, existing: set[str]) -> str:
    """A token label the text does not already use (a letter could quote one)."""
    while label in existing:
        label += "_X"
    return label


__all__ = [
    "DOB_FORMATS",
    "LONG_ID_LENGTH",
    "TEXT_DATE_RE",
    "TextReplacer",
    "TextResult",
    "dob_pairs",
    "phone_digits",
    "shift_text_dates",
    "subject_pairs",
]
