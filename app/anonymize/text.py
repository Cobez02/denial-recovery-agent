"""Free text of `denial-agent anonymize` and `deanonymize` (docs/SPEC.md section 16).

A :class:`TextIndex` holds every identifier of the batch, of *every* patient, next to the value
written in its place: a note about one patient that names another (a family member who is also a
patient, a recoupment letter listing several members) gets that patient's own pseudonyms, and the
row's own patient wins wherever two patients share a spelling. Matching is done on folded text
(``app.anonymize.matching``) with these rules:

- **Names** as the batch spells them and in every join of their parts (``O'Brien``, ``O’Brien``,
  ``O Brien``, ``OBrien``; ``Quiñonero-Vélez``, ``Quinonero Velez``, ``QuinoneroVelez``), each part
  alone, full names (``First Last``, ``Last, First``, ``First M Last``, ``F. Last``, ``Last, F.``,
  ``First L.``). A name part that is also an ordinary word (``Low``, ``May``, ``Will``, ``Ray``: see
  :data:`app.anonymize.wordlists.COMMON_WORDS`) or has two letters or fewer is replaced only in a
  name context: capitalized, not joined to another word by a hyphen (``X-Ray``), and next to
  another part of the same name or after a title or label (``Mrs.``, ``Patient:``).
- **Provider and organization names** the batch carries (rendering providers, note authors, the
  835's provider, payer and payee names) are kept as they are, so a patient who shares a surname
  with their chiropractor never turns the chiropractor's name into a pseudonym. A bare surname
  shared by both is kept unless a title or the patient's first name says it is the patient.
- **Ids** with or without separators (``NW-458-049-016``, ``NW 458049016``), and the digits of an
  alphanumeric id alone (``100019`` of ``ACC100019``); an id shorter than six characters only when it
  stands alone and is not one of the batch's procedure or diagnosis codes.
- **Dates of birth** in every form :data:`app.anonymize.matching.TEXT_DATE_RE` reads, written back in
  the same form; a date that is also one of the batch's service, payment or note dates only when
  labelled as a date of birth.
- **Street addresses** with USPS abbreviations (``Court``/``Ct``/``Crt``, ``North``/``N``) and with or
  without the unit; a **city** or **ZIP code** only in an address context (before a state code or a
  ZIP, after ``ZIP``), and never a ZIP that is one of the batch's procedure codes.

Replaced spans stay ``[[PSEUDONYM_n]]`` tokens until the end, so the residual scan never mistakes a
pseudonym for a leftover. The scan (:func:`residual_findings`) looks for identifiers the batch does
not know: SSNs (with or without dashes after a label), e-mail addresses, phone numbers, dates
labelled as a date of birth, Medicare MBIs, and ids after a label (MRN, chart, account, policy,
subscriber, member, ID#, MBI, HICN, Medicare or Medicaid id). Numbers the batch names as a
provider's, payer's, clinic's or claim's own (NPIs, tax ids, the payer's phone, claim numbers) are
kept and never reported. With ``allow_residual`` every finding is masked as a whole value with an
unrestorable ``[[MASKED_<KIND>_n]]`` marker and the text is scanned again.
"""

from __future__ import annotations

import itertools
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, timedelta

from app.anonymize.matching import (
    ID_SEPARATORS,
    NAME_SEPARATORS,
    Matcher,
    alnum_key,
    date_matches,
    date_values,
    fold,
    fold_map,
    id_steps,
    in_dob_context,
    name_key,
    name_steps,
    original_span,
    render_date_like,
    words_steps,
)
from app.anonymize.pseudonyms import (
    FAMILY,
    GIVEN,
    NAME_SUFFIXES,
    NAME_TOKEN_RE,
    AnonymizeError,
    Subject,
    match_case,
)
from app.anonymize.wordlists import COMMON_WORDS, FREQUENT_WORDS
from app.enums import IdentifierKind
from app.redact.registry import provider_name_variants

NAME = IdentifierKind.PATIENT_NAME.value
MEMBER_ID = IdentifierKind.MEMBER_ID.value
ACCOUNT = IdentifierKind.ACCOUNT.value
CLAIM_ID = IdentifierKind.CLAIM_ID.value
DOB = IdentifierKind.DOB.value
ADDRESS = IdentifierKind.ADDRESS.value

LONG_ID_LENGTH = 6
MIN_DIGIT_CORE = 6
FULL, PART, VALUE, STREET, CITY, ZIP = "full", "part", "value", "street", "city", "zip"
TOKEN_RE = re.compile(r"\[\[([A-Z][A-Z_]*?)_(\d+)\]\]")
MASK_PREFIX = "MASKED_"


@dataclass(frozen=True)
class Entry:
    """One spelling to find: what it is, whose it is and what is written in its place."""

    kind: str
    owner: str  # subject key; "" for claim numbers, which belong to the batch
    replacement: str
    form: str = PART
    ambiguous: bool = False


