"""`denial-agent anonymize`: one raw clinic batch in, one pseudonymized batch and its key out
(docs/SPEC.md section 16).

**Input.** The folder holds the clinic's claim export (CSV files with every column of
``app.schemas.CLAIM_EXPORT_COLUMNS``), optionally note excerpts and payer documents (CSV files with
``NOTE_COLUMNS`` / ``PAYER_DOCUMENT_COLUMNS``) and any number of X12 835 files. Files are recognised
by their content, never by their name, and are named in every message by their position in the
sorted listing ("input file 3"), never by name. A CSV header is compared without case; a CSV that
shares two or more columns with a contract file but lacks some is refused with the missing column
names. A row whose field count differs from its header is refused (an unquoted comma would shift
every later column into a kept one). A file is read as UTF-8, else as Windows-1252 (counted); a
recognised file holding U+FFFD replacement characters is refused. Only a real 835 is accepted
(``app.anonymize.remit.interchange_problem``); an 837, a 277CA, a 271, any other file and any
subfolder is skipped, never copied, and listed by position with the reason.

**First pass.** Every original value is collected (:class:`Originals`), with the claim index, the
numbers to keep (NPIs, tax ids, payer phones, claim numbers), the provider, staff and organization
names (pseudonyms are drawn outside them and free text keeps them), the clinic's billing NPIs and tax
ids (a key of another clinic is refused), the batch's service, payment and note dates, its
procedure and diagnosis codes, and the identity columns of the export the contract drops (insured,
subscriber, guarantor names, secondary ids: their values are replaced in free text before the
columns are dropped). A claim number that carries a patient's account number or member id refuses
the run unless claim numbers are replaced.

**The run.** The claim export defines the patients, the 835s are rewritten (each claim loop in its
patient's subject; PLB references in a second pass once every file is read), then the free text of
every note and letter is rewritten with every patient of the batch and of the key
(``app.anonymize.text``). Every 835 is checked with ``verify_835``. Then the leak sweep
(``app.anonymize.sweep``) searches every output byte and file name for every original the new key
holds; one hit and nothing is written.

**Output.** Files are written as ``pseudonymized_run<N>_<kind>_<n>.<ext>`` (the importer records the
name, and the export marks every package of such a claim) into a temporary sibling of OUT_DIR that is
renamed at the end; KEY.json is written last, also under a temporary name, with an owner-only ACL,
and never over an existing file. On any failure both are removed. One JSON line goes to the run log
next to the key. The command needs no database and audits nothing (SPEC section 0 lists the
exception); the import of the pseudonymized batch is audited as every import is.

**A later batch** (``key_in``) is pseudonymized with the newest key of the clinic's chain: the run
starts from everything that key maps and from its claim index, so a known patient, member id or
claim number keeps its pseudonym and an 835 or letter about an earlier claim finds its patient; a
claim export is then optional. The options must equal the key's, the batch's billing NPIs must meet
the key's, and a key the run log shows a newer run for is refused. The extended key goes to a new
file: the key read is never overwritten.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import re
import unicodedata
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from app.anonymize import paths
from app.anonymize.matching import alnum_key, name_key
from app.anonymize.pseudonyms import (
    FAMILY,
    GIVEN,
    MAX_SHIFT_DAYS,
    NAME_TOKEN_RE,
    PATIENT,
    REMITTANCE,
    AnonymizeError,
    Originals,
    Pseudonymizer,
    PseudonymKey,
    Subject,
    load_key,
    value_key,
)
from app.anonymize.remit import (
    PATIENT_REF_QUALIFIERS,
    PERSON_ENTITIES,
    PendingRemit,
    RemitRewriter,
    interchange_problem,
    plb_hic,
    verify_835,
)
from app.anonymize.sweep import Sweep, SweepHit, sweep_outputs
from app.anonymize.text import TextIndex, lowercase_corpus, phone_digits
from app.config import PROJECT_ROOT
from app.ingest.claims_import import NOTE_COLUMNS, PAYER_DOCUMENT_COLUMNS
from app.ingest.x12 import X12Error, read_delimiters, tokenize
from app.review import pseudonymized
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
OUTPUT_KINDS: dict[str, tuple[str, str]] = {
    CLAIMS: ("claims", "csv"),
    NOTES: ("notes", "csv"),
    DOCUMENTS: ("payer_documents", "csv"),
    REMIT: ("remit", "835"),
}
TEXT_FIELDS: dict[str, tuple[str, ...]] = {NOTES: ("author", "text"), DOCUMENTS: ("document_type", "text")}
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
NEAR_MISS_COLUMNS = 2
_LEADING_BYTES = b"\xef\xbb\xbf \t\r\n"

# Identity columns of an export that the contract leaves out: harvested, then dropped.
_PERSON_WORDS = frozenset(
    {
        "insured",
        "subscriber",
        "guarantor",
        "responsible",
        "policyholder",
        "holder",
        "parent",
        "guardian",
        "spouse",
        "emergency",
        "contact",
        "maiden",
        "alias",
        "aka",
        "preferred",
        "nickname",
        "middle",
    }
)
_ROLE_WORDS = {
    "first": GIVEN,
    "given": GIVEN,
    "last": FAMILY,
    "family": FAMILY,
    "surname": FAMILY,
    "middle": "middle",
    "initial": "middle",
    "mi": "middle",
    "name": "full",
}
_ID_ALONE = frozenset({"mrn", "chart", "ssn", "mbi", "hicn", "hic"})
_ID_OWNERS = frozenset(
    {
        "member",
        "subscriber",
        "policy",
        "insured",
        "medicaid",
        "medicare",
        "secondary",
        "alternate",
        "alt",
        "other",
        "patient",
        "insurance",
        "medical",
    }
)
_ID_MARKS = frozenset({"id", "number", "no", "num", "nbr", "record"})


@dataclass(frozen=True)
class AnonymizeOptions:
    seed: int | None = None  # test-only
    replace_claim_numbers: bool = False
    shift_dates_days: int = 0  # test-only
    allow_residual: bool = False
    key_in: Path | None = None
    test_only: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "replace_claim_numbers": self.replace_claim_numbers,
            "shift_dates_days": self.shift_dates_days,
            "allow_residual": self.allow_residual,
            "test_only": self.test_only,
            "seed": "given" if self.seed is not None else "random",
            "reuses_key": self.key_in is not None,
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
    key_in: Path | None = None
    chain: dict[str, Any] = field(default_factory=dict)
    key_acl: str = ""
    files: list[dict[str, Any]] = field(default_factory=list)
    skipped_files: list[dict[str, Any]] = field(default_factory=list)
    patients: int = 0
    counts: dict[str, int] = field(default_factory=dict)
    residual: list[ResidualFinding] = field(default_factory=list)
    leaks: list[SweepHit] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.leaks:
            return "leak"
        if self.residual and not self.options.allow_residual:
            return "residual"
        return "ok"

    def summary(self) -> dict[str, Any]:
        """What the CLI prints: paths, file names, counts, findings by place and kind (never a value)."""
        return {
            "status": self.status,
            "written": self.written,
            "out_dir": str(self.out_dir),
            "key_file": str(self.key_path),
            "key_in": str(self.key_in) if self.key_in is not None else None,
            "chain": self.chain,
            "key_acl": self.key_acl,
            "options": self.options.as_dict(),
            "files": self.files,
            "skipped_files": self.skipped_files,
            "patients": self.patients,
            "counts": self.counts,
            "residual": [finding.as_dict() for finding in self.residual],
            "leaks": [hit.as_dict() for hit in self.leaks],
        }


@dataclass
class _CsvFile:
    position: int
    name: str  # the input file name: written to the key only, never printed
    kind: str
    rows: list[tuple[int, dict[str, str], dict[str, str]]]  # (row number, contract values, identity extras)
    dropped_columns: int
    extra_roles: dict[str, tuple[str, str]]
    encoding: str

    @property
    def label(self) -> str:
        return f"input file {self.position}"


@dataclass
class _RemitFile:
    position: int
    name: str
    text: str
    encoding: str

    @property
    def label(self) -> str:
        return f"input file {self.position}"


# --------------------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------------------


def _refuse_synced(path: Path, what: str) -> None:
    client = paths.synced_folder(path)
    if client is not None:
        raise AnonymizeError(
            f"{what} {path} is in a {client} folder, which copies it to a cloud outside the clinic's "
            "agreements; use a local folder (Documents and Desktop are often redirected into OneDrive)"
        )


def check_paths(
    in_dir: Path,
    out_dir: Path,
    key_path: Path,
    *,
    key_in: Path | None = None,
    project_root: Path = PROJECT_ROOT,
) -> tuple[Path, Path, Path, Path | None]:
    """Resolve the paths and refuse every unsafe combination before anything is read.

    Refused: an input that is not a folder; an input, output folder or key inside the repository;
    input and output folders that are the same or nested; a key inside or beside the output folder
    (the folder that travels), or on the same removable or network volume; an output folder or key
    in a synced folder; an output folder that is not empty; a key file that already exists; a
    ``key_in`` that is missing, inside the repository, synced, inside the output folder or the
    same file as the key to write.
    """
    source = paths.canonical(in_dir)
    out = paths.canonical(out_dir)
    key = paths.canonical(key_path)
    reused = paths.canonical(key_in) if key_in is not None else None
    root = paths.canonical(project_root)
    if reused is not None:
        if not reused.is_file():
            raise AnonymizeError(f"--key-in {reused} not found")
        if paths.inside(reused, root):
            raise AnonymizeError(
                f"--key-in {reused} is inside the repository {root}; a key never lives there: move it "
                "back to the clinic's computer"
            )
        if paths.inside(reused, out):
            raise AnonymizeError("--key-in must not be inside --out: the key never travels with the batch")
        if reused == key:
            raise AnonymizeError(
                "--key-out must be a new file: the key read with --key-in is never overwritten"
            )
        _refuse_synced(reused, "--key-in")
    if not source.is_dir():
        raise AnonymizeError(f"input folder not found: {source}")
    if paths.inside(source, root):
        raise AnonymizeError(
            f"the input folder {source} is inside the repository {root}; a raw batch never comes near a "
            "git working tree: keep it on the clinic's computer"
        )
    if paths.inside(out, root):
        raise AnonymizeError(
            f"--out {out} is inside the repository {root}; a pseudonymized batch is still PHI: "
            "write it to the encrypted data disk outside the repository"
        )
    if paths.inside(key, root):
        raise AnonymizeError(
            f"--key-out {key} is inside the repository {root}; the key holds the original identifiers "
            "and stays with the clinic, outside the repository"
        )
    if out == source:
        raise AnonymizeError("--out must not be the input folder")
    if paths.overlap(out, source):
        raise AnonymizeError("--out and the input folder must not be inside one another")
    if paths.inside(key, out):
        raise AnonymizeError("--key-out must not be inside --out: the key never travels with the batch")
    if key.parent == out.parent:
        raise AnonymizeError(
            "--key-out must not sit beside --out: a copy of their folder would carry both; keep the key "
            "in a folder of its own on the clinic's computer"
        )
    if paths.same_volume(key, out) and paths.removable_or_remote(out):
        raise AnonymizeError(
            "--key-out is on the removable or network volume --out is written to; the key stays on the "
            "clinic's own disk"
        )
    _refuse_synced(out, "--out")
    _refuse_synced(key, "--key-out")
    if key.exists():
        raise AnonymizeError(f"--key-out {key} already exists; an existing key is never overwritten")
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise AnonymizeError(f"--out {out} exists and is not an empty folder")
    return source, out, key, reused


# --------------------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------------------


def _normalize_column(name: str) -> str:
    return re.sub(r"[\s\-]+", "_", name.strip().lstrip("﻿").casefold())


def _decode(data: bytes) -> tuple[str, str] | None:
    """(text, encoding): UTF-8 (a BOM dropped), else Windows-1252; None when it is neither."""
    try:
        text, encoding = data.decode("utf-8-sig"), "utf-8"
    except UnicodeDecodeError:
        try:
            text, encoding = data.decode("cp1252"), "cp1252"
        except UnicodeDecodeError:
            return None
    return unicodedata.normalize("NFC", text), encoding


def _refuse_damaged(label: str, text: str) -> None:
    if "�" in text:
        raise AnonymizeError(
            f"{label} holds U+FFFD replacement characters (text damaged by an earlier encoding error); "
            "export it again as UTF-8"
        )


def _csv_kind(text: str) -> tuple[str | None, tuple[str, list[str]] | None]:
    """(contract kind, None), or (None, (kind, missing columns)) for a near miss, or (None, None)."""
    try:
        header = next(csv.reader(io.StringIO(text)), None)
    except csv.Error:
        return None, None
    names = {_normalize_column(column) for column in header or []}
    for kind in (CLAIMS, NOTES, DOCUMENTS):
        if set(COLUMNS[kind]) <= names:
            return kind, None
    best: tuple[int, str] | None = None
    for kind in (CLAIMS, NOTES, DOCUMENTS):
        shared = len(set(COLUMNS[kind]) & names)
        if shared >= NEAR_MISS_COLUMNS and (best is None or shared > best[0]):
            best = (shared, kind)
    if best is None:
        return None, None
    kind = best[1]
    return None, (kind, [column for column in COLUMNS[kind] if column not in names])


def _extra_role(column: str) -> tuple[str, str] | None:
    """("name", "<person>:<role>") or ("id", "") for an identity column the contract does not carry."""
    words = [word for word in column.split("_") if word]
    if not words:
        return None
    roles = [_ROLE_WORDS[word] for word in words if word in _ROLE_WORDS]
    if roles and (any(word in _PERSON_WORDS for word in words) or words[0] == "patient"):
        for wanted in (GIVEN, FAMILY, "middle", "full"):
            if wanted in roles:
                person = "_".join(word for word in words if word not in _ROLE_WORDS) or "patient"
                if person == "patient" and wanted != "middle":
                    return None  # the contract's own first and last name
                return "name", f"{person}:{wanted}"
    if any(word in _ID_ALONE for word in words) or (
        any(word in _ID_OWNERS for word in words) and any(word in _ID_MARKS for word in words)
    ):
        return "id", ""
    return None


def _read_csv(position: int, name: str, kind: str, text: str, encoding: str) -> _CsvFile:
    """Rows as (row number, contract columns, identity extras); the header is row 1, like the importer."""
    label = f"input file {position}"
    reader = csv.reader(io.StringIO(text))
    rows: list[tuple[int, dict[str, str], dict[str, str]]] = []
    try:
        header = next(reader)
        normalized = [_normalize_column(column) for column in header]
        index_of: dict[str, int] = {}
        for index, column in enumerate(normalized):
            index_of.setdefault(column, index)
        columns = COLUMNS[kind]
        extras: dict[int, tuple[str, str]] = {}
        dropped = 0
        for index, column in enumerate(normalized):
            if column and column not in columns:
                dropped += 1
                role = _extra_role(column) if kind == CLAIMS else None
                if role is not None:
                    extras[index] = role
        for row_number, raw in enumerate(reader, start=2):
            if not any(cell.strip() for cell in raw):
                continue  # a blank line
            if len(raw) < len(header) or (
                len(raw) > len(header) and any(c.strip() for c in raw[len(header) :])
            ):
                raise AnonymizeError(
                    f"{label} row {row_number} has {len(raw)} fields where its header has {len(header)}; an "
                    "unquoted comma would shift every later column, so nothing was read: quote the field"
                )
            values = {column: raw[index_of[column]] for column in columns}
            identity = {normalized[index]: raw[index] for index in extras if raw[index].strip()}
            rows.append((row_number, values, identity))
    except csv.Error as exc:
        raise AnonymizeError(f"{label}: csv error near line {reader.line_num}") from exc
    except StopIteration as exc:
        raise AnonymizeError(f"{label} has no header") from exc
    roles = {normalized[index]: role for index, role in extras.items()}
    return _CsvFile(position, name, kind, rows, dropped, roles, encoding)


def _discover(source: Path) -> tuple[list[_CsvFile], list[_RemitFile], list[dict[str, Any]], int]:
    """(CSV files, 835 files, skipped files by position and reason, files read as Windows-1252)."""
    csv_files: list[_CsvFile] = []
    remits: list[_RemitFile] = []
    skipped: list[dict[str, Any]] = []
    cp1252 = 0
    for position, path in enumerate(sorted(source.iterdir(), key=lambda p: p.name.casefold()), start=1):
        label = f"input file {position}"

        def skip(reason: str, position: int = position) -> None:
            skipped.append({"position": position, "reason": reason})

        if path.is_dir():
            skip("a folder: its files are not read")
            continue
        if not path.is_file():
            skip("not a regular file")
            continue
        data = path.read_bytes()
        if not data.strip(_LEADING_BYTES):
            skip("an empty file")
            continue
        if b"\x00" in data[:4096]:
            skip("not a text file")
            continue
        decoded = _decode(data)
        if decoded is None:
            skip("neither UTF-8 nor Windows-1252 text")
            continue
        text, encoding = decoded
        if data.lstrip(_LEADING_BYTES).startswith(b"ISA"):
            problem = interchange_problem(text)
            if problem is not None:
                skip(problem)
                continue
            _refuse_damaged(label, text)
            remits.append(_RemitFile(position, path.name, text, encoding))
        else:
            kind, near = _csv_kind(text)
            if near is not None:
                near_kind, missing = near
                raise AnonymizeError(
                    f"{label} looks like a {near_kind.replace('_', ' ')} CSV but lacks the column(s) "
                    f"{', '.join(missing)} (column names are compared without case); fix its header, or take "
                    "the file out of the input folder"
                )
            if kind is None:
                skip("not a claim export, notes or payer documents CSV, and not an 835")
                continue
            _refuse_damaged(label, text)
            csv_files.append(_read_csv(position, path.name, kind, text, encoding))
        cp1252 += encoding == "cp1252"
    return csv_files, remits, skipped, cp1252


def _parse_date(label: str, row_number: int, column: str, value: str) -> date | None:
    text = value.strip()
    if not text:
        return None
    if ISO_DATE_RE.fullmatch(text):
        try:
            return date.fromisoformat(text)
        except ValueError:
            pass
    raise AnonymizeError(f"{label} row {row_number} ({column}: not a YYYY-MM-DD date)")


def _maybe_date(value: str) -> date | None:
    text = value.strip()
    if ISO_DATE_RE.fullmatch(text):
        try:
            return date.fromisoformat(text)
        except ValueError:
            return None
    return None


def _ccyymmdd(value: str | None) -> date | None:
    if value and re.fullmatch(r"\d{8}", value):
        try:
            return date(int(value[:4]), int(value[4:6]), int(value[6:]))
        except ValueError:
            return None
    return None


def _split_full_name(value: str) -> tuple[str, str]:
    """("First", "Last") from "Last, First" or "First Middle Last"."""
    if "," in value:
        last, first = (part.strip() for part in value.split(",", 1))
        return first, last
    tokens = value.split()
    if len(tokens) < 2:
        return "", value.strip()
    return " ".join(tokens[:-1]), tokens[-1]


def embeds_patient_id(claim_number: str, ids: list[str]) -> bool:
    """A claim number that is, or carries, one of these patient ids: as a part between separators,
    or (six characters or more) at its start or end ("ACC100001-3", "1000013")."""
    key = alnum_key(claim_number)
    if not key:
        return False
    parts = {alnum_key(part) for part in re.split(r"[^0-9A-Za-z]+", claim_number) if part}
    for value in ids:
        pid = alnum_key(value)
        if len(pid) < 3:
            continue
        if pid == key or pid in parts:
            return True
        if len(pid) >= 6 and (key.startswith(pid) or key.endswith(pid)):
            return True
    return False


# --------------------------------------------------------------------------------------
# The first pass
# --------------------------------------------------------------------------------------


@dataclass
class _Index:
    """What the first pass learns besides the originals (kept in the key as ``index``)."""

    ccn_accounts: dict[str, str] = field(default_factory=dict)  # value_key(claim number) -> account
    member_accounts: dict[str, set[str]] = field(default_factory=dict)  # value_key(member id) -> accounts
    keep_numbers: set[str] = field(default_factory=set)  # provider, payer, clinic and claim numbers

    def account_for(self, control_number: str) -> str | None:
        """The account of a claim number, or of the claim it extends (``CLM000123-R1``)."""
        key = value_key(control_number)
        if not key:
            return None
        account = self.ccn_accounts.get(key)
        if account is None:
            for cut in range(len(key) - 1, 0, -1):
                if not key[cut].isalnum() and key[:cut] in self.ccn_accounts:
                    return self.ccn_accounts[key[:cut]]
        return account

    def to_document(self) -> dict[str, Any]:
        return {
            "claims": dict(self.ccn_accounts),
            "members": {member: sorted(accounts) for member, accounts in self.member_accounts.items()},
            "keep_numbers": sorted(self.keep_numbers),
        }

    def add_document(self, document: dict[str, Any]) -> None:
        """What an earlier batch learned, from its key; this batch's own claim entries win."""
        for control_number, account in (document.get("claims") or {}).items():
            if control_number:
                self.ccn_accounts.setdefault(control_number, account)
        for member, accounts in (document.get("members") or {}).items():
            self.member_accounts.setdefault(member, set()).update(accounts)
        self.keep_numbers.update(document.get("keep_numbers") or [])


