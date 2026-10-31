"""X12 835 files of `denial-agent anonymize` (docs/SPEC.md section 16).

Only a real 835 is accepted (:func:`interchange_problem`): every interchange of the file uses the
delimiters of the first ISA, every functional group is a remittance (GS01 ``HP``) and every
transaction set an 835 (ST01). An 837, a 277CA, a 271 or any other X12 file is never copied: the
batch run skips it and lists it by position.

A file is rewritten element by element with the delimiters its own ISA declares. Every byte outside
a replaced element is copied as it is (segment order, terminators and the line breaks after them,
empty elements, the fixed-width ISA), so segment counts and control numbers stay what they were. A
replacement never carries a delimiter; the repetition separator is ISA11 only when ISA12 says 00501
or later and ISA11 is not a letter or digit, and no letter or digit is ever taken out of a value.

Replaced, inside a claim loop (CLP to the next CLP, LX, PLB or SE):

- NM1*QC (patient), NM1*IL (subscriber), NM1*74 (corrected name) and NM1*GB (other subscriber):
  last, first and middle name in the patient's subject (an organization named as the subscriber is
  the patient's employer, which a limited data set leaves out too), and NM109 as a patient
  identifier of the same shape;
- REF*1W, 28, EA, IG, SY, 1L and 6P: patient identifiers;
- any other element equal to a patient identifier the batch knows (a payer's own REF qualifier);
- with ``--replace-claim-numbers``: CLP01 and REF*6R (claim numbers), CLP07 and REF*F8 (payer
  claim numbers).

PLB references are rewritten in a second pass, once every file of the batch has been read
(:meth:`RemitRewriter.finish`): a reference equal to a replaced value; the HIC or MBI that Medicare
writes in positions 20 to 30 of a WO or FB reference (``00`` + the adjustment's claim number + the
beneficiary's id), drawn as a patient identifier even for a beneficiary the batch does not know;
any patient identifier of eight characters or more, or a replaced claim number, found inside a
reference. With ``--shift-dates`` (test-only): ISA09, GS04, BPR16, every DTM date and PLB02.
Kept: amounts, every code, payer and payee names, ids and addresses, NPIs, trace numbers.

:func:`verify_835` proves the result. Both files are tokenized and compared segment by segment and
element by element (only the replaceable positions may differ), then both are parsed with
``app.ingest.parse_835`` and compared field by field: everything outside the replaced fields must be
equal, and every replaced field must hold exactly the run's pseudonym of the original. A PLB
reference may still hold no known patient identifier and no MBI-shaped value.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from app.anonymize.pseudonyms import AnonymizeError, Pseudonymizer, Subject, match_case, name_key, value_key
from app.ingest.dates import parse_ccyymmdd, parse_yymmdd
from app.ingest.parse_835 import parse_835
from app.ingest.x12 import Delimiters, X12Error, read_delimiters, tokenize
from app.schemas import Parsed835

PERSON_ENTITIES: frozenset[str] = frozenset({"QC", "IL", "74", "GB"})
PATIENT_REF_QUALIFIERS: frozenset[str] = frozenset({"1W", "28", "EA", "IG", "SY", "1L", "6P"})
CLAIM_NUMBER_REF = "6R"
PAYER_CLAIM_NUMBER_REF = "F8"
LOOP_ENDS: frozenset[str] = frozenset({"LX", "PLB", "SE", "ST", "GE", "IEA", "GS", "ISA"})
PLB_COMPOSITES: tuple[int, ...] = (3, 5, 7, 9, 11, 13)
DTM_PERIOD_FORMATS: frozenset[str] = frozenset({"D8", "RD8"})
CMS_HIC_CODES: frozenset[str] = frozenset({"WO", "FB"})
CMS_HIC_SLICE = slice(19, 30)  # positions 20 to 30 of a WO / FB reference
MIN_EMBEDDED_ID = 8
_MBI_LETTER = "AC-HJKMNP-RT-Y"
MBI_RE = re.compile(
    rf"[1-9][{_MBI_LETTER}][{_MBI_LETTER}0-9]\d[{_MBI_LETTER}][{_MBI_LETTER}0-9]\d[{_MBI_LETTER}]{{2}}\d{{2}}"
)

_LEADING_NOISE = "﻿ \t\r\n"
_CHUNK_RE = re.compile(r"(\s*)(.*?)(\s*)", re.DOTALL)

# resolve(CLP01, member ids of the claim loop, a label for a subject nobody else knows) -> Subject
SubjectResolver = Callable[[str, list[str], str], Subject]


def interchange_problem(text: str) -> str | None:
    """Why an ISA-led file is not an 835 this run may copy (codes only, never a value), or None."""
    try:
        delimiters = read_delimiters(text)
        segments = tokenize(text, delimiters)
    except X12Error:
        return "not a readable X12 interchange"
    body = text.lstrip(_LEADING_NOISE)
    for match in re.finditer(r"ISA(?=[^A-Za-z0-9\s])", body):
        before = body[: match.start()].rstrip()
        if match.start() and not before.endswith(delimiters.segment):
            continue  # "ISA" inside a value
        try:
            if read_delimiters(body[match.start() :]) != delimiters:
                return "a later interchange uses other delimiters"
        except X12Error:
            return "a later interchange is not readable"
    groups = {seg.element(1) or "" for seg in segments if seg.id == "GS"}
    sets = {seg.element(1) or "" for seg in segments if seg.id == "ST"}
    if not sets:
        return "no transaction set"
    if sets != {"835"} or (groups and groups != {"HP"}):
        kinds = ", ".join(sorted(code for code in sets if code.isalnum())) or "unknown"
        return f"X12 {kinds}, not an 835"
    return None


def unsafe_characters(text: str, delimiters: Delimiters) -> frozenset[str]:
    """The characters no replacement may carry: the delimiters, and the repetition separator only
    when ISA12 is 00501 or later and ISA11 is a punctuation mark (never a letter or a digit)."""
    unsafe = {delimiters.element, delimiters.sub_element, delimiters.segment}
    isa = tokenize(text, delimiters)[0] if text else None
    if isa is not None and isa.id == "ISA" and len(isa.elements) > 12:
        repetition, version = isa.elements[11], isa.elements[12].strip()
        if (
            len(repetition) == 1
            and not repetition.isalnum()
            and not repetition.isspace()
            and version >= "00501"
        ):
            unsafe.add(repetition)
    return frozenset(ch for ch in unsafe if not ch.isalnum())


@dataclass
class _Segment:
    lead: str
    elements: list[str]
    trail: str

    @property
    def id(self) -> str:
        return self.elements[0].strip() if self.elements else ""

    def value(self, index: int) -> str:
        return self.elements[index].strip() if 0 <= index < len(self.elements) else ""


@dataclass
class RemitCounts:
    transactions: int = 0
    claims: int = 0
    segments: int = 0
    names: int = 0
    ids: int = 0
    claim_numbers: int = 0
    dates_shifted: int = 0
    plb_references: int = 0

    def as_dict(self) -> dict[str, int]:
        return dict(self.__dict__)


@dataclass
class RemitResult:
    text: str
    counts: RemitCounts
    subjects: list[Subject] = field(default_factory=list)  # one per claim loop, in file order


@dataclass
class PendingRemit:
    """A file rewritten but for its PLB references (see :meth:`RemitRewriter.finish`)."""

    head: str
    segments: list[_Segment]
    delimiters: Delimiters
    unsafe: frozenset[str]
    counts: RemitCounts
    subjects: list[Subject]


def _split(text: str, delimiters: Delimiters) -> tuple[str, list[_Segment]]:
    """(leading noise, segments with the whitespace around each kept for the rewrite)."""
    body = text.lstrip(_LEADING_NOISE)
    head = text[: len(text) - len(body)]
    segments: list[_Segment] = []
    for chunk in body.split(delimiters.segment):
        match = _CHUNK_RE.fullmatch(chunk)
        assert match is not None  # the pattern matches any string
        lead, content, trail = match.groups()
        segments.append(_Segment(lead, content.split(delimiters.element) if content else [], trail))
    return head, segments


def _join(head: str, segments: list[_Segment], delimiters: Delimiters) -> str:
    return head + delimiters.segment.join(
        seg.lead + delimiters.element.join(seg.elements) + seg.trail for seg in segments
    )


def _safe(value: str, unsafe: frozenset[str]) -> str:
    """A replacement can never carry one of the interchange's delimiters (letters and digits stay)."""
    cleaned = "".join(" " if ch in unsafe else ch for ch in value)
    return value if cleaned == value else " ".join(cleaned.split())