@dataclass(frozen=True)
class _Keep:
    strong: bool


@dataclass
class _Candidate:
    start: int
    end: int
    rank: int  # on equal spans the lower rank wins
    entry: Entry | None = None
    replacement: str = ""
    keep: bool = False


@dataclass(frozen=True)
class Hit:
    """An identifier found in a text: where and what kind, never the value."""

    start: int
    end: int
    kind: str
    owner: str


@dataclass
class TextResult:
    text: str
    replaced: int = 0
    other_patients: int = 0
    dates_shifted: int = 0
    residual: list[str] = field(default_factory=list)  # kinds, in text order
    masked: int = 0


# --------------------------------------------------------------------------------------
# Addresses
# --------------------------------------------------------------------------------------

_USPS_GROUPS: tuple[tuple[str, ...], ...] = (
    ("street", "st", "str"),
    ("avenue", "ave", "av"),
    ("road", "rd"),
    ("drive", "dr", "drv"),
    ("lane", "ln"),
    ("court", "ct", "crt"),
    ("boulevard", "blvd"),
    ("place", "pl"),
    ("terrace", "ter", "terr"),
    ("circle", "cir"),
    ("parkway", "pkwy"),
    ("highway", "hwy"),
    ("way", "wy"),
    ("trail", "trl"),
    ("square", "sq"),
    ("crossing", "xing"),
    ("hollow", "holw"),
    ("heights", "hts"),
    ("point", "pt"),
    ("ridge", "rdg"),
    ("creek", "crk"),
    ("grove", "grv"),
    ("mount", "mt"),
    ("center", "ctr"),
    ("meadow", "mdw"),
    ("north", "n"),
    ("south", "s"),
    ("east", "e"),
    ("west", "w"),
    ("northeast", "ne"),
    ("northwest", "nw"),
    ("southeast", "se"),
    ("southwest", "sw"),
)
_USPS: dict[str, tuple[str, ...]] = {word: group for group in _USPS_GROUPS for word in group}
UNIT_RE = re.compile(
    r"(?:,|\s)\s*(?:#|apt\b\.?|apartment\b|unit\b|ste\b\.?|suite\b|no\.|bldg\b\.?|building\b|fl\b\.?|floor\b"
    r"|rm\b\.?|room\b|lot\b|space\b|spc\b)\s*#?\s*[\w-]+\.?\s*$",
    re.IGNORECASE,
)
MAX_STREET_VARIANTS = 48


def street_without_unit(line1: str) -> str:
    """The street of an address line without its apartment, suite or unit."""
    match = UNIT_RE.search(line1)
    if match is not None:
        return line1[: match.start()].rstrip(" ,")
    return line1.split(",", 1)[0].strip()


def street_variants(line1: str) -> list[str]:
    """Steps for a street line with every USPS spelling of its suffix and direction words."""
    words = [w.strip(".").lower() for w in fold(street_without_unit(line1)).split() if w.strip(".")]
    if len(words) < 2:
        return []
    options = [list(_USPS.get(word, (word,))) for word in words]
    variants: list[str] = []
    for combination in itertools.product(*options):
        variants.append(words_steps(*combination))
        if len(variants) >= MAX_STREET_VARIANTS:
            break
    return variants


# --------------------------------------------------------------------------------------
# Name context
# --------------------------------------------------------------------------------------

_TITLE_RE = re.compile(
    r"(?:\b(?:mr|mrs|ms|miss|mx|mister)\.?"
    r"|\b(?:patient|pt|member|insured|subscriber|guarantor|name|re|son|daughter|mother|father|wife"
    r"|husband|spouse|parent|guardian|child|brother|sister)\b\s*[:#-]?)\s*$",
    re.IGNORECASE,
)
_STRICT_TITLE_RE = re.compile(
    r"(?:\b(?:mr|mrs|ms|miss|mx|mister)\.?|\b(?:patient|member|insured|subscriber|guarantor|name|re)\s*:)\s*$",
    re.IGNORECASE,
)
_PROVIDER_TITLE_RE = re.compile(r"\b(?:dr|doctor)\.?\s*$", re.IGNORECASE)
_STATE_BEFORE_RE = re.compile(r"\b[A-Z]{2}\.?,?\s{1,3}$")
_ZIP_LABEL_RE = re.compile(r"\b(?:zip|postal)(?:\s*code)?\b[^\n\d]{0,14}$", re.IGNORECASE)
_AFTER_CITY_RE = re.compile(r"^,?\s{0,3}(?:[A-Z]{2}\b|\d{5}\b)")
_CITY_LABEL_RE = re.compile(r"\bcity\s*[:#]?\s*$", re.IGNORECASE)


def _line_before(text: str, start: int, width: int = 40) -> str:
    line_start = text.rfind("\n", 0, start) + 1
    return text[max(line_start, start - width) : start]