@dataclass
class _Context:
    """What the first pass learns for the free text, the sweep and the checks."""

    data_dates: set[date] = field(default_factory=set)
    codes: set[str] = field(default_factory=set)
    texts: list[str] = field(default_factory=list)
    providers: set[str] = field(default_factory=set)  # people: rendering providers, staff authors, NM1*82
    organizations: set[str] = field(default_factory=set)  # payers, the clinic, other NM1 / N1 names
    provider_authors: set[str] = field(default_factory=set)
    claim_numbers: set[str] = field(default_factory=set)  # as written, to keep when not replaced
    billing_npis: set[str] = field(default_factory=set)
    tax_ids: set[str] = field(default_factory=set)
    embedded_claim_numbers: int = 0
    corpus: Counter[str] = field(default_factory=Counter)


def _keep(index: _Index, value: str | None) -> None:
    digits = phone_digits(value or "")
    if len(digits) >= 7:
        index.keep_numbers.add(digits)


def _person_pairs(rows: list[tuple[str, str]]) -> list[tuple[frozenset[str], frozenset[str]]]:
    pairs: list[tuple[frozenset[str], frozenset[str]]] = []
    for first, last in rows:
        first_keys = frozenset(k for k in (name_key(t) for t in NAME_TOKEN_RE.findall(first)) if len(k) > 1)
        last_keys = frozenset(k for k in (name_key(t) for t in NAME_TOKEN_RE.findall(last)) if len(k) > 1)
        if first_keys and last_keys:
            pairs.append((first_keys, last_keys))
    return pairs


