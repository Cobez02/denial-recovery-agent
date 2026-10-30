"""`denial-agent anonymize`: one raw clinic batch in, one pseudonymized batch and its key out
(docs/SPEC.md section 16).

The input folder holds the clinic's claim export (one or more CSV files with every column of
``app.schemas.CLAIM_EXPORT_COLUMNS``), optionally note excerpts and payer documents (CSV files with
``NOTE_COLUMNS`` / ``PAYER_DOCUMENT_COLUMNS``) and any number of X12 835 files. Files are recognised
by their content (a CSV header, an ``ISA`` segment), never by their name; anything else is skipped
and never copied. Every output CSV carries exactly the contract's columns (extra columns, which
could hold anything, are dropped) and note or document rows for claims the export does not carry
are dropped too (the importer would skip them, and their patient is unknown, so their text cannot
be pseudonymized).

The run reads everything first and collects every original value (:class:`Originals`), then
pseudonymizes the claim export (which defines the patients), the 835s (each claim loop in its
patient's subject, so the payer's spelling maps inside the same patient) and last the free text,
whose replacer then knows every spelling of the patient the batch contains. Every 835 is checked
with :func:`app.anonymize.remit.verify_835`. Nothing is written until all of that succeeded and the
residual scan found nothing (or ``allow_residual`` masked it): then KEY.json is created (never
overwritten) and the output folder filled. The command needs no database, so it can run on the
clinic's own computer; the import of the pseudonymized batch is audited as every import is.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import secrets
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from app.anonymize.pseudonyms import (
    FAMILY,
    GIVEN,
    MAX_SHIFT_DAYS,
    PATIENT,
    REMITTANCE,
    AnonymizeError,
    Originals,
    Pseudonymizer,
    PseudonymKey,
    Subject,
    name_key,
    value_key,
)
from app.anonymize.remit import PATIENT_REF_QUALIFIERS, PERSON_ENTITIES, RemitRewriter, verify_835
from app.anonymize.text import TextReplacer, phone_digits, subject_pairs
from app.config import PROJECT_ROOT
from app.ingest.claims_import import NOTE_COLUMNS, PAYER_DOCUMENT_COLUMNS
from app.ingest.x12 import X12Error, read_delimiters, tokenize
from app.schemas import CLAIM_EXPORT_COLUMNS

log = logging.getLogger(__name__)

CLAIMS = "claims"
NOTES = "notes"
DOCUMENTS = "payer_documents"
REMIT = "835"
COLUMNS: dict[str, tuple[str, ...]] = {
    CLAIMS: CLAIM_EXPORT_COLUMNS,
    NOTES: NOTE_COLUMNS,
    DOCUMENTS: PAYER_DOCUMENT_COLUMNS,
}
TEXT_FIELDS: dict[str, tuple[str, ...]] = {NOTES: ("author", "text"), DOCUMENTS: ("text",)}
DATE_FIELDS: dict[str, tuple[str, ...]] = {
    CLAIMS: ("original_submission_date", "service_date"),
    NOTES: ("note_date",),
    DOCUMENTS: ("received_date",),
}
CLAIM_NUMBER_FIELDS: tuple[str, ...] = (
    "claim_control_number",
    "original_claim_control_number",
    "line_item_control_number",
)
NPI_FIELDS: tuple[str, ...] = ("billing_provider_npi", "rendering_provider_npi", "referring_provider_npi")
ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
_LEADING_NOISE = "﻿ \t\r\n"
_FILE_NAME_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


@dataclass(frozen=True)
class AnonymizeOptions:
    seed: int | None = None
    replace_claim_numbers: bool = False
    shift_dates_days: int = 0
    allow_residual: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "replace_claim_numbers": self.replace_claim_numbers,
            "shift_dates_days": self.shift_dates_days,
            "allow_residual": self.allow_residual,
            "seed": "given" if self.seed is not None else "random",
        }


@dataclass(frozen=True)
class ResidualFinding:
    """Something identifier-shaped left in a free-text field: where and what kind, never the value."""

    file: str
    row: int
    field: str
    kind: str

    def as_dict(self) -> dict[str, Any]:
        return {"file": self.file, "row": self.row, "field": self.field, "kind": self.kind}


@dataclass
class AnonymizeResult:
    out_dir: Path
    key_path: Path
    written: bool
    options: AnonymizeOptions
    files: list[dict[str, Any]] = field(default_factory=list)
    skipped_files: list[str] = field(default_factory=list)
    patients: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    residual: list[ResidualFinding] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        """What the CLI prints: paths, file names, counts, and residual findings by place and kind."""
        blocked = bool(self.residual) and not self.options.allow_residual
        return {
            "status": "residual" if blocked else "ok",
            "written": self.written,
            "out_dir": str(self.out_dir),
            "key_file": str(self.key_path),
            "options": self.options.as_dict(),
            "files": self.files,
            "skipped_files": self.skipped_files,
            "patients": self.patients,
            "counts": self.counts,
            "residual": [finding.as_dict() for finding in self.residual],
        }


@dataclass
class _CsvFile:
    name: str
    kind: str
    rows: list[tuple[int, dict[str, str]]]
    dropped_columns: int


@dataclass
class _RemitFile:
    name: str
    text: str


# --------------------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------------------


def _inside(path: Path, parent: Path) -> bool:
    return path == parent or parent in path.parents


def check_paths(
    in_dir: Path, out_dir: Path, key_path: Path, *, project_root: Path = PROJECT_ROOT
) -> tuple[Path, Path, Path]:
    """Resolve the three paths and refuse every unsafe combination before anything is read.

    Refused: an input that is not a folder; an output folder or key inside the repository (a
    pseudonymized batch is still PHI and the key holds the originals: neither may ever be
    committed); a key inside the output folder (the key must never travel with the batch); an
    output folder that is the input folder or is not empty; a key file that already exists.
    """
    source = in_dir.expanduser().resolve()
    out = out_dir.expanduser().resolve()
    key = key_path.expanduser().resolve()
    root = project_root.resolve()
    if not source.is_dir():
        raise AnonymizeError(f"input folder not found: {source}")
    if _inside(out, root):
        raise AnonymizeError(
            f"--out {out} is inside the repository {root}; a pseudonymized batch is still PHI: "
            "write it to the encrypted data disk outside the repository"
        )
    if _inside(key, root):
        raise AnonymizeError(
            f"--key-out {key} is inside the repository {root}; the key holds the original identifiers "
            "and stays with the clinic, outside the repository"
        )
    if _inside(key, out):
        raise AnonymizeError("--key-out must not be inside --out: the key never travels with the batch")
    if out == source:
        raise AnonymizeError("--out must not be the input folder")
    if key.exists():
        raise AnonymizeError(f"--key-out {key} already exists; an existing key is never overwritten")
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise AnonymizeError(f"--out {out} exists and is not an empty folder")
    return source, out, key


# --------------------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------------------


def _csv_kind(text: str) -> str | None:
    try:
        header = next(csv.reader(io.StringIO(text)), None)
    except csv.Error:
        return None
    names = {column.strip() for column in header or []}
    for kind in (CLAIMS, NOTES, DOCUMENTS):
        if set(COLUMNS[kind]) <= names:
            return kind
    return None


def _read_csv(name: str, kind: str, text: str) -> _CsvFile:
    """Rows as (row number, contract columns only); the header is row 1, like the importer's errors."""
    columns = COLUMNS[kind]
    reader = csv.DictReader(io.StringIO(text))
    rows: list[tuple[int, dict[str, str]]] = []
    try:
        fieldnames = [column.strip() for column in reader.fieldnames or []]
        reader.fieldnames = fieldnames
        for row_number, raw in enumerate(reader, start=2):
            values = {column: raw.get(column) or "" for column in columns}
            if not any(value.strip() for value in values.values()):
                continue  # a blank line
            rows.append((row_number, values))
    except csv.Error as exc:
        raise AnonymizeError(f"{name}: csv error near line {reader.line_num}") from exc
    return _CsvFile(name, kind, rows, dropped_columns=len([c for c in fieldnames if c and c not in columns]))