def _line_after(text: str, end: int, width: int = 40) -> str:
    line_end = text.find("\n", end)
    line_end = len(text) if line_end < 0 else line_end
    return text[end : min(line_end, end + width)]


def _attached(text: str, start: int, end: int) -> bool:
    """Joined to another word by a hyphen or a slash: the ``Ray`` of ``X-Ray``, ``Low-grade``."""
    if start >= 2 and text[start - 1] in "-/" and text[start - 2].isalnum():
        return True
    return end + 1 < len(text) and text[end] in "-/" and text[end + 1].isalnum()


def _capitalized(surface: str) -> bool:
    words = [w for w in re.split(r"[\s,.'’-]+", surface) if w]
    return bool(words) and all(w[0].isupper() for w in words)


def _upper_line(text: str, start: int, end: int) -> bool:
    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    line = text[line_start : len(text) if line_end < 0 else line_end]
    letters = [ch for ch in line if ch.isalpha()]
    return len(letters) >= 4 and all(ch.isupper() for ch in letters)


_AFTER_WORD_RE = re.compile(r"[\s,.]{1,3}([^\W\d_]+(?:['’][^\W\d_]+)*)")
_BEFORE_WORD_RE = re.compile(r"([^\W\d_]+(?:['’][^\W\d_]+)*)[\s,.]{1,3}$")


def _neighbour_keys(text: str, start: int, end: int) -> list[str]:
    """Name keys of the capitalized words just before and just after a span (across a short gap),
    unless a hyphen joins them to another word (the ``Ray`` of ``X-Ray`` is no neighbour)."""
    keys: list[str] = []
    after = _AFTER_WORD_RE.match(text, end)
    if after is not None:
        word_start, word_end = after.span(1)
        if text[word_start].isupper() and not _attached(text, word_start, word_end):
            keys.append(name_key(after.group(1)))
    window_start = max(0, start - 40)
    before = _BEFORE_WORD_RE.search(text[window_start:start])
    if before is not None:
        word_start, word_end = window_start + before.start(1), window_start + before.end(1)
        if text[word_start].isupper() and not _attached(text, word_start, word_end):
            keys.append(name_key(before.group(1)))
    return keys


# --------------------------------------------------------------------------------------
# The index
# --------------------------------------------------------------------------------------


def lowercase_corpus(texts: Iterable[str]) -> Counter[str]:
    """For every word a text writes in lower case, the number of texts that do: such a word is an
    ordinary word of this batch, whatever else it may be."""
    counts: Counter[str] = Counter()
    for text in texts:
        if text:
            counts.update({word for word in re.findall(r"(?<![\w'’-])[^\W\d_A-Z]+(?![\w'’-])", fold(text))})
    return counts