def _collect(csv_files: list[_CsvFile], remits: list[_RemitFile]) -> tuple[Originals, _Index, _Context]:
    originals = Originals()
    index = _Index()
    context = _Context()
    people: list[tuple[str, str]] = []
    for csv_file in csv_files:
        if csv_file.kind != CLAIMS:
            for _row_number, row, _extras in csv_file.rows:
                for column in DATE_FIELDS[csv_file.kind]:
                    parsed = _maybe_date(row[column])
                    if parsed is not None:
                        context.data_dates.add(parsed)
                context.texts.extend(row[column] for column in TEXT_FIELDS[csv_file.kind])
                context.claim_numbers.add(row["claim_control_number"].strip())
            continue
        for row_number, row, extras in csv_file.rows:
            account = row["patient_account_number"].strip()
            originals.add_name(row["patient_first_name"])
            originals.add_name(row["patient_last_name"])
            people.append((row["patient_first_name"], row["patient_last_name"]))
            originals.add_patient_id(account)
            originals.add_patient_id(row["member_id"])
            dob = _parse_date(csv_file.label, row_number, "patient_dob", row["patient_dob"])
            if dob is not None:
                originals.dobs.add(dob)
            originals.add_address(row["patient_address_line1"], row["patient_city"], row["patient_zip"])
            for column in CLAIM_NUMBER_FIELDS:
                originals.add_claim_number(row[column])
                _keep(index, row[column])
                if row[column].strip():
                    context.claim_numbers.add(row[column].strip())
                    if embeds_patient_id(row[column], [account, row["member_id"]]):
                        context.embedded_claim_numbers += 1
            ccn = value_key(row["claim_control_number"])
            if ccn:
                index.ccn_accounts.setdefault(ccn, account)
            if row["member_id"].strip():
                index.member_accounts.setdefault(value_key(row["member_id"]), set()).add(account)
            for column in (*NPI_FIELDS, "prior_auth_number"):
                _keep(index, row[column])
            if row["billing_provider_npi"].strip():
                context.billing_npis.add(row["billing_provider_npi"].strip())
            for column in DATE_FIELDS[CLAIMS]:
                parsed = _maybe_date(row[column])
                if parsed is not None:
                    context.data_dates.add(parsed)
            context.codes.add(row["procedure_code"].strip())
            for column in ("modifiers", "diagnosis_codes"):
                context.codes.update(part.strip() for part in row[column].split("|") if part.strip())
            if row["rendering_provider_name"].strip():
                context.providers.add(row["rendering_provider_name"].strip())
                originals.add_provider(row["rendering_provider_name"])
            if row["payer_name"].strip():
                context.organizations.add(row["payer_name"].strip())
                originals.add_provider(row["payer_name"])
            for column, value in extras.items():
                role, _person = csv_file.extra_roles[column]
                if role == "name":
                    originals.add_name(value)
                else:
                    originals.add_patient_id(value)
    for remit in remits:
        _collect_remit(remit, originals, index, context, people)
    pairs = _person_pairs(people)
    for csv_file in csv_files:
        if csv_file.kind != NOTES:
            continue
        for _row_number, row, _extras in csv_file.rows:
            author = row["author"].strip()
            if not author:
                continue
            tokens = {name_key(t) for t in NAME_TOKEN_RE.findall(author)}
            if not any(first <= tokens and last <= tokens for first, last in pairs):
                context.provider_authors.add(author)
                context.providers.add(author)
                originals.add_provider(author)
    context.corpus = lowercase_corpus(context.texts)
    context.codes.discard("")
    return originals, index, context