def _put(seg: _Segment, index: int, value: str, unsafe: frozenset[str]) -> bool:
    """Replace one element (keeping any padding around it); True when it changed."""
    raw = seg.elements[index]
    stripped = raw.strip()
    if value == stripped:
        return False
    start = raw.index(stripped) if stripped else 0
    seg.elements[index] = raw[:start] + _safe(value, unsafe) + raw[start + len(stripped) :]
    return True


def _claim_loops(segments: list[_Segment]) -> list[int | None]:
    """For each segment, the index of the CLP whose loop it belongs to (None outside claims)."""
    owner: list[int | None] = []
    current: int | None = None
    for index, seg in enumerate(segments):
        if seg.id == "CLP":
            current = index
        elif seg.id in LOOP_ENDS:
            current = None
        owner.append(current)
    return owner


def _claim_number_position(seg_id: str, values: list[str], index: int) -> bool:
    if seg_id == "CLP":
        return index in (1, 7)
    return (
        seg_id == "REF"
        and index == 2
        and len(values) > 1
        and values[1].strip()
        in (
            CLAIM_NUMBER_REF,
            PAYER_CLAIM_NUMBER_REF,
        )
    )


def replaceable_positions(
    seg_id: str,
    values: list[str],
    *,
    in_claim: bool,
    shift: bool,
    claim_numbers: bool,
    known_ids: frozenset[str] = frozenset(),
) -> set[int]:
    """Element positions of one segment that the rewrite is allowed to change."""
    if seg_id == "ISA":
        return {9} if shift else set()
    if seg_id == "GS":
        return {4} if shift else set()
    if seg_id == "BPR":
        return {16} if shift else set()
    if seg_id == "DTM":
        return {2, 6} if shift else set()
    if seg_id == "PLB":
        return ({2} if shift else set()) | set(PLB_COMPOSITES)
    if not in_claim:
        return set()

    def value(index: int) -> str:
        return values[index].strip() if index < len(values) else ""

    allowed = {
        index
        for index in range(1, len(values))
        if value(index)
        and value_key(value(index)) in known_ids
        and not _claim_number_position(seg_id, values, index)
    }
    if seg_id == "CLP":
        return allowed | ({1, 7} if claim_numbers else set())
    if seg_id == "NM1" and value(1) in PERSON_ENTITIES:
        return allowed | {3, 4, 5, 9}
    if seg_id == "REF":
        qualifier = value(1)
        if qualifier in PATIENT_REF_QUALIFIERS:
            allowed.add(2)
        if claim_numbers and qualifier in (CLAIM_NUMBER_REF, PAYER_CLAIM_NUMBER_REF):
            allowed.add(2)
    return allowed