class TextIndex:
    """Every identifier to find in free text, with its replacement (see the module docstring)."""

    def __init__(
        self,
        *,
        keep_numbers: frozenset[str] = frozenset(),
        data_dates: frozenset[date] = frozenset(),
        codes: frozenset[str] = frozenset(),
        corpus: Counter[str] | None = None,
    ) -> None:
        self.names = Matcher(NAME_SEPARATORS)
        self.ids = Matcher(ID_SEPARATORS)
        self.places = Matcher(NAME_SEPARATORS)
        self.keep_names = Matcher(NAME_SEPARATORS)
        self.keep_ids = Matcher(ID_SEPARATORS)
        self.dobs: dict[date, list[tuple[str, date]]] = {}
        self.owner_tokens: dict[str, set[str]] = {}
        self.keep_numbers = keep_numbers
        self.data_dates = data_dates
        self.codes = frozenset(alnum_key(code) for code in codes if code)
        self.corpus = corpus or Counter()

    # -- building -------------------------------------------------------------------------

    def ambiguous(self, key: str) -> bool:
        """A name part that is also an ordinary word of the batch, or too short to stand alone."""
        if len(key) <= 2 or key in FREQUENT_WORDS:
            return True
        count = self.corpus.get(key, 0)
        return (key in COMMON_WORDS and count > 0) or count >= 3

    def _owned(self, owner: str, keys: Iterable[str]) -> None:
        self.owner_tokens.setdefault(owner, set()).update(k for k in keys if len(k) > 1)

    def add_name(self, surface: str, replacement: str, owner: str, *, form: str = PART) -> None:
        """A name: one field or token (``PART``) or a person's full name (``FULL``). A single token
        that is an ordinary word, or a full name made only of such tokens, is ambiguous; a name of
        several parts (``Low-Smith``) never is."""
        steps = words_steps(*surface.split()) if form == FULL else name_steps(surface)
        keys = [k for k in (name_key(t) for t in NAME_TOKEN_RE.findall(surface)) if k]
        if not steps or not keys:
            return
        ambiguous = (form == FULL or len(keys) == 1) and all(self.ambiguous(k) for k in keys)
        self.names.add(steps, Entry(NAME, owner, replacement, form, ambiguous))
        self._owned(owner, keys)

    def add_id(self, surface: str, replacement: str, owner: str, kind: str = MEMBER_ID) -> None:
        key = alnum_key(surface)
        if len(key) < 3 or (len(key) < LONG_ID_LENGTH and key in self.codes):
            return
        self.ids.add(
            id_steps(surface, separators_inside=len(key) >= LONG_ID_LENGTH),
            Entry(kind, owner, replacement, VALUE),
        )
        core_surface, core_replacement = _digit_core(surface, replacement)
        if core_surface:
            self.ids.add(
                id_steps(core_surface, separators_inside=True), Entry(kind, owner, core_replacement, VALUE)
            )

    def add_claim_number(self, surface: str, replacement: str, owner: str = "") -> None:
        if len(alnum_key(surface)) >= 3:
            self.ids.add(
                id_steps(surface, separators_inside=True), Entry(CLAIM_ID, owner, replacement, VALUE)
            )

    def add_dob(self, value: date, replacement: date, owner: str) -> None:
        entries = self.dobs.setdefault(value, [])
        if (owner, replacement) not in entries:
            entries.append((owner, replacement))

    def add_address(
        self, line1: str, city: str, zip_code: str, owner: str, replacement: tuple[str, str, str]
    ) -> None:
        p_line1, p_city, p_zip = replacement
        if line1.strip() and p_line1:
            for steps in street_variants(line1):
                self.places.add(steps, Entry(ADDRESS, owner, p_line1, STREET))
        if city.strip() and p_city:
            self.places.add(words_steps(*city.split()), Entry(ADDRESS, owner, p_city, CITY))
        five = zip_code.strip().split("-", 1)[0]
        if five and p_zip and alnum_key(five) not in self.codes:
            p_five = p_zip.split("-", 1)[0]
            self.places.add(
                id_steps(zip_code.strip(), separators_inside=True), Entry(ADDRESS, owner, p_zip, ZIP)
            )
            self.places.add(id_steps(five, separators_inside=False), Entry(ADDRESS, owner, p_five, ZIP))

    def add_keep_name(self, name: str, *, person: bool) -> None:
        """A provider (``person``) or an organization name the text keeps as it is."""
        if not name or not name.strip():
            return
        if not person:
            self.keep_names.add(words_steps(*name.split()), _Keep(strong=True))
            return
        variants = provider_name_variants(name)
        for variant in variants:
            last_only = len(NAME_TOKEN_RE.findall(variant)) == 1
            self.keep_names.add(words_steps(*variant.replace(",", " ").split()), _Keep(strong=not last_only))
            if last_only:
                for title in ("dr", "doctor"):
                    self.keep_names.add(words_steps(title, variant), _Keep(strong=True))
        first = next(
            (tokens[0] for v in variants if "," not in v and len(tokens := NAME_TOKEN_RE.findall(v)) >= 2),
            None,
        )
        if (
            first is not None
        ):  # the first name alone: kept unless a title or the patient's surname says otherwise
            self.keep_names.add(name_steps(first), _Keep(strong=False))

    def add_keep_value(self, value: str) -> None:
        """A number kept as it is (a claim number when claim numbers are not replaced)."""
        if len(alnum_key(value)) >= 5:
            self.keep_ids.add(id_steps(value, separators_inside=True), _Keep(strong=True))

    def add_subject(
        self,
        subject: Subject,
        *,
        reverse: bool = False,
        claim_numbers: bool = False,
        skip_parts: frozenset[str] = frozenset(),
        min_part: int = 2,
    ) -> None:
        """Every spelling a subject is known by: originals to pseudonyms, or back with ``reverse``.

        ``skip_parts`` (name keys) and ``min_part`` leave out single name parts (a restore looks
        for another patient's pseudonym parts that this patient does not share).
        """
        owner = subject.key

        def pair(original: str, pseudonym: str) -> tuple[str, str]:
            return (pseudonym, original) if reverse else (original, pseudonym)

        for role in (GIVEN, FAMILY):
            for original, pseudonym in subject.fields[role].items():
                surface, replacement = pair(original, pseudonym)
                surface_tokens = NAME_TOKEN_RE.findall(surface)
                replacement_tokens = NAME_TOKEN_RE.findall(replacement)
                if len(surface_tokens) > 1 or name_steps(surface).count("\x01"):
                    self.add_name(surface, replacement, owner)
                if len(surface_tokens) != len(replacement_tokens):
                    continue
                for index, (token, mapped) in enumerate(zip(surface_tokens, replacement_tokens, strict=True)):
                    key = name_key(token)
                    if len(key) < max(2, min_part) or key in skip_parts:
                        continue
                    if role == FAMILY and index > 0 and key in NAME_SUFFIXES:
                        continue
                    self.add_name(token, mapped, owner)
        for spelling in subject.spellings:
            first, p_first = pair(spelling.first, spelling.pseudo_first)
            last, p_last = pair(spelling.last, spelling.pseudo_last)
            middle, p_middle = pair(spelling.middle, spelling.pseudo_middle)
            for surface, replacement in full_name_forms(first, middle, last, p_first, p_middle, p_last):
                self.add_name(surface, replacement, owner, form=FULL)
        for original, pseudonym in subject.organizations.items():
            surface, replacement = pair(original, pseudonym)
            if len(NAME_TOKEN_RE.findall(surface)) > 1:
                self.names.add(words_steps(*surface.split()), Entry(NAME, owner, replacement, FULL, False))
            else:
                self.add_name(surface, replacement, owner)
        for original, pseudonym in subject.ids.items():
            surface, replacement = pair(original, pseudonym)
            self.add_id(surface, replacement, owner, ACCOUNT if original == subject.key else MEMBER_ID)
        for original, (pseudonym, _offset) in subject.dobs.items():
            value, replacement_date = (pseudonym, original) if reverse else (original, pseudonym)
            self.add_dob(value, replacement_date, owner)
        for record in subject.addresses:
            if reverse:
                self.add_address(
                    record.pseudo_line1,
                    record.pseudo_city,
                    record.pseudo_zip,
                    owner,
                    (record.line1, record.city, record.zip),
                )
            else:
                self.add_address(
                    record.line1,
                    record.city,
                    record.zip,
                    owner,
                    (record.pseudo_line1, record.pseudo_city, record.pseudo_zip),
                )
        if claim_numbers:
            for original, pseudonym in subject.claim_numbers.items():
                surface, replacement = pair(original, pseudonym)
                self.add_claim_number(surface, replacement, owner)

    # -- finding ------------------------------------------------------------------------------

    def _name_ok(self, text: str, start: int, end: int, entry: Entry, owner: str | None) -> tuple[bool, bool]:
        """(may this name match be replaced, does a name context back it)."""
        surface = text[start:end]
        before = _line_before(text, start)
        upper = _upper_line(text, start, end)
        title = (_STRICT_TITLE_RE if upper else _TITLE_RE).search(before) is not None
        neighbours = self.owner_tokens.get(entry.owner, set())
        adjacent = any(key in neighbours for key in _neighbour_keys(text, start, end))
        context = entry.form == FULL or title or adjacent
        if not entry.ambiguous:
            return True, context
        if _attached(text, start, end) or not (_capitalized(surface) or upper):
            return False, False
        if entry.form == FULL:
            return True, True
        return (title or adjacent), (title or adjacent)

    def _place_ok(self, text: str, start: int, end: int, entry: Entry) -> bool:
        if entry.form == STREET:
            return True
        if entry.form == ZIP:
            before = _line_before(text, start, 16)
            return _STATE_BEFORE_RE.search(before) is not None or _ZIP_LABEL_RE.search(before) is not None
        return (
            _AFTER_CITY_RE.match(_line_after(text, end)) is not None
            or _CITY_LABEL_RE.search(_line_before(text, start, 16)) is not None
        )

    def _candidates(self, text: str, owner: str | None) -> list[_Candidate]:
        low, starts = fold_map(text)
        candidates: list[_Candidate] = []

        def span(a: int, b: int) -> tuple[int, int]:
            return original_span(text, starts, a, b)

        for matcher in (self.keep_names, self.keep_ids):
            for a, b, payloads in matcher.find(low):
                start, end = span(a, b)
                strong = any(p.strong for p in payloads)
                candidates.append(_Candidate(start, end, 0 if strong else 2, keep=True))
        for a, b, payloads in self.names.find(low):
            start, end = span(a, b)
            entry = _pick(payloads, owner)
            ok, context = self._name_ok(text, start, end, entry, owner)
            if ok:
                rank = 1 if context else 3
                candidates.append(_Candidate(start, end, rank, entry, _render_name(text[start:end], entry)))
        for a, b, payloads in self.ids.find(low):
            start, end = span(a, b)
            entry = _pick(payloads, owner)
            candidates.append(_Candidate(start, end, 1, entry, _reshape(text[start:end], entry.replacement)))
        for a, b, payloads in self.places.find(low):
            start, end = span(a, b)
            entry = _pick(payloads, owner)
            if self._place_ok(text, start, end, entry):
                candidates.append(
                    _Candidate(start, end, 1, entry, match_case(text[start:end], entry.replacement))
                )
        for match, parsed in date_matches(text):
            value = next((v for v in date_values(match, parsed) if v in self.dobs), None)
            owners = self.dobs.get(value) if value is not None else None
            if value is None or not owners:
                continue
            if value in self.data_dates and not in_dob_context(text, match.start()):
                continue
            chosen = next((o for o in owners if o[0] == owner), owners[0])
            entry = Entry(DOB, chosen[0], chosen[1].isoformat(), VALUE)
            candidates.append(
                _Candidate(match.start(), match.end(), 1, entry, render_date_like(match, chosen[1]))
            )
        if self.keep_numbers:
            for match in PHONE_LIKE_RE.finditer(text):
                if phone_digits(match.group(0)) in self.keep_numbers:
                    candidates.append(_Candidate(match.start(), match.end(), 0, keep=True))
        return candidates

    def find(self, text: str, *, owner: str | None = None) -> list[Hit]:
        """Every identifier of the index in ``text`` (provider and kept spans left out)."""
        if not text:
            return []
        return [
            Hit(c.start, c.end, c.entry.kind, c.entry.owner)
            for c in _select(self._candidates(text, owner))
            if not c.keep and c.entry is not None
        ]

    # -- the whole rewrite --------------------------------------------------------------------

    def process(
        self,
        text: str,
        *,
        owner: str | None,
        shift_days: int = 0,
        allow_residual: bool = False,
        scan: bool = True,
    ) -> TextResult:
        """Replace identifiers, keep provider names and known numbers, shift dates, scan, mask when
        allowed, render. ``scan=False`` (a restore, where real identifiers are the point) skips the
        residual scan."""
        if not text:
            return TextResult(text=text)
        existing = {m.group(1) for m in TOKEN_RE.finditer(text)}
        pseudonym_label = _free_label("PSEUDONYM", existing)
        keep_label = _free_label("KEEP", existing)
        tokens: dict[str, str] = {}
        counter = itertools.count(1)
        replaced = others = 0
        parts: list[str] = []
        cursor = 0
        for candidate in _select(self._candidates(text, owner)):
            parts.append(text[cursor : candidate.start])
            label = keep_label if candidate.keep else pseudonym_label
            name = f"[[{label}_{next(counter)}]]"
            tokens[name] = text[candidate.start : candidate.end] if candidate.keep else candidate.replacement
            parts.append(name)
            cursor = candidate.end
            if not candidate.keep and candidate.entry is not None:
                replaced += 1
                entry = candidate.entry
                others += int(
                    bool(owner) and bool(entry.owner) and entry.owner != owner and entry.kind != ADDRESS
                )
        parts.append(text[cursor:])
        tokenized, shifted = shift_text_dates("".join(parts), shift_days)
        residual = [f.kind for f in residual_findings(tokenized, self.keep_numbers)] if scan else []
        masked = 0
        if residual and allow_residual:
            tokenized, masked = mask_findings(tokenized, residual_findings(tokenized, self.keep_numbers))
            if residual_findings(tokenized, self.keep_numbers) or _DIGITS_AFTER_MASK_RE.search(tokenized):
                raise AnonymizeError("masking residual identifiers left some behind")
        rendered = TOKEN_RE.sub(lambda m: tokens.get(m.group(0), m.group(0)), tokenized)
        return TextResult(
            text=rendered,
            replaced=replaced,
            other_patients=others,
            dates_shifted=shifted,
            residual=residual,
            masked=masked,
        )