def _collect_remit(
    remit: _RemitFile, originals: Originals, index: _Index, context: _Context, people: list[tuple[str, str]]
) -> None:
    try:
        delimiters = read_delimiters(remit.text)
        segments = tokenize(remit.text, delimiters)
    except X12Error as exc:
        raise AnonymizeError(f"{remit.label}: not a readable X12 interchange") from exc
    loop_ids: list[str] = []
    loop_numbers: list[str] = []
    payee = False

    def close_loop() -> None:
        if any(embeds_patient_id(number, loop_ids) for number in loop_numbers):
            context.embedded_claim_numbers += 1
        loop_ids.clear()
        loop_numbers.clear()

    for seg in segments:
        sid, first = seg.id, seg.element(1)
        if sid in ("CLP", "LX", "PLB", "SE"):
            close_loop()
        if sid == "N1":
            payee = first == "PE"
            if seg.element(2):
                context.organizations.add(seg.element(2) or "")
                originals.add_provider(seg.element(2))
            if payee and seg.element(3) == "XX" and seg.element(4):
                context.billing_npis.add(seg.element(4) or "")
            _keep(index, seg.element(4))
        elif sid in ("LX", "CLP"):
            payee = False
        if sid == "NM1" and first in PERSON_ENTITIES:
            if seg.element(2) == "2":  # an employer named as the subscriber: never a pseudonym word
                originals.add_provider(seg.element(3))
            else:
                for position in (3, 4, 5):
                    originals.add_name(seg.element(position))
                people.append((seg.element(4) or "", seg.element(3) or ""))
            originals.add_patient_id(seg.element(9))
            if seg.element(9):
                loop_ids.append(seg.element(9) or "")
        elif sid == "NM1":
            name = " ".join(part for part in (seg.element(4), seg.element(5), seg.element(3)) if part)
            if name:
                (context.providers if seg.element(2) == "1" else context.organizations).add(name)
                originals.add_provider(name)
            _keep(index, seg.element(9))
        elif sid == "REF" and first in PATIENT_REF_QUALIFIERS:
            originals.add_patient_id(seg.element(2))
            if seg.element(2):
                loop_ids.append(seg.element(2) or "")
        elif sid == "REF" and first == "6R":
            originals.add_claim_number(seg.element(2))
            loop_numbers.append(seg.element(2) or "")
        elif sid == "REF" and first == "F8":
            originals.add_payer_claim_number(seg.element(2))
            loop_numbers.append(seg.element(2) or "")
        elif sid == "REF":
            _keep(index, seg.element(2))  # tax id, receiver id, payer id
            if payee and first == "TJ" and seg.element(2):
                context.tax_ids.add(seg.element(2) or "")
        elif sid == "CLP":
            originals.add_claim_number(seg.element(1))
            originals.add_payer_claim_number(seg.element(7))
            _keep(index, seg.element(1))
            _keep(index, seg.element(7))
            loop_numbers.extend(value for value in (seg.element(1), seg.element(7)) if value)
            if seg.element(1):
                context.claim_numbers.add(seg.element(1) or "")
        elif sid == "PER":
            for position in (4, 6, 8):
                _keep(index, seg.element(position))  # the payer's phone and fax
        elif sid == "TRN":
            _keep(index, seg.element(3))
        elif sid == "PLB":
            _keep(index, seg.element(1))
            for position in (3, 5, 7, 9, 11, 13):
                parts = (seg.element(position) or "").split(delimiters.sub_element)
                if len(parts) > 1:
                    originals.add_patient_id(plb_hic(parts[0], parts[1].strip()) or None)
        elif sid == "SVC":
            parts = (seg.element(1) or "").split(delimiters.sub_element)
            context.codes.update(part.strip() for part in parts[1:] if part.strip())
        if sid == "BPR":
            found = _ccyymmdd(seg.element(16))
            if found is not None:
                context.data_dates.add(found)
        elif sid == "DTM":
            for value in (seg.element(2), *(seg.element(6) or "").split("-")):
                found = _ccyymmdd(value)
                if found is not None:
                    context.data_dates.add(found)
    close_loop()


