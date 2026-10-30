"""Pseudonyms for `denial-agent anonymize` (docs/SPEC.md section 16).

A :class:`Pseudonymizer` holds every mapping of one run and is the only place a pseudonym is drawn:

- **Names, per patient.** Each patient of the claim export (keyed by account number) is a
  :class:`Subject` with a first and last name from ``synthetic.names`` that no other patient of the
  batch shares. Every other spelling met for that patient (the payer's in the 835, the subscriber's,
  a middle name or an initial) is mapped token by token inside the subject, so a surname the patient
  shares with the subscriber stays shared and each field maps back to exactly one original.
- **Patient identifiers** (account numbers, member ids, the NM109 and REF ids of an 835) keep their
  shape: digits become digits, letters become letters of the same case, punctuation stays.
- **Addresses, cities and ZIP codes**; **dates of birth** keep the year while day and month move by a
  per-patient offset; **claim numbers** and **payer claim numbers** when asked; one global date shift.

Every map is injective and never emits a value that is an original of the same family anywhere in
the batch, so a pseudonym can never be mistaken for (or leak) another patient's real value. Draws
come from one ``random.Random(seed)`` in the order values are first met: a pseudonym is a function
of where a value sits in the batch, not of the value, so knowing the seed does not let anyone test a
guessed name against the output.

The key (:meth:`Pseudonymizer.key_document`, read back by :class:`PseudonymKey`) holds every map in
full, the originals of every batch it has seen and the claim index, so :meth:`Pseudonymizer.from_key`
rebuilds the same state for a later batch: a returning value keeps its pseudonym, and every new
pseudonym is still drawn outside the originals of all those batches.
"""

from __future__ import annotations

import json
import random
import re
import unicodedata
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field, fields
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from synthetic import names

# A name token: a run of letters in any script, with inner apostrophes ("O'Brien").
NAME_TOKEN_RE = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*")
# Generational suffixes are kept as they are (after the first token of a last name only).
NAME_SUFFIXES: frozenset[str] = frozenset({"jr", "sr", "ii", "iii", "iv"})
CITY_SUFFIXES: tuple[str, ...] = (
    "Falls",
    "Springs",
    "Hollow",
    "Crossing",
    "Harbor",
    "Ridge",
    "Flats",
    "Creek",
    "Grove",
    "Bluff",
    "Mills",
    "Point",
)
STREET_FALLBACK_SUFFIXES: tuple[str, ...] = ("Road", "Lane", "Way", "Court")
HOUSE_NUMBERS = (100, 9899)
DOB_OFFSET_DAYS = (1, 364)  # inside one year: the year is kept, the date always moves
MAX_DRAWS = 200
MAX_SHIFT_DAYS = 3650
LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

GIVEN = "given"
FAMILY = "family"
PATIENT = "patient"
REMITTANCE = "remittance"


class AnonymizeError(Exception):
    """A refusal or failure of `denial-agent anonymize`.

    Messages carry file names, row numbers, field names and counts only, never a value.
    """


def name_key(token: str) -> str:
    """Comparison form of a name token: accents and apostrophes dropped, case folded."""
    decomposed = unicodedata.normalize("NFKD", token)
    return "".join(ch for ch in decomposed if ch.isalpha() and not unicodedata.combining(ch)).casefold()


def value_key(value: str) -> str:
    """Comparison form of an address part or identifier: single-spaced, case folded."""
    return " ".join(str(value).split()).casefold()


def match_case(template: str, replacement: str) -> str:
    """Give ``replacement`` the case style of ``template`` (all upper, all lower, else as it is)."""
    letters = [ch for ch in template if ch.isalpha()]
    if not letters:
        return replacement
    if all(ch.isupper() for ch in letters):
        return replacement.upper()
    if all(ch.islower() for ch in letters):
        return replacement.lower()
    return replacement