def _discover(source: Path) -> tuple[list[_CsvFile], list[_RemitFile], list[str]]:
    csv_files: list[_CsvFile] = []
    remits: list[_RemitFile] = []
    skipped: list[str] = []
    for path in sorted((p for p in source.iterdir() if p.is_file()), key=lambda p: p.name):
        text = path.read_bytes().decode("utf-8-sig", errors="replace")
        if text.lstrip(_LEADING_NOISE).startswith("ISA"):
            remits.append(_RemitFile(path.name, text))
            continue
        kind = _csv_kind(text)
        if kind is None:
            skipped.append(path.name)
        else:
            csv_files.append(_read_csv(path.name, kind, text))
    return csv_files, remits, skipped


def _parse_date(file_name: str, row_number: int, column: str, value: str) -> date | None:
    text = value.strip()
    if not text:
        return None
    if ISO_DATE_RE.fullmatch(text):
        try:
            return date.fromisoformat(text)
        except ValueError:
            pass
    raise AnonymizeError(f"{file_name} row {row_number} ({column}: not a YYYY-MM-DD date)")


# --------------------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------------------


@dataclass
class _Index:
    """What the first pass learns besides the originals."""

    ccn_accounts: dict[str, str] = field(default_factory=dict)  # value_key(claim number) -> account
    member_accounts: dict[str, set[str]] = field(default_factory=dict)  # value_key(member id) -> accounts
    keep_numbers: set[str] = field(default_factory=set)  # provider, payer, clinic and claim numbers