# --------------------------------------------------------------------------------------
# The run
# --------------------------------------------------------------------------------------


class _Run:
    """One anonymize run over files already read (see the module docstring)."""

    def __init__(
        self,
        options: AnonymizeOptions,
        originals: Originals,
        index: _Index,
        context: _Context,
        previous: PseudonymKey | None = None,
    ) -> None:
        self.options = options
        self.index = index
        self.context = context
        seed = options.seed if options.test_only else None
        shift = previous.date_shift_days if previous is not None else options.shift_dates_days
        avoid = {value + timedelta(days=shift) for value in context.data_dates}
        if previous is not None:
            self.pseudo = Pseudonymizer.from_key(previous, originals, seed=seed, avoid_dates=avoid)
        else:
            self.pseudo = Pseudonymizer(
                originals,
                seed=seed,
                shift_days=options.shift_dates_days,
                replace_claim_numbers=options.replace_claim_numbers,
                avoid_dates=avoid,
            )
        self.restored = len(self.pseudo.subjects)
        self.touched: dict[str, None] = {}  # subjects this batch mentions, in the order met
        self.text: TextIndex | None = None
        self.counts: dict[str, int] = {
            "claim_rows": 0,
            "note_rows": 0,
            "document_rows": 0,
            "rows_dropped_unknown_claim": 0,
            "rows_dropped_blank_claim": 0,
            "columns_dropped": 0,
            "identity_values_harvested": 0,
            "text_identifiers_replaced": 0,
            "text_other_patient_mentions": 0,
            "provider_authors_kept": 0,
            "dates_shifted": 0,
            "remittance_claims": 0,
            "remittance_names": 0,
            "remittance_ids": 0,
            "plb_references_replaced": 0,
            "claim_numbers_replaced": 0,
            "residual_masked": 0,
        }
        self.residual: list[ResidualFinding] = []

    # -- claim export -------------------------------------------------------------------------

    def _shift_column(self, label: str, row_number: int, column: str, value: str) -> str:
        parsed = _parse_date(label, row_number, column, value)
        if parsed is None:
            return value
        self.counts["dates_shifted"] += 1
        return self.pseudo.shift(parsed).isoformat()

    def _harvest(
        self, csv_file: _CsvFile, subject: Subject, row: dict[str, str], extras: dict[str, str]
    ) -> None:
        """Map the export's identity columns the contract drops, so free text loses them too."""
        people: dict[str, dict[str, str]] = {}
        for column, value in extras.items():
            role, person_role = csv_file.extra_roles[column]
            self.counts["identity_values_harvested"] += 1
            if role == "id":
                self.pseudo.patient_id(value, subject)
                continue
            person, part = person_role.split(":", 1)
            people.setdefault(person, {})[part] = value
        for person, parts in people.items():
            if person == "patient":
                subject.person(row["patient_first_name"], row["patient_last_name"], parts.get("middle", ""))
                continue
            first, last = parts.get(GIVEN, ""), parts.get(FAMILY, "")
            if "full" in parts and not (first and last):
                first, last = _split_full_name(parts["full"])
            subject.person(first, last, parts.get("middle", ""))

    def claim_row(
        self, csv_file: _CsvFile, row_number: int, row: dict[str, str], extras: dict[str, str]
    ) -> dict[str, str]:
        pseudo = self.pseudo
        out = dict(row)
        account = row["patient_account_number"].strip()
        subject = pseudo.subjects.get(account)
        if subject is None:
            subject = pseudo.subject(account, kind=PATIENT)
            subject.seed(row["patient_first_name"], row["patient_last_name"])
        self.touched[subject.key] = None
        first, last, _middle = subject.person(row["patient_first_name"], row["patient_last_name"])
        out["patient_first_name"], out["patient_last_name"] = first, last
        out["patient_account_number"] = pseudo.patient_id(row["patient_account_number"], subject)
        out["member_id"] = pseudo.patient_id(row["member_id"], subject)
        dob = _parse_date(csv_file.label, row_number, "patient_dob", row["patient_dob"])
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
                out[column] = self._shift_column(csv_file.label, row_number, column, row[column])
        self._harvest(csv_file, subject, row, extras)
        self.counts["claim_rows"] += 1
        return out

    # -- 835 ------------------------------------------------------------------------------------

    def resolve(self, control_number: str, member_ids: list[str], label: str) -> Subject:
        """The subject of a claim loop: the patient whose claim CLP01 names (or extends, as a
        corrected claim's ``-R1`` number does) or whom exactly one of the loop's member ids names,
        in this batch's export or an earlier one known from the key; else a remittance subject."""
        account = self.index.account_for(control_number) if control_number else None
        if account is None:
            candidates: set[str] = set()
            for member_id in member_ids:
                candidates |= self.index.member_accounts.get(value_key(member_id), set())
            if len(candidates) == 1:
                account = next(iter(candidates))
        if account is not None and account in self.pseudo.subjects:
            subject = self.pseudo.subjects[account]
        else:
            key = "remittance:" + (member_ids[0] if member_ids else label)
            subject = self.pseudo.subject(key, kind=REMITTANCE)
        self.touched[subject.key] = None
        return subject

    def remit(self, remit: _RemitFile, output_name: str) -> tuple[RemitRewriter, PendingRemit]:
        rewriter = RemitRewriter(self.pseudo, self.resolve, output_name)
        return rewriter, rewriter.rewrite(remit.text)

    def finish_remit(
        self, remit: _RemitFile, output_name: str, rewriter: RemitRewriter, pending: PendingRemit
    ) -> tuple[str, dict[str, Any]]:
        result = rewriter.finish(pending)
        verify_835(output_name, remit.text, result.text, self.pseudo)
        counts = result.counts
        self.counts["remittance_claims"] += counts.claims
        self.counts["remittance_names"] += counts.names
        self.counts["remittance_ids"] += counts.ids
        self.counts["plb_references_replaced"] += counts.plb_references
        self.counts["claim_numbers_replaced"] += counts.claim_numbers
        self.counts["dates_shifted"] += counts.dates_shifted
        return result.text, {
            "transactions": counts.transactions,
            "claims": counts.claims,
            "segments": counts.segments,
        }

    # -- notes and payer documents ----------------------------------------------------------

    def build_text_index(self) -> TextIndex:
        """Every patient of the batch and of the key, every provider and organization name."""
        context = self.context
        index = TextIndex(
            keep_numbers=frozenset(self.index.keep_numbers),
            data_dates=frozenset(context.data_dates),
            codes=frozenset(context.codes),
            corpus=context.corpus,
        )
        pseudo = self.pseudo
        for subject in pseudo.subjects.values():
            index.add_subject(subject)
        for original, pseudonym in pseudo.patient_ids.items():
            index.add_id(original, pseudonym, "")
        if pseudo.replace_claim_numbers:
            for value_map in (pseudo.claim_numbers, pseudo.payer_claim_numbers):
                for original, pseudonym in value_map.items():
                    index.add_claim_number(original, pseudonym)
        else:
            for number in context.claim_numbers:
                index.add_keep_value(number)
        for name in context.providers:
            index.add_keep_name(name, person=True)
        for name in context.organizations:
            index.add_keep_name(name, person=False)
        self.text = index
        return index

    def text_row(
        self, csv_file: _CsvFile, output_name: str, row_number: int, row: dict[str, str]
    ) -> dict[str, str] | None:
        if not row["claim_control_number"].strip():
            self.counts["rows_dropped_blank_claim"] += 1
            return None
        account = self.index.account_for(row["claim_control_number"])
        subject = self.pseudo.subjects.get(account) if account is not None else None
        if subject is None:
            self.counts["rows_dropped_unknown_claim"] += 1
            return None
        assert self.text is not None
        self.touched[subject.key] = None
        out = dict(row)
        out["claim_control_number"] = self.pseudo.claim_number(row["claim_control_number"], subject)
        if self.pseudo.shift_days:
            for column in DATE_FIELDS[csv_file.kind]:
                out[column] = self._shift_column(csv_file.label, row_number, column, row[column])
        for column in TEXT_FIELDS[csv_file.kind]:
            if column == "author" and row[column].strip() in self.context.provider_authors:
                self.counts["provider_authors_kept"] += 1
                continue  # the clinic's staff: kept as written
            result = self.text.process(
                row[column],
                owner=subject.key,
                shift_days=self.pseudo.shift_days,
                allow_residual=self.options.allow_residual,
            )
            out[column] = result.text
            self.counts["text_identifiers_replaced"] += result.replaced
            self.counts["text_other_patient_mentions"] += result.other_patients
            self.counts["dates_shifted"] += result.dates_shifted
            self.counts["residual_masked"] += result.masked
            # Once per kind and field: two nets often see the same phone number.
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