def full_name_forms(
    first: str, middle: str, last: str, p_first: str, p_middle: str, p_last: str
) -> list[tuple[str, str]]:
    """(spelling, replacement) for the ways a full name is written in a note or letter."""
    first, middle, last = (" ".join(v.split()) for v in (first, middle, last))
    p_first, p_middle, p_last = (" ".join(v.split()) for v in (p_first, p_middle, p_last))
    if not (first and last and p_first and p_last):
        return []
    fi, li = name_key(first)[:1], name_key(last)[:1]
    pfi, pli = p_first[:1].upper(), p_last[:1].upper()
    forms = [
        (f"{first} {last}", f"{p_first} {p_last}"),
        (f"{last} {first}", f"{p_last}, {p_first}"),
    ]
    if middle and p_middle:
        forms += [
            (f"{first} {middle} {last}", f"{p_first} {p_middle} {p_last}"),
            (f"{last} {first} {middle}", f"{p_last}, {p_first} {p_middle}"),
        ]
    if fi and pfi:
        forms += [(f"{fi} {last}", f"{pfi}. {p_last}"), (f"{last} {fi}", f"{p_last}, {pfi}.")]
    if li and pli:
        forms.append((f"{first} {li}", f"{p_first} {pli}."))
    return forms


def _pick(payloads: list[Entry], owner: str | None) -> Entry:
    """The row's own patient first; else the first subject that spells it this way."""
    for entry in payloads:
        if entry.owner == owner:
            return entry
    return payloads[0]