def shift_within_year(value: date, offset: int) -> date:
    """Move ``value`` forward by ``offset`` days, wrapping inside its own calendar year."""
    start = date(value.year, 1, 1)
    length = (date(value.year + 1, 1, 1) - start).days
    return start + timedelta(days=((value - start).days + offset) % length)


def _blend_names(words: Sequence[str]) -> list[str]:
    """Invented names made of the front of one list name and the back of another (a fallback for
    batches so large that the lists themselves run out)."""
    seen = {w.casefold() for w in words}
    blends: list[str] = []
    for first in words:
        for second in words:
            if first is second:
                continue
            blend = (first[: (len(first) + 1) // 2] + second[len(second) // 2 :]).capitalize()
            if blend.isalpha() and blend.casefold() not in seen:
                seen.add(blend.casefold())
                blends.append(blend)
    return blends


@dataclass
class Originals:
    """Every original value of the batch by family; pseudonyms are always drawn outside these sets."""

    name_tokens: set[str] = field(default_factory=set)  # name_key()
    patient_ids: set[str] = field(default_factory=set)  # value_key()
    claim_numbers: set[str] = field(default_factory=set)
    payer_claim_numbers: set[str] = field(default_factory=set)
    address_lines: set[str] = field(default_factory=set)
    cities: set[str] = field(default_factory=set)
    zips: set[str] = field(default_factory=set)
    dobs: set[date] = field(default_factory=set)

    def add_name(self, value: str | None) -> None:
        for token in NAME_TOKEN_RE.findall(value or ""):
            key = name_key(token)
            if key:
                self.name_tokens.add(key)

    @staticmethod
    def _add(target: set[str], value: str | None) -> None:
        if value and value.strip():
            target.add(value_key(value))

    def add_patient_id(self, value: str | None) -> None:
        self._add(self.patient_ids, value)

    def add_claim_number(self, value: str | None) -> None:
        self._add(self.claim_numbers, value)

    def add_payer_claim_number(self, value: str | None) -> None:
        self._add(self.payer_claim_numbers, value)

    def add_address(self, line1: str | None, city: str | None, zip_code: str | None) -> None:
        if line1 and line1.strip():
            self.address_lines.add(value_key(line1))
            self.address_lines.add(value_key(line1.split(",", 1)[0]))  # without the apartment
        self._add(self.cities, city)
        if zip_code and zip_code.strip():
            self.zips.add(value_key(zip_code))
            self.zips.add(value_key(zip_code.split("-", 1)[0]))

    def merge(self, other: Originals) -> Originals:
        for family in fields(self):
            getattr(self, family.name).update(getattr(other, family.name))
        return self

    def to_document(self) -> dict[str, list[str]]:
        document = {f.name: sorted(getattr(self, f.name)) for f in fields(self) if f.name != "dobs"}
        document["dobs"] = sorted(d.isoformat() for d in self.dobs)
        return document

    @classmethod
    def from_document(cls, document: dict[str, list[str]]) -> Originals:
        originals = cls(**{f.name: set(document.get(f.name) or []) for f in fields(cls) if f.name != "dobs"})
        originals.dobs = {date.fromisoformat(value) for value in document.get("dobs") or []}
        return originals


class ValueMap:
    """original -> pseudonym for one family of values: injective, never emitting an original.

    Originals are compared in ``value_key`` form, so ``ACC100001`` and ``acc100001`` share one
    pseudonym; :meth:`get` gives it back in the case style of the value asked for.
    """

    def __init__(self, family: str, taken: Iterable[str]) -> None:
        self.family = family
        self.forward: dict[str, str] = {}
        self.first_seen: dict[str, str] = {}
        self._taken = set(taken)
        self._emitted: set[str] = set()

    def lookup(self, value: str) -> str | None:
        pseudonym = self.forward.get(value_key(value))
        return None if pseudonym is None else match_case(value, pseudonym)

    def available(self, candidate: str) -> bool:
        key = value_key(candidate)
        return key not in self._taken and key not in self._emitted

    def get(self, value: str, draw: Callable[[], str], *, preferred: str | None = None) -> str:
        key = value_key(value)
        pseudonym = self.forward.get(key)
        if pseudonym is None:
            if preferred is not None and self.available(preferred):
                pseudonym = preferred
            else:
                for _ in range(MAX_DRAWS):
                    candidate = draw()
                    if self.available(candidate):
                        pseudonym = candidate
                        break
                else:
                    raise AnonymizeError(f"could not draw a unique {self.family} pseudonym")
            self.forward[key] = pseudonym
            self.first_seen[key] = value
            self._emitted.add(value_key(pseudonym))
        return match_case(value, pseudonym)

    def items(self) -> list[tuple[str, str]]:
        """(original as first met, pseudonym) in the order they were drawn."""
        return [(self.first_seen[key], pseudonym) for key, pseudonym in self.forward.items()]

    def restore(self, mapping: dict[str, str]) -> None:
        """Take back the pairs a key recorded (original as first met -> pseudonym)."""
        for original, pseudonym in mapping.items():
            key = value_key(original)
            self.forward[key] = pseudonym
            self.first_seen[key] = original
            self._emitted.add(value_key(pseudonym))


class _Pool:
    """Candidate name words outside the batch's originals: the list first, blends when it runs short."""

    def __init__(self, words: Sequence[str], excluded: set[str], rng: random.Random) -> None:
        self._rng = rng
        self._words = list(words)
        self._excluded = excluded
        self.base = [w for w in self._words if name_key(w) not in excluded]
        self._extended: list[str] | None = None

    def extended(self) -> list[str]:
        if self._extended is None:
            blends = [w for w in _blend_names(self._words) if name_key(w) not in self._excluded]
            self._extended = self.base + blends
        return self._extended

    def draw(self, avoid: set[str]) -> str:
        """A word whose name_key is not in ``avoid``; random from the list, then from the blends."""
        for candidates, tries in ((self.base, 32), (self.extended(), 64)):
            if not candidates:
                continue
            for _ in range(tries):
                word = self._rng.choice(candidates)
                if name_key(word) not in avoid:
                    return word
        for word in self.extended():
            if name_key(word) not in avoid:
                return word
        raise AnonymizeError("ran out of synthetic names")


@dataclass
class Spelling:
    """One spelling of a person met in the batch and the spelling written in its place."""

    first: str
    last: str
    middle: str
    pseudo_first: str
    pseudo_last: str
    pseudo_middle: str


@dataclass
class AddressRecord:
    line1: str
    city: str
    state: str
    zip: str
    pseudo_line1: str
    pseudo_city: str
    pseudo_zip: str


class Subject:
    """Everything mapped for one person-centred record: a patient of the claim export (keyed by
    account number) or a remittance subject the export does not know (keyed by the payer's ids).

    The free-text replacer reads ``spellings``, ``surfaces``, ``dobs``, ``ids``, ``addresses`` and
    ``claim_numbers`` to know every identifier this patient can appear under in a note or letter.
    """

    def __init__(self, key: str, kind: str, owner: Pseudonymizer) -> None:
        self.key = key
        self.kind = kind
        self._owner = owner
        self.tokens: dict[str, dict[str, str]] = {GIVEN: {}, FAMILY: {}}
        self.initials: dict[str, str] = {}
        self.fields: dict[str, dict[str, str]] = {GIVEN: {}, FAMILY: {}}
        self.surfaces: dict[str, str] = {}
        self.spellings: list[Spelling] = []
        self.dobs: dict[date, tuple[date, int]] = {}
        self.ids: dict[str, str] = {}
        self.addresses: list[AddressRecord] = []
        self.claim_numbers: dict[str, str] = {}

    # -- names ------------------------------------------------------------------------------

    def _used_pseudonyms(self) -> set[str]:
        return {name_key(v) for role in (GIVEN, FAMILY) for v in self.tokens[role].values()}

    def _token(self, role: str, surface: str) -> str:
        key = name_key(surface)
        pseudonym = self.tokens[role].get(key)
        if pseudonym is None:
            pseudonym = self._owner.draw_name(role, self._used_pseudonyms())
            self.tokens[role][key] = pseudonym
        return pseudonym

    def _initial(self, letter: str) -> str:
        key = letter.casefold()
        pseudonym = self.initials.get(key)
        if pseudonym is None:
            used = set(self.initials.values()) | {key.upper()}
            choices = [ch for ch in LETTERS if ch not in used] or [ch for ch in LETTERS if ch != key.upper()]
            pseudonym = self._owner.rng.choice(choices)
            self.initials[key] = pseudonym
        return pseudonym

    def seed(self, first: str | None, last: str | None) -> None:
        """Give the first token of the clinic's first and last name the patient's own name pair."""
        given_tokens = [t for t in NAME_TOKEN_RE.findall(first or "") if len(name_key(t)) > 1]
        family_tokens = [t for t in NAME_TOKEN_RE.findall(last or "") if len(name_key(t)) > 1]
        if not given_tokens and not family_tokens:
            return
        given, family = self._owner.draw_name_pair()
        if given_tokens and name_key(given_tokens[0]) not in self.tokens[GIVEN]:
            self.tokens[GIVEN][name_key(given_tokens[0])] = given
        if family_tokens and name_key(family_tokens[0]) not in self.tokens[FAMILY]:
            self.tokens[FAMILY][name_key(family_tokens[0])] = family

    def name_field(self, role: str, value: str | None) -> str:
        """Map one name field (first, middle or last name) token by token; separators stay."""
        if not value:
            return value or ""
        known = self.fields[role].get(value)
        if known is not None:
            return known
        parts: list[str] = []
        cursor = 0
        for index, match in enumerate(NAME_TOKEN_RE.finditer(value)):
            surface = match.group(0)
            key = name_key(surface)
            parts.append(value[cursor : match.start()])
            if not key or (role == FAMILY and index > 0 and key in NAME_SUFFIXES):
                replacement = surface
            elif len(key) == 1:
                replacement = match_case(surface, self._initial(key))
            else:
                replacement = match_case(surface, self._token(role, surface))
                self.surfaces.setdefault(surface, replacement)
            parts.append(replacement)
            cursor = match.end()
        parts.append(value[cursor:])
        result = "".join(parts)
        self.fields[role][value] = result
        return result

    def person(self, first: str | None, last: str | None, middle: str | None = None) -> tuple[str, str, str]:
        """Map a person's first, last and middle name and remember the spelling for free text."""
        pseudo = (
            self.name_field(GIVEN, first),
            self.name_field(FAMILY, last),
            self.name_field(GIVEN, middle),
        )
        spelling = Spelling(first or "", last or "", middle or "", *pseudo)
        if (spelling.first or spelling.last) and spelling not in self.spellings:
            self.spellings.append(spelling)
        return pseudo

    # -- the key ------------------------------------------------------------------------------

    def to_document(self) -> dict[str, Any]:
        """Everything this subject maps, for the key (field maps first: they are what a person reads)."""
        return {
            "kind": self.kind,
            "given": dict(self.fields[GIVEN]),
            "family": dict(self.fields[FAMILY]),
            "dob": {
                original.isoformat(): {"pseudonym": pseudo.isoformat(), "offset_days": offset}
                for original, (pseudo, offset) in self.dobs.items()
            },
            "tokens": {GIVEN: dict(self.tokens[GIVEN]), FAMILY: dict(self.tokens[FAMILY])},
            "initials": dict(self.initials),
            "surfaces": dict(self.surfaces),
            "spellings": [asdict(spelling) for spelling in self.spellings],
            "ids": dict(self.ids),
            "addresses": [asdict(record) for record in self.addresses],
            "claim_numbers": dict(self.claim_numbers),
        }

    @classmethod
    def from_document(cls, key: str, document: dict[str, Any], owner: Pseudonymizer) -> Subject:
        subject = cls(key, str(document["kind"]), owner)
        subject.fields = {GIVEN: dict(document[GIVEN]), FAMILY: dict(document[FAMILY])}
        subject.tokens = {GIVEN: dict(document["tokens"][GIVEN]), FAMILY: dict(document["tokens"][FAMILY])}
        subject.initials = dict(document["initials"])
        subject.surfaces = dict(document["surfaces"])
        subject.spellings = [Spelling(**spelling) for spelling in document["spellings"]]
        subject.dobs = {
            date.fromisoformat(original): (date.fromisoformat(entry["pseudonym"]), int(entry["offset_days"]))
            for original, entry in document["dob"].items()
        }
        subject.ids = dict(document["ids"])
        subject.addresses = [AddressRecord(**record) for record in document["addresses"]]
        subject.claim_numbers = dict(document["claim_numbers"])
        return subject


class Pseudonymizer:
    """Every mapping of one anonymize run (see the module docstring)."""

    def __init__(
        self,
        originals: Originals,
        *,
        seed: int,
        shift_days: int = 0,
        replace_claim_numbers: bool = False,
    ) -> None:
        if abs(shift_days) > MAX_SHIFT_DAYS:
            raise AnonymizeError(f"--shift-dates must be between -{MAX_SHIFT_DAYS} and {MAX_SHIFT_DAYS} days")
        self.rng = random.Random(seed)
        self.originals = originals
        self.shift_days = shift_days
        self.replace_claim_numbers = replace_claim_numbers
        excluded = originals.name_tokens
        self._given = _Pool(names.FIRST_NAMES, excluded, self.rng)
        self._family = _Pool(names.LAST_NAMES, excluded, self.rng)
        self._pairs: dict[tuple[str, str], None] = {}  # ordered: the key is written the same way each run
        self.duplicate_name_pairs = 0
        self.subjects: dict[str, Subject] = {}
        self.patient_ids = ValueMap("patient identifier", originals.patient_ids)
        self.claim_numbers = ValueMap("claim number", originals.claim_numbers)
        self.payer_claim_numbers = ValueMap("payer claim number", originals.payer_claim_numbers)
        self.address_lines = ValueMap("address", originals.address_lines)
        self.cities = ValueMap("city", originals.cities)
        self.zips = ValueMap("ZIP code", originals.zips)
        self._streets = self._street_candidates()
        self._city_candidates = self._cities()

    # -- names --------------------------------------------------------------------------------

    def draw_name(self, role: str, avoid: set[str]) -> str:
        return (self._given if role == GIVEN else self._family).draw(avoid)

    def draw_name_pair(self) -> tuple[str, str]:
        """A first and last name pair no other patient of the batch has (best effort)."""
        for given_words, family_words, tries in (
            (self._given.base, self._family.base, 64),
            (self._given.extended(), self._family.extended(), 256),
        ):
            if not given_words or not family_words:
                continue
            for _ in range(tries):
                pair = (self.rng.choice(given_words), self.rng.choice(family_words))
                if pair not in self._pairs:
                    self._pairs[pair] = None
                    return pair
        self.duplicate_name_pairs += 1
        return (self._given.draw(set()), self._family.draw(set()))

    def subject(self, key: str, *, kind: str = PATIENT) -> Subject:
        subject = self.subjects.get(key)
        if subject is None:
            subject = Subject(key, kind, self)
            self.subjects[key] = subject
        return subject

    def value_maps(self) -> dict[str, ValueMap]:
        """The value maps under the family names the key uses (``VALUE_FAMILIES``)."""
        return {
            "patient_id": self.patient_ids,
            "address_line": self.address_lines,
            "city": self.cities,
            "zip": self.zips,
            "claim_number": self.claim_numbers,
            "payer_claim_number": self.payer_claim_numbers,
        }

    @classmethod
    def from_key(cls, key: PseudonymKey, originals: Originals, *, seed: int) -> Pseudonymizer:
        """The state a key recorded, ready for a later batch whose own originals are given: every
        value the key maps keeps its pseudonym, and new draws avoid the originals of every batch."""
        document = key.document
        try:
            merged = originals.merge(Originals.from_document(document["originals"]))
            pseudo = cls(
                merged,
                seed=seed,
                shift_days=key.date_shift_days,
                replace_claim_numbers=key.replace_claim_numbers,
            )
            pseudo._pairs = {(str(given), str(family)): None for given, family in document["name_pairs"]}
            for family, value_map in pseudo.value_maps().items():
                value_map.restore(document["values"][family])
            for subject_key, data in document["subjects"].items():
                pseudo.subjects[subject_key] = Subject.from_document(subject_key, data, pseudo)
        except (KeyError, TypeError, ValueError) as exc:
            raise AnonymizeError("--key-in is not a complete anonymize key") from exc
        return pseudo

    # -- identifiers --------------------------------------------------------------------------

    def _shaped(self, value: str) -> str:
        """Same length and character classes: ASCII digits and letters redrawn, the rest kept."""
        out: list[str] = []
        for ch in value:
            if "0" <= ch <= "9":
                out.append(str(self.rng.randrange(10)))
            elif "A" <= ch <= "Z":
                out.append(self.rng.choice(LETTERS))
            elif "a" <= ch <= "z":
                out.append(self.rng.choice(LETTERS).lower())
            else:
                out.append(ch)
        return "".join(out)

    @staticmethod
    def _has_alnum(value: str) -> bool:
        return any(ch.isalnum() for ch in value)

    def patient_id(self, value: str, subject: Subject | None = None) -> str:
        """A member id, account number, medical record number or other patient id, same shape."""
        if not value or not self._has_alnum(value):
            return value
        stripped = value.strip()
        pseudonym = self.patient_ids.get(stripped, lambda: self._shaped(stripped))
        if subject is not None:
            subject.ids.setdefault(stripped, pseudonym)
        return pseudonym

    def claim_number(self, value: str, subject: Subject | None = None) -> str:
        """A claim control number or line item control number (only with --replace-claim-numbers).

        A number that extends a known claim number after a separator (``CLM000123-1``,
        ``CLM000123-R1``) keeps the extension on the known number's pseudonym.
        """
        if not self.replace_claim_numbers or not value or not self._has_alnum(value):
            return value
        stripped = value.strip()
        preferred: str | None = None
        if self.claim_numbers.lookup(stripped) is None:
            for cut in range(len(stripped) - 1, 0, -1):
                if stripped[cut].isalnum():
                    continue
                base = self.claim_numbers.lookup(stripped[:cut])
                if base is not None:
                    preferred = base + stripped[cut:]
                    break
        pseudonym = self.claim_numbers.get(stripped, lambda: self._shaped(stripped), preferred=preferred)
        if subject is not None:
            subject.claim_numbers.setdefault(stripped, pseudonym)
        return pseudonym

    def payer_claim_number(self, value: str, subject: Subject | None = None) -> str:
        """CLP07 / REF*F8 (only with --replace-claim-numbers), same shape, its own map."""
        if not self.replace_claim_numbers or not value or not self._has_alnum(value):
            return value
        stripped = value.strip()
        pseudonym = self.payer_claim_numbers.get(stripped, lambda: self._shaped(stripped))
        if subject is not None:
            subject.claim_numbers.setdefault(stripped, pseudonym)
        return pseudonym

    # -- addresses ----------------------------------------------------------------------------

    def _clean_words(self, text: str) -> bool:
        """True when no word of ``text`` is a name token of the batch."""
        return all(name_key(w) not in self.originals.name_tokens for w in NAME_TOKEN_RE.findall(text))

    def _street_candidates(self) -> list[str]:
        streets = [s for s in names.STREET_NAMES if self._clean_words(s)]
        if streets:
            return streets
        return [f"{w} {suffix}" for w in self._family.extended()[:20] for suffix in STREET_FALLBACK_SUFFIXES]

    def _cities(self) -> list[str]:
        """Town names: the synthetic towns, then street-name words with a town suffix (never a
        surname, so a patient is not called after the town written next to their name)."""

        def usable(candidates: Iterable[str]) -> list[str]:
            return [
                c for c in candidates if value_key(c) not in self.originals.cities and self._clean_words(c)
            ]

        words = sorted({street.split()[0] for street in names.STREET_NAMES})
        cities = usable([city for city, _state, _zip in names.CITIES])
        cities += usable(f"{word} {suffix}" for word in words for suffix in CITY_SUFFIXES)
        if len(cities) < 2 * max(1, len(self.originals.cities)):  # a batch with very many towns
            cities += usable(
                f"{w} {suffix}" for w in self._family.extended()[:400] for suffix in CITY_SUFFIXES
            )
        return cities

    def address(
        self,
        subject: Subject,
        line1: str | None,
        city: str | None,
        state: str | None,
        zip_code: str | None,
    ) -> tuple[str, str, str]:
        """Pseudonyms for an address line, a city and a ZIP code (the state is kept)."""
        rng = self.rng
        pseudo_line1 = (
            self.address_lines.get(
                line1.strip(),
                lambda: f"{rng.randint(*HOUSE_NUMBERS)} {rng.choice(self._streets)}",
            )
            if line1 and line1.strip()
            else (line1 or "")
        )
        pseudo_city = (
            self.cities.get(city.strip(), lambda: rng.choice(self._city_candidates))
            if city and city.strip()
            else (city or "")
        )
        pseudo_zip = (
            self.zips.get(zip_code.strip(), lambda: self._shaped(zip_code.strip()))
            if zip_code and zip_code.strip() and self._has_alnum(zip_code)
            else (zip_code or "")
        )
        record = AddressRecord(
            (line1 or "").strip(),
            (city or "").strip(),
            (state or "").strip(),
            (zip_code or "").strip(),
            pseudo_line1,
            pseudo_city,
            pseudo_zip,
        )
        if (record.line1 or record.city or record.zip) and record not in subject.addresses:
            subject.addresses.append(record)
        return pseudo_line1, pseudo_city, pseudo_zip

    # -- dates --------------------------------------------------------------------------------

    def shift(self, value: date) -> date:
        return value + timedelta(days=self.shift_days) if self.shift_days else value

    def dob(self, subject: Subject, value: date) -> date:
        """Same year, day and month moved by the patient's offset; then the global shift.

        The offset is drawn again when the result would equal any original date of birth of the
        batch, so a pseudonymized date of birth is never another patient's real one.
        """
        known = subject.dobs.get(value)
        if known is not None:
            return known[0]
        # A patient exported with two dates of birth (a data error) keeps one offset for both.
        earlier = next(iter(subject.dobs.values()), None)
        offset = earlier[1] if earlier is not None else self.rng.randint(*DOB_OFFSET_DAYS)
        for _ in range(MAX_DRAWS):
            pseudonym = self.shift(shift_within_year(value, offset))
            if pseudonym not in self.originals.dobs:
                break
            offset = self.rng.randint(*DOB_OFFSET_DAYS)
        else:
            raise AnonymizeError("could not draw a unique date of birth")
        subject.dobs[value] = (pseudonym, offset)
        return pseudonym

    # -- the key ------------------------------------------------------------------------------

    def key_document(self, *, runs: list[dict[str, Any]], index: dict[str, Any]) -> dict[str, Any]:
        """The KEY.json content: the only place where an original value is written.

        It holds everything :meth:`from_key` needs to go on with a later batch: the options that
        shape the maps, every subject in full, the value maps, the claim index, the originals of
        every batch seen so far, the name pairs already given, and one entry per run.
        """
        return {
            "format": KEY_FORMAT,
            "version": KEY_VERSION,
            "warning": KEY_WARNING,
            "options": {
                "replace_claim_numbers": self.replace_claim_numbers,
                "shift_dates_days": self.shift_days,
            },
            "runs": runs,
            "subjects": {key: subject.to_document() for key, subject in self.subjects.items()},
            "values": {family: dict(value_map.items()) for family, value_map in self.value_maps().items()},
            "index": index,
            "originals": self.originals.to_document(),
            "name_pairs": [list(pair) for pair in self._pairs],
        }


KEY_FORMAT = "denial-agent/anonymize-key"
KEY_VERSION = 2  # 2: everything from_key needs; a version 1 key cannot be extended
KEY_WARNING = (
    "This file holds the original patient identifiers of a pseudonymized batch. It stays with the "
    "clinic, on the clinic's own computer; never copy it next to the pseudonymized files."
)
VALUE_FAMILIES: tuple[str, ...] = (
    "patient_id",
    "address_line",
    "city",
    "zip",
    "claim_number",
    "payer_claim_number",
)


class PseudonymKey:
    """A loaded KEY.json: maps every pseudonym back to its original, and seeds a later run."""

    def __init__(self, document: dict[str, Any]) -> None:
        if document.get("format") != KEY_FORMAT or document.get("version") != KEY_VERSION:
            raise AnonymizeError(f"not an anonymize key of version {KEY_VERSION} (format or version differs)")
        self.document = document
        options = document.get("options") or {}
        self.replace_claim_numbers = bool(options.get("replace_claim_numbers"))
        self.date_shift_days = int(options.get("shift_dates_days") or 0)
        self.runs: list[dict[str, Any]] = list(document.get("runs") or [])
        self.index: dict[str, Any] = document.get("index") or {}
        self.subjects: dict[str, dict[str, Any]] = document.get("subjects") or {}
        self._reverse: dict[str, dict[str, str]] = {}
        for family in VALUE_FAMILIES:
            mapping = (document.get("values") or {}).get(family) or {}
            self._reverse[family] = {value_key(pseudo): original for original, pseudo in mapping.items()}

    def original(self, family: str, pseudonym: str) -> str | None:
        """The original of a pseudonymized identifier, address part or claim number."""
        original = self._reverse[family].get(value_key(pseudonym))
        return None if original is None else match_case(pseudonym, original)

    def unshift(self, value: date) -> date:
        return value - timedelta(days=self.date_shift_days)

    def subject_for_account(self, pseudo_account: str) -> dict[str, Any] | None:
        """The key's record of the patient whose pseudonymized account number this is."""
        original = self._reverse["patient_id"].get(value_key(pseudo_account))
        return None if original is None else self.subjects.get(original)

    def original_name(self, subject: dict[str, Any], role: str, pseudonym: str) -> str | None:
        for original, mapped in (subject.get(role) or {}).items():
            if mapped == pseudonym:
                return original
        return None

    def original_dob(self, subject: dict[str, Any], pseudonym: date) -> date | None:
        for original, entry in (subject.get("dob") or {}).items():
            if entry.get("pseudonym") == pseudonym.isoformat():
                return date.fromisoformat(original)
        return None


def load_key(path: Path) -> PseudonymKey:
    """Read a KEY.json; anything else is refused by file name only (never by content)."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AnonymizeError(f"{path.name} is not a readable anonymize key") from exc
    if not isinstance(document, dict):
        raise AnonymizeError(f"{path.name} is not an anonymize key")
    return PseudonymKey(document)


__all__ = [
    "FAMILY",
    "GIVEN",
    "KEY_FORMAT",
    "KEY_VERSION",
    "AnonymizeError",
    "Originals",
    "PseudonymKey",
    "Pseudonymizer",
    "Subject",
    "ValueMap",
    "match_case",
    "name_key",
    "load_key",
    "shift_within_year",
    "value_key",
]