def plb_hic(code: str, reference: str) -> str:
    """The beneficiary's HIC or MBI Medicare writes in positions 20 to 30 of a WO / FB reference."""
    if code.strip().upper() not in CMS_HIC_CODES or len(reference) <= CMS_HIC_SLICE.start:
        return ""
    hic = reference[CMS_HIC_SLICE].strip()
    return hic if any(ch.isalnum() for ch in hic) else ""


class RemitRewriter:
    """Rewrites one 835 file with a run's :class:`Pseudonymizer` (see the module docstring)."""

    def __init__(self, pseudonymizer: Pseudonymizer, resolve: SubjectResolver, label: str) -> None:
        self.pseudo = pseudonymizer
        self.resolve = resolve
        self.label = label  # how errors name the file: its output name, never the input's

    def _shift_ccyymmdd(self, value: str) -> str | None:
        parsed = parse_ccyymmdd(value)
        return None if parsed is None else self.pseudo.shift(parsed).strftime("%Y%m%d")

    def _shift_period(self, value: str) -> str | None:
        parts = value.split("-")
        shifted = [self._shift_ccyymmdd(part) for part in parts]
        return None if any(s is None for s in shifted) else "-".join(s or "" for s in shifted)

    def _subjects(self, segments: list[_Segment], owner: list[int | None]) -> dict[int, Subject]:
        """The subject of every claim loop: CLP01, else the loop's member ids, else a new one."""
        member_ids: dict[int, list[str]] = {}
        labels: dict[int, str] = {}
        for seg, loop in zip(segments, owner, strict=True):
            if loop is None or seg.id != "NM1" or seg.value(1) not in ("QC", "IL"):
                continue
            if seg.value(9):
                member_ids.setdefault(loop, []).append(seg.value(9))
            if seg.value(1) == "QC" and (seg.value(3) or seg.value(4)):
                labels[loop] = f"name:{name_key(seg.value(3))}|{name_key(seg.value(4))}"
        return {
            index: self.resolve(
                seg.value(1), member_ids.get(index, []), labels.get(index, f"{self.label}#{index}")
            )
            for index, seg in enumerate(segments)
            if seg.id == "CLP"
        }

    def rewrite(self, text: str) -> PendingRemit:
        """Everything but the PLB references, which :meth:`finish` rewrites once every file is read."""
        try:
            delimiters = read_delimiters(text)
            unsafe = unsafe_characters(text, delimiters)
        except X12Error as exc:
            raise AnonymizeError(f"{self.label}: not a readable X12 interchange") from exc
        head, segments = _split(text, delimiters)
        owner = _claim_loops(segments)
        subjects = self._subjects(segments, owner)
        counts = RemitCounts(
            transactions=sum(1 for seg in segments if seg.id == "ST"),
            claims=len(subjects),
            segments=sum(1 for seg in segments if seg.elements),
        )
        pseudo = self.pseudo
        shift = bool(pseudo.shift_days)
        known = pseudo.originals.patient_ids
        for seg, loop in zip(segments, owner, strict=True):
            sid = seg.id
            subject = subjects.get(loop) if loop is not None else None
            if shift and sid in ("ISA", "GS", "BPR", "DTM", "PLB"):
                counts.dates_shifted += self._shift_dates(seg, unsafe)
            if subject is None:
                continue
            if sid == "CLP" and len(seg.elements) > 1:
                counts.claim_numbers += _put(seg, 1, pseudo.claim_number(seg.value(1), subject), unsafe)
                if len(seg.elements) > 7 and seg.value(7):
                    new = pseudo.payer_claim_number(seg.value(7), subject)
                    counts.claim_numbers += _put(seg, 7, new, unsafe)
            elif sid == "NM1" and seg.value(1) in PERSON_ENTITIES:
                counts.names += self._person(seg, subject, unsafe)
                if len(seg.elements) > 9 and seg.value(9):
                    counts.ids += _put(seg, 9, pseudo.patient_id(seg.value(9), subject), unsafe)
            elif sid == "REF" and len(seg.elements) > 2 and seg.value(2):
                qualifier = seg.value(1)
                if qualifier in PATIENT_REF_QUALIFIERS:
                    counts.ids += _put(seg, 2, pseudo.patient_id(seg.value(2), subject), unsafe)
                elif qualifier == CLAIM_NUMBER_REF:
                    new = pseudo.claim_number(seg.value(2), subject)
                    counts.claim_numbers += _put(seg, 2, new, unsafe)
                elif qualifier == PAYER_CLAIM_NUMBER_REF:
                    new = pseudo.payer_claim_number(seg.value(2), subject)
                    counts.claim_numbers += _put(seg, 2, new, unsafe)
            # Any other element of the loop that is a known patient id (a payer's own qualifier).
            for index in range(1, len(seg.elements)):
                value = seg.value(index)
                if (
                    value
                    and value_key(value) in known
                    and not _claim_number_position(sid, seg.elements, index)
                ):
                    counts.ids += _put(seg, index, pseudo.patient_id(value, subject), unsafe)
        ordered = [subjects[index] for index in sorted(subjects)]
        return PendingRemit(head, segments, delimiters, unsafe, counts, ordered)

    def finish(self, pending: PendingRemit) -> RemitResult:
        """The PLB references (every claim number and id of the batch is mapped by now), then the text."""
        for seg in pending.segments:
            if seg.id == "PLB":
                pending.counts.plb_references += self._plb_references(seg, pending.delimiters, pending.unsafe)
        text = _join(pending.head, pending.segments, pending.delimiters)
        return RemitResult(text=text, counts=pending.counts, subjects=pending.subjects)

    def _person(self, seg: _Segment, subject: Subject, unsafe: frozenset[str]) -> int:
        if seg.value(2) == "2":  # an organization (the employer as the subscriber): its name as a whole
            return _put(seg, 3, subject.organization(seg.value(3)), unsafe) if seg.value(3) else 0
        first, last, middle = seg.value(4), seg.value(3), seg.value(5)
        pseudo_first, pseudo_last, pseudo_middle = subject.person(first, last, middle)
        changed = 0
        for index, value in ((3, pseudo_last), (4, pseudo_first), (5, pseudo_middle)):
            if index < len(seg.elements) and seg.value(index):
                changed += _put(seg, index, value, unsafe)
        return changed

    def _shift_dates(self, seg: _Segment, unsafe: frozenset[str]) -> int:
        sid = seg.id
        changed = 0
        if sid == "ISA" and len(seg.elements) > 9:
            parsed = parse_yymmdd(seg.value(9))
            if parsed is not None:
                changed += _put(seg, 9, self.pseudo.shift(parsed).strftime("%y%m%d"), unsafe)
            return changed
        positions = {"GS": (4,), "BPR": (16,), "DTM": (2,), "PLB": (2,)}[sid]
        for index in positions:
            if index < len(seg.elements) and seg.value(index):
                shifted = self._shift_ccyymmdd(seg.value(index))
                if shifted is not None:
                    changed += _put(seg, index, shifted, unsafe)
        if sid == "DTM" and seg.value(5) in DTM_PERIOD_FORMATS and len(seg.elements) > 6 and seg.value(6):
            shifted = self._shift_period(seg.value(6))
            if shifted is not None:
                changed += _put(seg, 6, shifted, unsafe)
        return changed

    def plb_reference(self, code: str, reference: str) -> str:
        """One PLB reference with every patient identifier and replaced claim number in it replaced."""
        pseudo = self.pseudo
        exact = (
            pseudo.claim_numbers.lookup(reference)
            or pseudo.payer_claim_numbers.lookup(reference)
            or pseudo.patient_ids.lookup(reference)
        )
        if exact is not None:
            return exact
        out = reference
        hic = plb_hic(code, reference)
        if hic:
            start = reference.index(hic, CMS_HIC_SLICE.start)
            out = out[:start] + pseudo.patient_id(hic) + out[start + len(hic) :]
        maps = [pseudo.patient_ids]
        if pseudo.replace_claim_numbers:
            maps += [pseudo.claim_numbers, pseudo.payer_claim_numbers]
        found: list[tuple[str, str]] = []
        for value_map in maps:
            for original, pseudonym in value_map.items():
                if len(original) >= MIN_EMBEDDED_ID or value_map is not pseudo.patient_ids:
                    found.append((original, pseudonym))
        for original, pseudonym in sorted(found, key=lambda pair: -len(pair[0])):
            if len(original) < 5:
                continue
            index = out.casefold().find(original.casefold())
            while index >= 0 and len(pseudonym) == len(original):
                out = (
                    out[:index]
                    + match_case(out[index : index + len(original)], pseudonym)
                    + out[index + len(original) :]
                )
                index = out.casefold().find(original.casefold(), index + len(original))
        return out

    def _plb_references(self, seg: _Segment, delimiters: Delimiters, unsafe: frozenset[str]) -> int:
        changed = 0
        for index in PLB_COMPOSITES:
            if index >= len(seg.elements) or not seg.value(index):
                continue
            parts = seg.elements[index].split(delimiters.sub_element)
            if len(parts) < 2 or not parts[1].strip():
                continue
            reference = parts[1].strip()
            replacement = self.plb_reference(parts[0], reference)
            if replacement != reference:
                parts[1] = _safe(replacement, unsafe)
                seg.elements[index] = delimiters.sub_element.join(parts)
                changed += 1
        return changed