def _select(candidates: list[_Candidate]) -> list[_Candidate]:
    """Non-overlapping candidates: earlier first, then longer, then by rank."""
    chosen: list[_Candidate] = []
    end = -1
    for candidate in sorted(candidates, key=lambda c: (c.start, -(c.end - c.start), c.rank)):
        if candidate.start >= end:
            chosen.append(candidate)
            end = candidate.end
    return chosen


def _render_name(matched: str, entry: Entry) -> str:
    """The replacement of a name, with the comma and the initial's period the text used."""
    replacement = entry.replacement
    if entry.form == FULL:
        if "," not in matched:
            replacement = replacement.replace(", ", " ")
        if "." not in matched:
            replacement = re.sub(r"\b([A-Z])\.", r"\1", replacement)
    return match_case(matched, replacement)


def _reshape(matched: str, replacement: str) -> str:
    """An id written with the separators the text used (``NW-458-049-016`` -> ``KR-913-620-875``)."""
    letters = [ch for ch in replacement if ch.isalnum()]
    if len(letters) == sum(ch.isalnum() for ch in matched):
        out = iter(letters)
        rebuilt = "".join(next(out) if ch.isalnum() else ch for ch in matched)
        return match_case(matched, rebuilt)
    return match_case(matched, replacement)


def _digit_core(surface: str, replacement: str) -> tuple[str, str]:
    """The longest digit run of an alphanumeric id (six digits or more) and the same run of its
    replacement (``ACC100019`` -> ``100019``)."""
    if not any(ch.isalpha() for ch in surface):
        return "", ""
    runs = list(re.finditer(r"\d+", surface))
    if not runs:
        return "", ""
    run = max(runs, key=lambda m: len(m.group(0)))
    if len(run.group(0)) < MIN_DIGIT_CORE or len(replacement) != len(surface):
        return "", ""
    return run.group(0), replacement[run.start() : run.end()]