def _output_names(run: int, files: list[tuple[str, str]]) -> dict[str, str]:
    """Input file name -> ``pseudonymized_run<N>_<kind>_<n>.<ext>``, numbered per kind in input order."""
    counters: dict[str, int] = {}
    names: dict[str, str] = {}
    for name, kind in files:
        label, extension = OUTPUT_KINDS[kind]
        counters[label] = counters.get(label, 0) + 1
        names[name] = pseudonymized.batch_file_name(run, label, counters[label], extension)
    return names


def restore_claim_row(row: dict[str, str], key: PseudonymKey) -> dict[str, str]:
    """A claim-export row of a pseudonymized batch with every replaced value put back from the key.

    Every non-empty patient-block value must map back, and to the patient the account number names
    (a later batch's pseudonym, kept by an older key, is refused rather than copied onto a claim);
    so must every claim number when the key replaced them. Refusals name the column, never a value.
    """
    out = dict(row)
    subject = (
        key.subject_for_account(row["patient_account_number"]) if row["patient_account_number"] else None
    )
    if subject is None:
        raise AnonymizeError("patient_account_number: not a pseudonym of this key")
    ids = {value_key(value) for value in (subject.get("ids") or {})}
    records = subject.get("addresses") or []
    out["patient_account_number"] = key.original("patient_id", row["patient_account_number"]) or ""
    if row["member_id"]:
        original = key.original("patient_id", row["member_id"])
        if original is None or value_key(original) not in ids:
            raise AnonymizeError("member_id: not a pseudonym of this patient in this key")
        out["member_id"] = original
    for column, family, record_field in (
        ("patient_address_line1", "address_line", "line1"),
        ("patient_city", "city", "city"),
        ("patient_zip", "zip", "zip"),
    ):
        if row[column]:
            original = key.original(family, row[column])
            if original is None or not any(
                value_key(r.get(record_field) or "") == value_key(original) for r in records
            ):
                raise AnonymizeError(f"{column}: not a pseudonym of this patient in this key")
            out[column] = original
    for column in CLAIM_NUMBER_FIELDS:  # a corrected claim's <pseudonym>-R1 included
        if row[column]:
            original = key.original_claim_number(row[column])
            if original is None and key.replace_claim_numbers:
                raise AnonymizeError(f"{column}: not a claim number this key replaced")
            out[column] = original or row[column]
    for column, role in (("patient_first_name", GIVEN), ("patient_last_name", FAMILY)):
        if row[column]:
            original = key.original_name(subject, role, row[column])
            if original is None:
                raise AnonymizeError(f"{column}: not a pseudonym of this patient in this key")
            out[column] = original
    if row["patient_dob"]:
        try:
            pseudonym = date.fromisoformat(row["patient_dob"])
        except ValueError as exc:
            raise AnonymizeError("patient_dob: not a date") from exc
        original_dob = key.original_dob(subject, pseudonym)
        if original_dob is None:
            raise AnonymizeError("patient_dob: not a pseudonym of this patient in this key")
        out["patient_dob"] = original_dob.isoformat()
    if key.date_shift_days:
        for column in DATE_FIELDS[CLAIMS]:
            if row[column]:
                out[column] = key.unshift(date.fromisoformat(row[column])).isoformat()
    return out


