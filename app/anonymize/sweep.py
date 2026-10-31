"""The leak sweep of `denial-agent anonymize` (docs/SPEC.md section 16).

Nothing is written until every output file name and every output byte has been searched for every
original value the key holds (this batch's and every earlier batch's of the chain), and nothing was
found. The sweep is independent of how the values were replaced: it reads the outputs as the
importer will.

- **Names**: folded (accents, case, curly apostrophes) and in every join of their parts, as whole
  words; an ordinary-word name part only in a name context, exactly as free text treats it.
- **Ids** of five characters or more (patient ids, and claim numbers when they are replaced) with
  separators stripped; in an X12 element an id of eight characters or more anywhere inside it.
- **Dates of birth** as ISO, CCYYMMDD, M/D/YYYY and every other form a note writes; in a date column
  or a date element only the date-of-birth column counts (a service date may fall on a child's
  birthday).
- **Addresses**: street lines with their USPS spellings anywhere; a city or a ZIP code in the
  address columns, an N4 segment or an address context of free text, never a ZIP that is a
  procedure code of the batch.

Allowed by design: the payer and payee loops of an 835 (the payer's and the clinic's own names and
addresses), provider and organization names (rendering provider and payer columns, a note author
that is the clinic's staff, NM1 segments of providers and payers), codes, amounts and service dates.
A hit is reported by file, place, field and kind, never by value, and the run is refused.
"""

from __future__ import annotations

import csv
import io
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.anonymize.matching import alnum_key, date_matches, date_values, fold, name_key
from app.anonymize.pseudonyms import NAME_TOKEN_RE, Subject, value_key
from app.anonymize.remit import PERSON_ENTITIES
from app.anonymize.text import (
    ACCOUNT,
    ADDRESS,
    CLAIM_ID,
    DOB,
    MEMBER_ID,
    NAME,
    TextIndex,
    street_without_unit,
)
from app.ingest.x12 import X12Error, read_delimiters, tokenize

MIN_SWEEP_ID = 5
MIN_EMBEDDED_ID = 8
NAME_COLUMNS = frozenset({"patient_first_name", "patient_last_name"})
ID_COLUMNS = frozenset({"patient_account_number", "member_id"})
CLAIM_COLUMNS = frozenset(
    {"claim_control_number", "original_claim_control_number", "line_item_control_number"}
)
PROVIDER_COLUMNS = frozenset({"rendering_provider_name", "payer_name"})
DATE_COLUMNS = frozenset({"service_date", "original_submission_date", "note_date", "received_date"})
TEXT_COLUMNS = frozenset({"text", "author", "document_type"})
DATE_ELEMENTS: dict[str, frozenset[int]] = {
    "ISA": frozenset({9, 10}),
    "GS": frozenset({4, 5}),
    "BPR": frozenset({16}),
    "DTM": frozenset({2, 3, 6}),
    "PLB": frozenset({2}),
}
_PAYER_PAYEE_SEGMENTS = frozenset({"N1", "N2", "N3", "N4", "REF", "PER", "RDM"})
_AMOUNT_RE = re.compile(r"-?\d*\.\d+")
_LOOP_RESET = frozenset({"LX", "CLP", "SE", "ST", "GE", "IEA", "PLB", "TS3", "TS2"})


@dataclass(frozen=True)
class SweepHit:
    file: str
    place: str  # "row 12", "segment 34", "file name"
    field: str  # a column, an element position
    kind: str

    def as_dict(self) -> dict[str, Any]:
        return {"file": self.file, "place": self.place, "field": self.field, "kind": self.kind}