def _keep(index: _Index, value: str | None) -> None:
    digits = phone_digits(value or "")
    if len(digits) >= 7:
        index.keep_numbers.add(digits)


def _collect(csv_files: list[_CsvFile], remits: list[_RemitFile]) -> tuple[Originals, _Index]:
    originals = Originals()
    index = _Index()
    for csv_file in csv_files:
        if csv_file.kind != CLAIMS:
            continue
        for row_number, row in csv_file.rows:
            account = row["patient_account_number"].strip()
            originals.add_name(row["patient_first_name"])
            originals.add_name(row["patient_last_name"])
            originals.add_patient_id(account)
            originals.add_patient_id(row["member_id"])
            dob = _parse_date(csv_file.name, row_number, "patient_dob", row["patient_dob"])
            if dob is not None:
                originals.dobs.add(dob)
            originals.add_address(row["patient_address_line1"], row["patient_city"], row["patient_zip"])
            for column in CLAIM_NUMBER_FIELDS:
                originals.add_claim_number(row[column])
                _keep(index, row[column])
            index.ccn_accounts.setdefault(value_key(row["claim_control_number"]), account)
            if row["member_id"].strip():
                index.member_accounts.setdefault(value_key(row["member_id"]), set()).add(account)
            for column in (*NPI_FIELDS, "prior_auth_number"):
                _keep(index, row[column])
    for remit in remits:
        try:
            segments = tokenize(remit.text, read_delimiters(remit.text))
        except X12Error as exc:
            raise AnonymizeError(f"{remit.name}: not a readable X12 interchange ({exc})") from exc
        for seg in segments:
            sid, first = seg.id, seg.element(1)
            if sid == "NM1" and first in PERSON_ENTITIES:
                for position in (3, 4, 5):
                    originals.add_name(seg.element(position))
                originals.add_patient_id(seg.element(9))
            elif sid == "REF" and first in PATIENT_REF_QUALIFIERS:
                originals.add_patient_id(seg.element(2))
            elif sid == "REF" and first == "6R":
                originals.add_claim_number(seg.element(2))
            elif sid == "REF" and first == "F8":
                originals.add_payer_claim_number(seg.element(2))
            elif sid == "CLP":
                originals.add_claim_number(seg.element(1))
                originals.add_payer_claim_number(seg.element(7))
                _keep(index, seg.element(1))
                _keep(index, seg.element(7))
            if sid in ("NM1", "N1") and first not in PERSON_ENTITIES:
                _keep(index, seg.element(9) if sid == "NM1" else seg.element(4))  # NPIs, payer ids
            elif sid == "REF" and first not in PATIENT_REF_QUALIFIERS:
                _keep(index, seg.element(2))  # tax id, receiver id, payer id
            elif sid == "PER":
                for position in (4, 6, 8):
                    _keep(index, seg.element(position))  # the payer's phone and fax
            elif sid in ("TRN", "PLB"):
                _keep(index, seg.element(3) if sid == "TRN" else seg.element(1))
    return originals, index