# --------------------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------------------

_PATIENT_NAME_FIELDS = ("patient_last_name", "patient_first_name", "insured_last_name", "insured_first_name")
_CLAIM_DATE_FIELDS = ("claim_received_date", "statement_from", "statement_to")


def _fail(label: str, what: str) -> AnonymizeError:
    return AnonymizeError(f"835 check failed for {label}: {what}")


def _blank(
    parsed: Parsed835, *, shift: bool, claim_numbers: bool, known_ids: frozenset[str]
) -> dict[str, Any]:
    """The parsed transaction without the fields the rewrite replaces (compared separately)."""
    data = parsed.model_dump(mode="json")
    data["unparsed_segments"] = len(data["unparsed_segments"])  # compared segment by segment
    if shift:
        data["interchange_date"] = None
        data["payment"]["payment_date"] = None
        data["payment"]["production_date"] = None
    for claim in data["claims"]:
        for name in (*_PATIENT_NAME_FIELDS, "member_id"):
            claim[name] = None
        for qualifier, value in list(claim["references"].items()):
            if (
                qualifier in PATIENT_REF_QUALIFIERS
                or (claim_numbers and qualifier in (CLAIM_NUMBER_REF, PAYER_CLAIM_NUMBER_REF))
                or (value and value_key(str(value)) in known_ids)
            ):
                claim["references"][qualifier] = None
        if claim_numbers:
            claim["patient_control_number"] = None
            claim["payer_claim_control_number"] = None
        if shift:
            for name in _CLAIM_DATE_FIELDS:
                claim[name] = None
        for service in claim["services"]:
            if claim_numbers:
                service["line_item_control_number"] = None
            if shift:
                service["service_date"] = None
    for adjustment in data["provider_adjustments"]:
        adjustment["reference"] = None
        if shift:
            adjustment["fiscal_period_date"] = None
    return data