class Sweep:
    """Every original of a key, ready to be searched for in output files."""

    def __init__(
        self,
        key_document: dict[str, Any],
        *,
        data_dates: frozenset[date],
        codes: frozenset[str],
        corpus: Counter[str],
        provider_names: frozenset[str],
        organization_names: frozenset[str],
    ) -> None:
        originals = key_document.get("originals") or {}
        options = key_document.get("options") or {}
        self.claim_numbers_replaced = bool(options.get("replace_claim_numbers"))
        self.text = TextIndex(data_dates=data_dates, codes=codes, corpus=corpus)
        subjects = [
            Subject.from_document(subject_key, document, None)
            for subject_key, document in (key_document.get("subjects") or {}).items()
        ]
        # A word an earlier batch gave a patient as a pseudonym, that a patient of a later batch
        # really carries: the earlier patient keeps it (the app knows them by it), so on its own it
        # proves nothing; full names with it are still searched.
        pseudonym_words = {
            name_key(token)
            for subject in subjects
            for values in (*subject.tokens.values(), subject.organizations)
            for value in values.values()
            for token in NAME_TOKEN_RE.findall(value)
        }
        originals_words = frozenset(originals.get("name_tokens") or [])
        self.collisions = originals_words & pseudonym_words
        for subject in subjects:
            self.text.add_subject(
                subject, claim_numbers=self.claim_numbers_replaced, skip_parts=self.collisions
            )
        self.name_keys = originals_words - self.collisions
        for token in self.name_keys:
            self.text.add_name(token, "", "")
        self.codes = frozenset(alnum_key(code) for code in codes)
        self.ids = frozenset(
            key
            for key in (alnum_key(value) for value in originals.get("patient_ids") or [])
            if len(key) >= MIN_SWEEP_ID and key not in self.codes
        )
        for value in self.ids:
            self.text.add_id(value, "", "")
        self.claim_ids: frozenset[str] = frozenset()
        if self.claim_numbers_replaced:
            self.claim_ids = frozenset(
                key
                for family in ("claim_numbers", "payer_claim_numbers")
                for key in (alnum_key(value) for value in originals.get(family) or [])
                if len(key) >= MIN_SWEEP_ID
            )
            for value in self.claim_ids:
                self.text.add_claim_number(value, "")
        self.lines = frozenset(value_key(fold(value)) for value in originals.get("address_lines") or [])
        self.cities = frozenset(value_key(fold(value)) for value in originals.get("cities") or [])
        self.zips = frozenset(
            value.split("-", 1)[0]
            for value in originals.get("zips") or []
            if alnum_key(value) not in self.codes
        )
        for line in originals.get("address_lines") or []:
            self.text.add_address(line, "", "", "", ("x", "", ""))
        for city in originals.get("cities") or []:
            self.text.add_address("", city, "", "", ("", "x", ""))
        for zip_code in self.zips:
            self.text.add_address("", "", zip_code, "", ("", "", "x"))
        self.dobs = frozenset(date.fromisoformat(value) for value in originals.get("dobs") or [])
        for value in self.dobs:
            self.text.add_dob(value, value, "")
        for name in provider_names:
            self.text.add_keep_name(name, person=True)
        for name in organization_names:
            self.text.add_keep_name(name, person=False)

    # -- single values ------------------------------------------------------------------------

    def _name_tokens(self, value: str, *, every: bool) -> bool:
        """Whether a structured value holds an original name token (``every``: ordinary words too)."""
        for token in NAME_TOKEN_RE.findall(value):
            key = name_key(token)
            if key in self.name_keys and (every or not self.text.ambiguous(key)):
                return True
        return False

    def _ids(self, value: str, *, embedded: bool) -> str | None:
        key = alnum_key(value)
        if not key:
            return None
        if key in self.ids:
            return MEMBER_ID
        if key in self.claim_ids:
            return CLAIM_ID
        parts = {alnum_key(part) for part in re.split(r"[^0-9A-Za-z]+", fold(value)) if part}
        if parts & self.ids:
            return MEMBER_ID
        if parts & self.claim_ids:
            return CLAIM_ID
        if embedded or len(key) > MIN_EMBEDDED_ID:
            for original in self.ids:
                if len(original) >= MIN_EMBEDDED_ID and original in key:
                    return MEMBER_ID
        return None

    def _dob(self, value: str) -> bool:
        return any(v in self.dobs for match, found in date_matches(value) for v in date_values(match, found))

    def cell(self, column: str, value: str) -> list[str]:
        """Kinds of originals in one CSV cell."""
        if not value.strip():
            return []
        kinds: list[str] = []
        if column in TEXT_COLUMNS:
            return list(dict.fromkeys(hit.kind for hit in self.text.find(value)))
        if column in NAME_COLUMNS and self._name_tokens(value, every=True):
            kinds.append(NAME)
        found = self._ids(value, embedded=column in CLAIM_COLUMNS or column in ID_COLUMNS)
        if found is not None:
            kinds.append(ACCOUNT if column == "patient_account_number" and found == MEMBER_ID else found)
        if column == "patient_address_line1" and (
            value_key(fold(value)) in self.lines or value_key(fold(street_without_unit(value))) in self.lines
        ):
            kinds.append(ADDRESS)
        if column == "patient_city" and value_key(fold(value)) in self.cities:
            kinds.append(ADDRESS)
        if column == "patient_zip" and value.strip().split("-", 1)[0] in self.zips:
            kinds.append(ADDRESS)
        if column not in DATE_COLUMNS and column not in PROVIDER_COLUMNS and self._dob(value):
            kinds.append(DOB)
        if column == "patient_address_line1" and any(hit.kind == ADDRESS for hit in self.text.find(value)):
            kinds.append(ADDRESS)
        return list(dict.fromkeys(kinds))

    # -- files --------------------------------------------------------------------------------

    def csv_file(self, name: str, data: bytes) -> list[SweepHit]:
        hits: list[SweepHit] = []
        rows = list(csv.reader(io.StringIO(data.decode("utf-8"))))
        if not rows:
            return hits
        header = rows[0]
        for row_number, row in enumerate(rows[1:], start=2):
            for column, value in zip(header, row, strict=False):
                hits.extend(
                    SweepHit(name, f"row {row_number}", column, kind) for kind in self.cell(column, value)
                )
        return hits

    def remit_file(self, name: str, data: bytes) -> list[SweepHit]:
        text = data.decode("utf-8")
        try:
            delimiters = read_delimiters(text)
            segments = tokenize(text, delimiters)
        except X12Error:
            return [SweepHit(name, "file", "ISA", "UNREADABLE")]
        hits: list[SweepHit] = []
        loop: str | None = None
        for number, seg in enumerate(segments, start=1):
            sid = seg.id
            if sid == "N1":
                loop = {"PR": "payer", "PE": "payee"}.get(seg.element(1) or "")
            elif sid in _LOOP_RESET:
                loop = None
            if loop is not None and sid in _PAYER_PAYEE_SEGMENTS:
                continue
            entity = seg.element(1) or ""
            for index in range(1, len(seg.elements)):
                raw = seg.elements[index].strip()
                if not raw:
                    continue
                for kind in self._element(sid, entity, index, raw, delimiters.sub_element):
                    hits.append(SweepHit(name, f"segment {number}", f"{sid}{index:02d}", kind))
        return hits

    def _element(self, sid: str, entity: str, index: int, value: str, sub: str) -> list[str]:
        kinds: list[str] = []
        names_exempt = (sid == "NM1" and entity not in PERSON_ENTITIES) or sid == "N1"
        if sid == "NM1" and entity in PERSON_ENTITIES and index in (3, 4, 5):
            if self._name_tokens(value, every=True):
                kinds.append(NAME)
        elif not names_exempt and any(
            key in self.name_keys and len(key) > 2 and not self.text.ambiguous(key)
            for key in (name_key(token) for token in NAME_TOKEN_RE.findall(value))
        ):
            kinds.append(NAME)
        for part in value.split(sub) if sub in value else [value]:
            if _AMOUNT_RE.fullmatch(part.strip()):
                continue  # an amount: its digits are no identifier
            found = self._ids(part, embedded=True)
            if found is not None:
                kinds.append(found)
                break
        if index not in DATE_ELEMENTS.get(sid, frozenset()) and any(
            found in self.dobs for found in _compact_dates(value)
        ):
            kinds.append(DOB)
        if sid == "N3" and value_key(fold(value)) in self.lines:
            kinds.append(ADDRESS)
        if sid == "N4" and (
            (index == 1 and value_key(fold(value)) in self.cities)
            or (index == 3 and value.split("-", 1)[0][:5] in self.zips)
        ):
            kinds.append(ADDRESS)
        return list(dict.fromkeys(kinds))

    def file_name(self, name: str) -> list[SweepHit]:
        stem = name.rsplit(".", 1)[0]
        kinds: list[str] = []
        if any(
            key in self.name_keys and len(key) > 2 and not self.text.ambiguous(key)
            for key in (name_key(word) for word in re.findall(r"[^\W\d_]+", fold(stem)))
        ):
            kinds.append(NAME)
        stripped = alnum_key(stem)
        if any(original in stripped for original in self.ids | self.claim_ids):
            kinds.append(MEMBER_ID)
        if self._dob(stem.replace("_", " ")) or any(found in self.dobs for found in _compact_dates(stem)):
            kinds.append(DOB)
        return [SweepHit(name, "file name", "name", kind) for kind in dict.fromkeys(kinds)]


def _compact_dates(value: str) -> list[date]:
    """Dates written as eight digits (CCYYMMDD or MMDDYYYY) inside a value."""
    found: list[date] = []
    for match in re.finditer(r"(?<!\d)\d{8}(?!\d)", value):
        digits = match.group(0)
        for year, month, day in (
            (digits[:4], digits[4:6], digits[6:]),
            (digits[4:], digits[:2], digits[2:4]),
        ):
            try:
                found.append(date(int(year), int(month), int(day)))
            except ValueError:
                continue
    return found


def sweep_outputs(outputs: dict[str, bytes], kinds: dict[str, str], sweep: Sweep) -> list[SweepHit]:
    """Every hit in the outputs; ``kinds`` maps an output name to "csv" or "835"."""
    hits: list[SweepHit] = []
    for name in sorted(outputs):
        hits.extend(sweep.file_name(name))
        if kinds[name] == "835":
            hits.extend(sweep.remit_file(name, outputs[name]))
        else:
            hits.extend(sweep.csv_file(name, outputs[name]))
    return hits


__all__ = ["Sweep", "SweepHit", "sweep_outputs"]