def _free_label(label: str, existing: set[str]) -> str:
    """A token label the text does not already use (a letter could quote one)."""
    while label in existing:
        label += "_X"
    return label


# --------------------------------------------------------------------------------------
# Dates written in free text (only with --shift-dates, a test-only option)
# --------------------------------------------------------------------------------------


def _overlaps(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < span_end and end > span_start for span_start, span_end in spans)


def _token_spans(text: str) -> list[tuple[int, int]]:
    return [m.span() for m in TOKEN_RE.finditer(text)]


def shift_text_dates(text: str, days: int) -> tuple[str, int]:
    """Move every date written in ``text`` by ``days`` (tokens left alone); returns (text, count)."""
    if not days or not text:
        return text, 0
    tokens = _token_spans(text)
    parts: list[str] = []
    cursor = count = 0
    for match, value in date_matches(text, compact=False):
        if _overlaps(*match.span(), tokens):
            continue
        try:
            moved = value + timedelta(days=days)
        except OverflowError:
            continue
        parts.append(text[cursor : match.start()])
        parts.append(render_date_like(match, moved))
        cursor = match.end()
        count += 1
    parts.append(text[cursor:])
    return "".join(parts), count


# --------------------------------------------------------------------------------------
# The residual scan: identifiers the batch does not know
# --------------------------------------------------------------------------------------

# Anything a phone pattern could take for a phone number.
PHONE_LIKE_RE = re.compile(
    r"(?<![\dA-Za-z])(?:\+?1[-.\s]?)?(?:\(\d{3}\)\s?|\d{3}[-.\s]?)\d{3}[-.\s]?\d{4}(?![\dA-Za-z])"
)
_SSN_RE = re.compile(r"(?<![\d-])(?!000|666|9\d\d)\d{3}([- ])(?!00)\d{2}\1(?!0000)\d{4}(?![\d-])")
_SSN_LABEL_RE = re.compile(
    r"\b(?:ssn|s\.s\.n\.?|social\s+security(?:\s+(?:number|no\.?|#))?|soc\.?\s*sec\.?(?:\s*(?:number|no\.?|#))?"
    r"|ss\s*#)\s*[:#=]?\s*(?P<value>\d{3}[\s.-]?\d{2}[\s.-]?\d{4})(?!\d)",
    re.IGNORECASE,
)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_MBI_LETTER = "AC-HJKMNP-RT-Y"
_MBI_RE = re.compile(
    rf"(?<![A-Za-z0-9])[1-9][{_MBI_LETTER}][{_MBI_LETTER}0-9]\d-?[{_MBI_LETTER}][{_MBI_LETTER}0-9]\d-?"
    rf"[{_MBI_LETTER}]{{2}}\d{{2}}(?![A-Za-z0-9])"
)
# Labels that name a patient id by themselves ("MRN 445566", "member ID XEH123"), and words that
# do only with a mark after them ("Acct #: 100234", "Chart: 445566", "ID#: W123456789").
_ID_LABEL_RE = re.compile(
    r"\b(?:(?P<label>mrn|medical\s+record(?:\s+(?:number|no\.?|#))?|mbi|hicn?"
    r"|(?:member|subscriber|policy|insurance|patient|pt|medicaid|medicare|account|acct|chart)\s+(?:id|number|no\b\.?))"
    r"\s*(?:#\s*)?[:=]?|(?P<weak>acct|account|chart|policy|member|subscriber|medicaid|medicare|id)"
    r"\s*(?:#\s*:?|:|=))\s*(?:#\s*)?",
    re.IGNORECASE,
)
_ID_LABEL_KINDS = {"mrn": "MRN", "medical": "MRN", "chart": "MRN", "acct": ACCOUNT, "account": ACCOUNT}
_DOB_LABEL_RE = re.compile(
    r"\b(?:dob|d\.o\.b\.?|date\s+of\s+birth|birth\s*date|birthdate|born(?:\s+on)?)\s*[:#=]?\s*", re.IGNORECASE
)
_ID_WORD_RE = re.compile(r"[A-Za-z0-9]+")
_DIGITS_AFTER_MASK_RE = re.compile(r"\]\][ -]?\d")