def _check_segments(label: str, original: str, rewritten: str, pseudo: Pseudonymizer) -> None:
    before_delims, after_delims = read_delimiters(original), read_delimiters(rewritten)
    if before_delims != after_delims:
        raise _fail(label, "the ISA delimiters differ")
    before, after = tokenize(original, before_delims), tokenize(rewritten, after_delims)
    if len(before) != len(after):
        raise _fail(label, "the segment count differs")
    in_claim = False
    shift = bool(pseudo.shift_days)
    known = frozenset(pseudo.originals.patient_ids)
    for number, (old, new) in enumerate(zip(before, after, strict=True), start=1):
        if old.id == "CLP":
            in_claim = True
        elif old.id in LOOP_ENDS:
            in_claim = False
        if old.id != new.id or len(old.elements) != len(new.elements):
            raise _fail(label, f"segment {number} ({old.id}) changed shape")
        allowed = replaceable_positions(
            old.id,
            old.elements,
            in_claim=in_claim,
            shift=shift,
            claim_numbers=pseudo.replace_claim_numbers,
            known_ids=known,
        )
        for index, (a, b) in enumerate(zip(old.elements, new.elements, strict=True)):
            if a == b:
                continue
            if index not in allowed:
                raise _fail(label, f"segment {number} ({old.id}) element {index:02d} changed")
            if old.id == "PLB" and index in PLB_COMPOSITES:
                a_parts, b_parts = a.split(before_delims.sub_element), b.split(after_delims.sub_element)
                if len(a_parts) != len(b_parts) or a_parts[0] != b_parts[0] or a_parts[2:] != b_parts[2:]:
                    raise _fail(
                        label, f"segment {number} (PLB) element {index:02d} changed beyond its reference"
                    )
            elif (
                in_claim
                and value_key(a.strip()) in known
                and not _claim_number_position(old.id, old.elements, index)
            ):
                if b.strip() != (pseudo.patient_ids.lookup(a.strip()) or a.strip()):
                    raise _fail(
                        label, f"segment {number} ({old.id}) element {index:02d} does not hold the pseudonym"
                    )
        if old.id == "PLB":
            _check_plb(label, number, new.elements, after_delims, pseudo)