def _check_key(
    previous: PseudonymKey, options: AnonymizeOptions, context: _Context, key_in: Path, key: Path
) -> None:
    """A reused key keeps the options that shaped its maps, belongs to this clinic and is the newest."""
    if previous.replace_claim_numbers != options.replace_claim_numbers:
        how = "with" if previous.replace_claim_numbers else "without"
        raise AnonymizeError(
            f"--key-in was made {how} --replace-claim-numbers; run {how} it too (a reused key keeps its options)"
        )
    if previous.date_shift_days != options.shift_dates_days:
        raise AnonymizeError(
            "--key-in was made with another --shift-dates value; pass the value the key was made with "
            "(a reused key keeps its options)"
        )
    if previous.test_only != options.test_only:
        message = (
            "--key-in was made with --test-only; a real batch never extends a test key"
            if previous.test_only
            else "--key-in is a real key; a --test-only run never extends it"
        )
        raise AnonymizeError(message)
    if previous.billing_npis and context.billing_npis and not previous.billing_npis & context.billing_npis:
        raise AnonymizeError(
            "--key-in belongs to another clinic: none of this batch's billing NPIs is the key's; use this "
            "clinic's newest key"
        )
    if not (previous.billing_npis and context.billing_npis) and previous.tax_ids and context.tax_ids:
        if not previous.tax_ids & context.tax_ids:
            raise AnonymizeError(
                "--key-in belongs to another clinic: no tax id in common; use this clinic's newest key"
            )
    newer = paths.newer_runs([key_in.parent, key.parent], previous.chain_id, previous.run_number)
    if newer:
        names = ", ".join(sorted({str(entry.get("key") or "?") for entry in newer}))
        raise AnonymizeError(
            f"--key-in is not the newest key of its chain: the run log next to it shows a later run ({names}); "
            "pass the newest key, which holds every earlier one"
        )


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def anonymize_batch(
    in_dir: Path,
    out_dir: Path,
    key_path: Path,
    options: AnonymizeOptions | None = None,
    *,
    project_root: Path = PROJECT_ROOT,
) -> AnonymizeResult:
    """Pseudonymize one raw batch (see the module docstring). Writes nothing when residual
    identifiers were found (and ``allow_residual`` is off) or the leak sweep found an original;
    ``AnonymizeResult.written`` says which."""
    options = options or AnonymizeOptions()
    if (options.seed is not None or options.shift_dates_days) and not options.test_only:
        raise AnonymizeError(
            "--seed and --shift-dates are for synthetic test data only: add --test-only, and never use them "
            "on a real batch (a seed makes the offsets of the dates of birth replayable, and the app compares "
            "deadlines and outcomes with the real today)"
        )
    if abs(options.shift_dates_days) > MAX_SHIFT_DAYS:
        raise AnonymizeError(f"--shift-dates must be between -{MAX_SHIFT_DAYS} and {MAX_SHIFT_DAYS} days")
    source, out, key, reused = check_paths(
        in_dir, out_dir, key_path, key_in=options.key_in, project_root=project_root
    )
    previous = load_key(reused) if reused is not None else None
    csv_files, remits, skipped, cp1252 = _discover(source)
    if previous is None and not any(f.kind == CLAIMS for f in csv_files):
        raise AnonymizeError(
            "no claim export found in the input folder (a CSV whose header carries every claim-export "
            "column); a batch without one needs --key-in, the newest key of the clinic's chain"
        )
    originals, index, context = _collect(csv_files, remits)
    if previous is not None and reused is not None:
        _check_key(previous, options, context, reused, key)
        index.add_document(previous.index)
    if context.embedded_claim_numbers and not options.replace_claim_numbers:
        raise AnonymizeError(
            f"{context.embedded_claim_numbers} claim number(s) carry a patient's account number or member id "
            "(a claim numbered after the account, say); claim numbers would keep it, so re-run with "
            "--replace-claim-numbers (recommended for every real batch)"
        )
    run_number = previous.run_number + 1 if previous is not None else 1
    if previous is not None:
        chain_id = previous.chain_id
    elif options.test_only and options.seed is not None:  # a reproducible test run
        chain_id = _sha256(f"test-seed:{options.seed}".encode())[:32]
    else:
        chain_id = uuid.uuid4().hex
    chain = {
        "id": chain_id,
        "run": run_number,
        "parent_sha256": _sha256(reused.read_bytes()) if reused is not None else None,
    }
    run = _Run(options, originals, index, context, previous)
    kinds = [(f.name, f.kind) for f in sorted(csv_files, key=lambda f: f.position)]
    kinds += [(r.name, REMIT) for r in remits]
    names = _output_names(run_number, kinds)

    outputs: dict[str, bytes] = {}
    files: list[dict[str, Any]] = []
    for csv_file in (f for f in csv_files if f.kind == CLAIMS):
        rows = [run.claim_row(csv_file, number, row, extras) for number, row, extras in csv_file.rows]
        outputs[names[csv_file.name]] = _render_csv(CLAIMS, rows)
        run.counts["columns_dropped"] += csv_file.dropped_columns
        files.append({"file": names[csv_file.name], "kind": CLAIMS, "rows": len(rows)})
    pending = [(remit, *run.remit(remit, names[remit.name])) for remit in remits]
    for remit, rewriter, state in pending:  # PLB references once every file has been read
        text, counts = run.finish_remit(remit, names[remit.name], rewriter, state)
        outputs[names[remit.name]] = text.encode("utf-8")
        files.append({"file": names[remit.name], "kind": REMIT, **counts})
    run.build_text_index()
    for csv_file in (f for f in csv_files if f.kind != CLAIMS):
        output_name = names[csv_file.name]
        rows = [
            out_row
            for number, row, _extras in csv_file.rows
            if (out_row := run.text_row(csv_file, output_name, number, row)) is not None
        ]
        outputs[output_name] = _render_csv(csv_file.kind, rows)
        run.counts["columns_dropped"] += csv_file.dropped_columns
        files.append({"file": output_name, "kind": csv_file.kind, "rows": len(rows)})

    pseudo = run.pseudo
    touched = [pseudo.subjects[subject_key] for subject_key in run.touched]
    patients = sum(1 for subject in touched if subject.kind == PATIENT)
    run.counts["remittance_subjects"] = sum(1 for subject in touched if subject.kind == REMITTANCE)
    run.counts["subjects_from_key_in"] = run.restored
    run.counts["subjects_in_key"] = len(pseudo.subjects)
    run.counts["duplicate_name_pairs"] = pseudo.duplicate_name_pairs
    run.counts["files_read_as_windows_1252"] = cp1252
    run.counts["residual"] = len(run.residual)
    result = AnonymizeResult(
        out_dir=out,
        key_path=key,
        written=False,
        options=options,
        key_in=reused,
        chain={"id": chain["id"], "run": run_number},
        files=sorted(files, key=lambda f: f["file"]),
        skipped_files=skipped,
        patients=patients,
        counts=run.counts,
        residual=run.residual,
    )
    if run.residual and not options.allow_residual:
        log.warning(
            "anonymize stopped: %d residual finding(s) in free text; nothing was written", len(run.residual)
        )
        return result

    run_entry: dict[str, Any] = {
        "run": run_number,
        "allow_residual": options.allow_residual,
        "files": names,
        "skipped": skipped,
    }
    if options.test_only:
        run_entry["seed"] = options.seed
    runs = [*(previous.runs if previous is not None else []), run_entry]
    clinic = {
        "billing_npis": sorted(
            context.billing_npis | (previous.billing_npis if previous is not None else set())
        ),
        "tax_ids": sorted(context.tax_ids | (previous.tax_ids if previous is not None else set())),
    }
    document = pseudo.key_document(
        runs=runs, index=run.index.to_document(), chain=chain, clinic=clinic, test_only=options.test_only
    )
    shift = timedelta(days=pseudo.shift_days)
    sweep = Sweep(
        document,
        data_dates=frozenset(context.data_dates | {value + shift for value in context.data_dates}),
        codes=frozenset(context.codes),
        corpus=context.corpus,
        provider_names=frozenset(context.providers),
        organization_names=frozenset(context.organizations),
    )
    output_kinds = {name: ("835" if name.endswith(".835") else "csv") for name in outputs}
    result.leaks = sweep_outputs(outputs, output_kinds, sweep)
    run.counts["leaks"] = len(result.leaks)
    run.counts["pseudonym_name_collisions"] = len(sweep.collisions)
    if result.leaks:
        log.warning(
            "anonymize stopped: the leak sweep found %d original value(s); nothing was written",
            len(result.leaks),
        )
        return result

    key_bytes = (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    publication = paths.Publication()
    try:
        staged_out = publication.stage_folder(out, outputs)
        staged_key = publication.stage_file(key, key_bytes)
        result.key_acl = paths.restrict_to_owner(staged_key)
        publication.publish(staged_out, out)
        publication.publish(staged_key, key)  # the key last
    except BaseException:
        publication.roll_back()
        raise
    result.written = True
    paths.append_run_log(
        key.parent,
        {
            "command": "anonymize",
            "chain": chain["id"],
            "run": run_number,
            "key": key.name,
            "key_sha256": _sha256(key_bytes),
            "parent_sha256": chain["parent_sha256"],
            "test_only": options.test_only,
            "inputs": len(csv_files) + len(remits),
            "skipped": len(skipped),
            "outputs": {name: _sha256(data) for name, data in sorted(outputs.items())},
            "patients": patients,
            "residual_masked": run.counts["residual_masked"],
        },
    )
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
    "embeds_patient_id",
    "restore_claim_row",
]