@dataclass(frozen=True)
class Finding:
    start: int
    end: int
    kind: str


def phone_digits(value: str) -> str:
    """Digits of a phone-like value, without a leading country code 1."""
    digits = "".join(ch for ch in value if ch.isdigit())
    return digits[1:] if len(digits) == 11 and digits.startswith("1") else digits


def _labelled_value(text: str, start: int) -> tuple[int, int] | None:
    """The id written after a label: letter-and-digit groups joined by single spaces or dashes,
    holding a digit (``123 456 789``, ``XEH 123456789``, ``1EG4-TE5-MK73``), up to the first word."""
    cursor, end, first = start, start, True
    has_digit = False
    while cursor < len(text):
        word = _ID_WORD_RE.match(text, cursor)
        if word is None:
            break
        value = word.group(0)
        digit = any(ch.isdigit() for ch in value)
        if not digit and not (first and value.isupper() and len(value) <= 4):
            break
        has_digit = has_digit or digit
        end = word.end()
        first = False
        if end < len(text) and text[end] in " -" and end + 1 < len(text) and text[end + 1].isalnum():
            cursor = end + 1
        else:
            break
    return (start, end) if has_digit else None


def residual_findings(text: str, keep_numbers: frozenset[str] = frozenset()) -> list[Finding]:
    """Identifier-shaped values left in ``text`` outside tokens, as (start, end, kind)."""
    tokens = _token_spans(text)
    found: list[Finding] = []

    def add(start: int, end: int, kind: str) -> None:
        if end > start and not _overlaps(start, end, tokens):
            found.append(Finding(start, end, kind))

    for match in _SSN_LABEL_RE.finditer(text):
        add(*match.span("value"), "SSN")
    for match in _SSN_RE.finditer(text):
        add(*match.span(), "SSN")
    for match in _EMAIL_RE.finditer(text):
        add(*match.span(), "EMAIL")
    for match in PHONE_LIKE_RE.finditer(text):
        if phone_digits(match.group(0)) not in keep_numbers:
            add(*match.span(), "PHONE")
    for match in _MBI_RE.finditer(text):
        add(*match.span(), MEMBER_ID)
    for match in _ID_LABEL_RE.finditer(text):
        value = _labelled_value(text, match.end())
        if value is not None and phone_digits(text[value[0] : value[1]]) not in keep_numbers:
            label = (match.group("label") or match.group("weak")).split()[0].lower()
            add(*value, _ID_LABEL_KINDS.get(label, MEMBER_ID))
    for match in _DOB_LABEL_RE.finditer(text):
        dated = next(iter(date_matches(text[match.end() : match.end() + 40])), None)
        if dated is not None and dated[0].start() == 0:
            add(match.end(), match.end() + dated[0].end(), DOB)
    merged: list[Finding] = []
    for finding in sorted(found, key=lambda f: (f.start, -(f.end - f.start))):
        if merged and finding.start < merged[-1].end:
            continue
        merged.append(finding)
    return merged


def mask_findings(text: str, findings: list[Finding]) -> tuple[str, int]:
    """Every finding replaced, as a whole value, by an unrestorable ``[[MASKED_<KIND>_n]]`` marker."""
    numbering: dict[str, int] = {}
    parts: list[str] = []
    cursor = 0
    for finding in findings:
        label = MASK_PREFIX + finding.kind
        numbering[label] = numbering.get(label, 0) + 1
        parts.append(text[cursor : finding.start])
        parts.append(f"[[{label}_{numbering[label]}]]")
        cursor = finding.end
    parts.append(text[cursor:])
    return "".join(parts), len(findings)


__all__ = [
    "ACCOUNT",
    "ADDRESS",
    "CLAIM_ID",
    "DOB",
    "LONG_ID_LENGTH",
    "MEMBER_ID",
    "NAME",
    "PHONE_LIKE_RE",
    "TOKEN_RE",
    "Entry",
    "Finding",
    "Hit",
    "TextIndex",
    "TextResult",
    "full_name_forms",
    "lowercase_corpus",
    "mask_findings",
    "phone_digits",
    "residual_findings",
    "shift_text_dates",
    "street_variants",
    "street_without_unit",
]