class _Run:
    """One anonymize run over files already read (see the module docstring)."""

    def __init__(self, options: AnonymizeOptions, originals: Originals, index: _Index) -> None:
        self.options = options
        self.index = index
        self.seed = options.seed if options.seed is not None else secrets.randbits(63)
        self.pseudo = Pseudonymizer(
            originals,
            seed=self.seed,
            shift_days=options.shift_dates_days,
            replace_claim_numbers=options.replace_claim_numbers,
        )
        self.keep_numbers = frozenset(index.keep_numbers)
        self.replacers: dict[str, TextReplacer] = {}
        self.counts: dict[str, int] = {
            "claim_rows": 0,
            "note_rows": 0,
            "document_rows": 0,
            "rows_dropped_unknown_claim": 0,
            "columns_dropped": 0,
            "text_identifiers_replaced": 0,
            "dates_shifted": 0,
            "remittance_claims": 0,
            "remittance_names": 0,
            "remittance_ids": 0,
            "claim_numbers_replaced": 0,
            "residual_masked": 0,
        }
        self.residual: list[ResidualFinding] = []

    # -- claim export -------------------------------------------------------------------------

    def _shift_column(self, file_name: str, row_number: int, column: str, value: str) -> str:
        parsed = _parse_date(file_name, row_number, column, value)
        if parsed is None:
            return value
        self.counts["dates_shifted"] += 1
        return self.pseudo.shift(parsed).isoformat()

    def claim_row(self, file_name: str, row_number: int, row: dict[str, str]) -> dict[str, str]:
        pseudo = self.pseudo
        out = dict(row)
        account = row["patient_account_number"].strip()
        subject = pseudo.subjects.get(account)
        if subject is None:
            subject = pseudo.subject(account, kind=PATIENT)
            subject.seed(row["patient_first_name"], row["patient_last_name"])
        first, last, _middle = subject.person(row["patient_first_name"], row["patient_last_name"])
        out["patient_first_name"], out["patient_last_name"] = first, last
        out["patient_account_number"] = pseudo.patient_id(row["patient_account_number"], subject)
        out["member_id"] = pseudo.patient_id(row["member_id"], subject)
        dob = _parse_date(file_name, row_number, "patient_dob", row["patient_dob"])
        if dob is not None:
            out["patient_dob"] = pseudo.dob(subject, dob).isoformat()
        line1, city, zip_code = pseudo.address(
            subject,
            row["patient_address_line1"],
            row["patient_city"],
            row["patient_state"],
            row["patient_zip"],
        )
        out["patient_address_line1"], out["patient_city"], out["patient_zip"] = line1, city, zip_code
        for column in CLAIM_NUMBER_FIELDS:
            out[column] = pseudo.claim_number(row[column], subject)
            if out[column] != row[column]:
                self.counts["claim_numbers_replaced"] += 1
        if pseudo.shift_days:
            for column in DATE_FIELDS[CLAIMS]:
                out[column] = self._shift_column(file_name, row_number, column, row[column])
        self.counts["claim_rows"] += 1
        return out

    # -- 835 ------------------------------------------------------------------------------------

    def resolve(self, control_number: str, member_ids: list[str], label: str) -> Subject:
        """The subject of a claim loop: the claim export's patient when CLP01 or exactly one of the
        loop's member ids points at one, else a remittance subject of its own."""
        account = self.index.ccn_accounts.get(value_key(control_number)) if control_number else None
        if account is None:
            candidates: set[str] = set()
            for member_id in member_ids:
                candidates |= self.index.member_accounts.get(value_key(member_id), set())
            if len(candidates) == 1:
                account = next(iter(candidates))
        if account is not None and account in self.pseudo.subjects:
            return self.pseudo.subjects[account]
        return self.pseudo.subject("remittance:" + (member_ids[0] if member_ids else label), kind=REMITTANCE)

    def remit(self, remit: _RemitFile) -> tuple[str, dict[str, Any]]:
        result = RemitRewriter(self.pseudo, self.resolve, remit.name).rewrite(remit.text)
        verify_835(remit.name, remit.text, result.text, self.pseudo)
        counts = result.counts
        self.counts["remittance_claims"] += counts.claims
        self.counts["remittance_names"] += counts.names
        self.counts["remittance_ids"] += counts.ids
        self.counts["claim_numbers_replaced"] += counts.claim_numbers
        self.counts["dates_shifted"] += counts.dates_shifted
        return result.text, {
            "transactions": counts.transactions,
            "claims": counts.claims,
            "segments": counts.segments,
        }

    # -- notes and payer documents ----------------------------------------------------------

    def _replacer(self, subject: Subject) -> TextReplacer:
        replacer = self.replacers.get(subject.key)
        if replacer is None:
            replacer = TextReplacer(subject_pairs(subject), keep_numbers=self.keep_numbers)
            self.replacers[subject.key] = replacer
        return replacer

    def text_row(
        self, csv_file: _CsvFile, output_name: str, row_number: int, row: dict[str, str]
    ) -> dict[str, str] | None:
        account = self.index.ccn_accounts.get(value_key(row["claim_control_number"]))
        subject = self.pseudo.subjects.get(account) if account is not None else None
        if subject is None:
            self.counts["rows_dropped_unknown_claim"] += 1
            return None
        out = dict(row)
        out["claim_control_number"] = self.pseudo.claim_number(row["claim_control_number"], subject)
        if self.pseudo.shift_days:
            for column in DATE_FIELDS[csv_file.kind]:
                out[column] = self._shift_column(csv_file.name, row_number, column, row[column])
        replacer = self._replacer(subject)
        for column in TEXT_FIELDS[csv_file.kind]:
            result = replacer.process(
                row[column], shift_days=self.pseudo.shift_days, allow_residual=self.options.allow_residual
            )
            out[column] = result.text
            self.counts["text_identifiers_replaced"] += result.replaced
            self.counts["dates_shifted"] += result.dates_shifted
            self.counts["residual_masked"] += result.masked
            # Once per kind and field: the scanner and the log masks often see the same phone number.
            self.residual.extend(
                ResidualFinding(output_name, row_number, column, kind)
                for kind in dict.fromkeys(result.residual)
            )
        self.counts["note_rows" if csv_file.kind == NOTES else "document_rows"] += 1
        return out