def _check_plb(
    label: str, number: int, elements: list[str], delimiters: Delimiters, pseudo: Pseudonymizer
) -> None:
    """A rewritten PLB reference holds no known patient id and no MBI-shaped value."""
    long_ids = [original for original in pseudo.originals.patient_ids if len(original) >= MIN_EMBEDDED_ID]
    for index in PLB_COMPOSITES:
        if index >= len(elements):
            continue
        parts = elements[index].split(delimiters.sub_element)
        reference = parts[1].strip() if len(parts) > 1 else ""
        if not reference:
            continue
        folded = reference.casefold()
        if value_key(reference) in pseudo.originals.patient_ids or any(i in folded for i in long_ids):
            raise _fail(label, f"segment {number} (PLB) element {index:02d} still holds a patient identifier")
        for match in MBI_RE.finditer(reference.upper()):
            if not pseudo.patient_ids.is_pseudonym(match.group(0)):
                raise _fail(label, f"segment {number} (PLB) element {index:02d} holds an MBI-shaped value")


def _shifted(pseudo: Pseudonymizer, value: date | None) -> date | None:
    return None if value is None else value + timedelta(days=pseudo.shift_days)


class _ParsedCheck:
    """Field-by-field comparison of one original and one rewritten transaction set."""

    def __init__(self, label: str, pseudo: Pseudonymizer) -> None:
        self.label = label
        self.pseudo = pseudo
        self.shift = bool(pseudo.shift_days)
        self.claim_numbers = pseudo.replace_claim_numbers
        self.known = frozenset(pseudo.originals.patient_ids)

    def expect(self, what: str, actual: Any, expected: Any) -> None:
        if actual != expected:
            raise _fail(self.label, f"{what} does not hold the expected value")

    def expect_date(self, what: str, actual: date | None, original: date | None) -> None:
        self.expect(what, actual, _shifted(self.pseudo, original))

    def transaction(self, old: Parsed835, new: Parsed835) -> None:
        options = {"shift": self.shift, "claim_numbers": self.claim_numbers, "known_ids": self.known}
        if _blank(old, **options) != _blank(new, **options):
            raise _fail(self.label, "a parsed value outside the replaced fields differs")
        if self.shift:
            self.expect_date("ISA09", new.interchange_date, old.interchange_date)
            self.expect_date("BPR16", new.payment.payment_date, old.payment.payment_date)
            self.expect_date("DTM*405", new.payment.production_date, old.payment.production_date)
        for number, (a, b) in enumerate(zip(old.claims, new.claims, strict=True), start=1):
            self.claim(f"claim {number}", a, b)
        for number, (a, b) in enumerate(
            zip(old.provider_adjustments, new.provider_adjustments, strict=True), 1
        ):
            if self.shift:
                self.expect_date(f"PLB {number} PLB02", b.fiscal_period_date, a.fiscal_period_date)

    def claim(self, where: str, a: Any, b: Any) -> None:
        pseudo = self.pseudo
        if self.claim_numbers:
            ccn = pseudo.claim_numbers.lookup(a.patient_control_number) or a.patient_control_number
            self.expect(f"{where} CLP01", b.patient_control_number, ccn)
            if a.payer_claim_control_number:
                payer_ccn = pseudo.payer_claim_numbers.lookup(a.payer_claim_control_number)
                self.expect(f"{where} CLP07", b.payer_claim_control_number, payer_ccn)
        if a.member_id:
            self.expect(f"{where} member id", b.member_id, pseudo.patient_ids.lookup(a.member_id))
        for name in _PATIENT_NAME_FIELDS:
            old_name, new_name = getattr(a, name), getattr(b, name)
            if old_name and any(ch.isalpha() for ch in old_name) and new_name in (None, "", old_name):
                raise _fail(self.label, f"{where} {name} was not replaced")
        for qualifier, value in a.references.items():
            if qualifier in PATIENT_REF_QUALIFIERS or (value and value_key(str(value)) in self.known):
                expected = pseudo.patient_ids.lookup(value)
            elif self.claim_numbers and qualifier == PAYER_CLAIM_NUMBER_REF:
                expected = pseudo.payer_claim_numbers.lookup(value)
            else:
                continue
            self.expect(f"{where} REF*{qualifier}", b.references.get(qualifier), expected)
        if self.shift:
            for name in _CLAIM_DATE_FIELDS:
                self.expect_date(f"{where} {name}", getattr(b, name), getattr(a, name))
        for line, (x, y) in enumerate(zip(a.services, b.services, strict=True), start=1):
            if self.claim_numbers and x.line_item_control_number:
                licn = pseudo.claim_numbers.lookup(x.line_item_control_number)
                self.expect(f"{where} line {line} REF*6R", y.line_item_control_number, licn)
            if self.shift:
                self.expect_date(f"{where} line {line} DTM*472", y.service_date, x.service_date)


def _check_parsed(label: str, original: str, rewritten: str, pseudo: Pseudonymizer) -> None:
    before, after = parse_835(original), parse_835(rewritten)
    if len(before) != len(after):
        raise _fail(label, "the number of transaction sets differs")
    check = _ParsedCheck(label, pseudo)
    for old, new in zip(before, after, strict=True):
        check.transaction(old, new)


def verify_835(label: str, original: str, rewritten: str, pseudo: Pseudonymizer) -> None:
    """Raise AnonymizeError (positions and field names only) unless ``rewritten`` is ``original``
    with only the replaceable elements changed and every replaced value equal to the run's pseudonym."""
    try:
        _check_segments(label, original, rewritten, pseudo)
        _check_parsed(label, original, rewritten, pseudo)
    except X12Error as exc:
        raise _fail(label, "the parser refused a file") from exc


__all__ = [
    "MBI_RE",
    "PATIENT_REF_QUALIFIERS",
    "PERSON_ENTITIES",
    "PendingRemit",
    "RemitCounts",
    "RemitResult",
    "RemitRewriter",
    "interchange_problem",
    "plb_hic",
    "replaceable_positions",
    "unsafe_characters",
    "verify_835",
]