def _render_csv(kind: str, rows: list[dict[str, str]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(COLUMNS[kind])
    for row in rows:
        writer.writerow([row[column] for column in COLUMNS[kind]])
    return buffer.getvalue().encode("utf-8")


def _identifying_words(originals: Originals) -> set[str]:
    words = {token for token in originals.name_tokens if len(token) > 1}
    words |= {re.sub(r"[^0-9a-z]", "", value) for value in originals.patient_ids | originals.zips}
    words |= {d.strftime(fmt) for d in originals.dobs for fmt in ("%Y%m%d", "%m%d%Y")}
    return words - {""}


def _output_names(names: list[tuple[str, str]], originals: Originals) -> dict[str, str]:
    """Input file name -> output file name. A name that carries a patient's name token, id, ZIP or
    date of birth is replaced by ``<kind>_<n>`` with the same extension."""
    identifying = _identifying_words(originals)
    taken: set[str] = set()
    out: dict[str, str] = {}
    counters: dict[str, int] = {}
    for name, kind in names:
        stem, dot, extension = name.rpartition(".")
        stem = stem if dot else name
        words = {
            word.casefold() if any(ch.isdigit() for ch in word) else name_key(word)
            for word in _FILE_NAME_TOKEN_RE.findall(stem)
        }
        candidate = name
        if words & identifying or candidate.casefold() in taken:
            while True:
                counters[kind] = counters.get(kind, 0) + 1
                candidate = f"{kind}_{counters[kind]}" + (f".{extension}" if dot else "")
                if candidate.casefold() not in taken:
                    break
        taken.add(candidate.casefold())
        out[name] = candidate
    return out


def restore_claim_row(row: dict[str, str], key: PseudonymKey) -> dict[str, str]:
    """A claim-export row of a pseudonymized batch with every replaced value put back from the key:
    what a person needs before a corrected claim goes out under the patient's real identifiers."""
    out = dict(row)
    subject = key.subject_for_account(row["patient_account_number"]) or {}
    for column, family in (
        ("patient_account_number", "patient_id"),
        ("member_id", "patient_id"),
        ("patient_address_line1", "address_line"),
        ("patient_city", "city"),
        ("patient_zip", "zip"),
        *((column, "claim_number") for column in CLAIM_NUMBER_FIELDS),
    ):
        if row[column]:
            out[column] = key.original(family, row[column]) or row[column]
    for column, role in (("patient_first_name", GIVEN), ("patient_last_name", FAMILY)):
        if row[column]:
            out[column] = key.original_name(subject, role, row[column]) or row[column]
    if row["patient_dob"]:
        original = key.original_dob(subject, date.fromisoformat(row["patient_dob"]))
        out["patient_dob"] = original.isoformat() if original is not None else row["patient_dob"]
    if key.date_shift_days:
        for column in DATE_FIELDS[CLAIMS]:
            if row[column]:
                out[column] = key.unshift(date.fromisoformat(row[column])).isoformat()
    return out


def _write_key(key_path: Path, document: dict[str, Any]) -> None:
    key_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(key_path, flags, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write((json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


def anonymize_batch(
    in_dir: Path,
    out_dir: Path,
    key_path: Path,
    options: AnonymizeOptions | None = None,
    *,
    project_root: Path = PROJECT_ROOT,
) -> AnonymizeResult:
    """Pseudonymize one raw batch (see the module docstring). Writes nothing when residual
    identifiers were found and ``allow_residual`` is off; ``AnonymizeResult.written`` says which."""
    options = options or AnonymizeOptions()
    if abs(options.shift_dates_days) > MAX_SHIFT_DAYS:
        raise AnonymizeError(f"--shift-dates must be between -{MAX_SHIFT_DAYS} and {MAX_SHIFT_DAYS} days")
    source, out, key = check_paths(in_dir, out_dir, key_path, project_root=project_root)
    csv_files, remits, skipped = _discover(source)
    if not any(f.kind == CLAIMS for f in csv_files):
        raise AnonymizeError(
            "no claim export found in the input folder (a CSV whose header carries every claim-export column)"
        )
    originals, index = _collect(csv_files, remits)
    run = _Run(options, originals, index)
    kinds = [(f.name, f.kind) for f in csv_files] + [(r.name, REMIT) for r in remits]
    names = _output_names(sorted(kinds), originals)

    outputs: dict[str, bytes] = {}
    files: list[dict[str, Any]] = []
    for csv_file in (f for f in csv_files if f.kind == CLAIMS):
        rows = [run.claim_row(csv_file.name, number, row) for number, row in csv_file.rows]
        outputs[names[csv_file.name]] = _render_csv(CLAIMS, rows)
        run.counts["columns_dropped"] += csv_file.dropped_columns
        files.append({"file": names[csv_file.name], "kind": CLAIMS, "rows": len(rows)})
    for remit in remits:
        text, counts = run.remit(remit)
        outputs[names[remit.name]] = text.encode("utf-8")
        files.append({"file": names[remit.name], "kind": REMIT, **counts})
    for csv_file in (f for f in csv_files if f.kind != CLAIMS):
        output_name = names[csv_file.name]
        rows = [
            out_row
            for number, row in csv_file.rows
            if (out_row := run.text_row(csv_file, output_name, number, row)) is not None
        ]
        outputs[output_name] = _render_csv(csv_file.kind, rows)
        run.counts["columns_dropped"] += csv_file.dropped_columns
        files.append({"file": output_name, "kind": csv_file.kind, "rows": len(rows)})

    pseudo = run.pseudo
    patients = sum(1 for subject in pseudo.subjects.values() if subject.kind == PATIENT)
    run.counts["remittance_subjects"] = sum(1 for s in pseudo.subjects.values() if s.kind == REMITTANCE)
    run.counts["duplicate_name_pairs"] = pseudo.duplicate_name_pairs
    run.counts["residual"] = len(run.residual)
    skipped_names = sorted(_output_names([(name, "skipped") for name in skipped], originals).values())
    result = AnonymizeResult(
        out_dir=out,
        key_path=key,
        written=False,
        options=options,
        files=sorted(files, key=lambda f: f["file"]),
        skipped_files=skipped_names,
        patients=patients,
        counts=run.counts,
        residual=run.residual,
    )
    if run.residual and not options.allow_residual:
        log.warning(
            "anonymize stopped: %d residual finding(s) in free text; nothing was written", len(run.residual)
        )
        return result

    document = pseudo.key_document(seed=options.seed, options=options.as_dict(), files=names)
    _write_key(key, document)
    out.mkdir(parents=True, exist_ok=True)
    for name, data in sorted(outputs.items()):
        with open(out / name, "xb") as handle:
            handle.write(data)
    result.written = True
    log.info(
        "anonymize wrote %d file(s): %d patient(s), %d residual finding(s) masked, %d file(s) skipped",
        len(outputs),
        patients,
        run.counts["residual_masked"],
        len(skipped),
    )
    return result


__all__ = [
    "AnonymizeOptions",
    "AnonymizeResult",
    "ResidualFinding",
    "anonymize_batch",
    "check_paths",
    "restore_claim_row",
]
