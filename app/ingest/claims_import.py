"""Claim-export importer and its companions (docs/SPEC.md sections 4.1 and 6).

``import_claims_csv`` upserts payers (by payer_id), patients (by clinic + account number) and
claims with their lines (by clinic + claim control number; lines replaced by line number, never
duplicated; a line the re-export no longer carries is dropped, and the remittance lines of an
updated claim are re-linked so a renumbered or trimmed export never leaves one pointing at the
wrong procedure). Payer windows (timely filing, corrected claim, appeal days) are set from the
settings defaults when a payer is first seen and never overwritten by a later import: an operator
sets them with ``app.ingest.payers.set_payer_windows``. ``import_notes_csv`` and
``import_payer_documents_csv`` attach note excerpts and payer documents to existing claims; both
are untrusted text and every row is scanned for planted instructions on the way in
(``flagged_injection`` / ``injection_findings`` on the stored row, ``flagged`` in the counts).

Every importer: hashes the whole file (a re-import is a ``duplicate`` batch and changes nothing;
a notes or documents file whose earlier import skipped rows for claims not yet known may be
imported again, row-level de-duplication keeping it from attaching anything twice), validates
every row before writing anything (an invalid file is a ``failed`` batch whose error lists row
numbers and field names only; a value longer than its database column is rejected the same way,
so SQLite and Postgres behave alike), flushes as it goes and leaves the commit to the caller.
After a claims import, remittance claims still unmatched (an 835 imported first) are matched.

A claim export that carries the ``pseudonymized_batch`` column ``denial-agent anonymize`` writes is a
pseudonymized batch (docs/SPEC.md section 16, ``app.review.pseudonymized``): every claim of the import
is marked in ``Claim.extra``, the mark survives a later import of the claim without the column, and
the export marks every package of such a claim. Nothing else about the import changes: the column is
left out of the rows like any other column the contract does not name.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
from collections.abc import Iterable, Mapping
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.enums import ClaimStatus, DocumentType, ImportKind, ImportStatus
from app.ingest import (
    begin_batch,
    complete_batch,
    duplicate_batch,
    find_completed_batch,
    read_source,
    sha256_hex,
)
from app.ingest.remit_import import relink_remittance_lines, rematch_unmatched
from app.models import (
    Claim,
    ClaimLine,
    ImportBatch,
    NoteExcerpt,
    Patient,
    Payer,
    PayerDocument,
    RemittanceLine,
)
from app.redact.injection import detect_injection
from app.review import pseudonymized
from app.schemas import CLAIM_EXPORT_COLUMNS, ClaimExportRow, NoteRow, PayerDocumentRow

log = logging.getLogger(__name__)

NOTE_COLUMNS: tuple[str, ...] = ("claim_control_number", "note_date", "author", "text")
PAYER_DOCUMENT_COLUMNS: tuple[str, ...] = ("claim_control_number", "document_type", "received_date", "text")

CLAIM_COUNT_KEYS: tuple[str, ...] = (
    "claims_created",
    "claims_updated",
    "lines",
    "patients_created",
    "payers_created",
    "warnings",
    "remittance_claims_matched",
    "remittance_lines_relinked",
)

_TOKEN_LENGTHS = (6, 8, 10, 12, 16, 24, 32)
_DOCUMENT_TYPES = frozenset(str(t) for t in DocumentType)


def _column_limits(columns: Mapping[str, Any]) -> dict[str, int]:
    """CSV field -> width of the VARCHAR column it is written to (taken from the models, never typed)."""
    return {field: column.type.length for field, column in columns.items() if column.type.length}


_claims_c, _patients_c, _payers_c, _lines_c = (
    Claim.__table__.c,
    Patient.__table__.c,
    Payer.__table__.c,
    ClaimLine.__table__.c,
)
CLAIM_FIELD_LIMITS: dict[str, int] = _column_limits(
    {
        "claim_control_number": _claims_c.claim_control_number,
        "original_claim_control_number": _claims_c.claim_control_number,
        "billing_provider_npi": _claims_c.billing_provider_npi,
        "rendering_provider_npi": _claims_c.rendering_provider_npi,
        "rendering_provider_name": _claims_c.rendering_provider_name,
        "referring_provider_npi": _claims_c.referring_provider_npi,
        "place_of_service": _claims_c.place_of_service,
        "prior_auth_number": _claims_c.prior_auth_number,
        "patient_account_number": _patients_c.account_number,
        "patient_first_name": _patients_c.first_name,
        "patient_last_name": _patients_c.last_name,
        "patient_address_line1": _patients_c.address_line1,
        "patient_city": _patients_c.city,
        "patient_state": _patients_c.state,
        "patient_zip": _patients_c.zip,
        "member_id": _patients_c.member_id,
        "payer_id": _payers_c.payer_id,
        "payer_name": _payers_c.name,
        "procedure_code": _lines_c.procedure_code,
        "ndc": _lines_c.ndc,
        "line_item_control_number": _lines_c.line_item_control_number,
    }
)
NOTE_FIELD_LIMITS: dict[str, int] = _column_limits({"author": NoteExcerpt.__table__.c.author})


# --------------------------------------------------------------------------------------
# CSV reading and validation (no database access)
# --------------------------------------------------------------------------------------


def _read_rows[RowModel: BaseModel](
    text: str,
    model: type[RowModel],
    required_columns: Iterable[str],
    limits: Mapping[str, int] | None = None,
) -> tuple[list[tuple[int, RowModel]], list[str], int]:
    """Parse and validate every row. Returns (rows as (row_number, model), errors, total rows).

    Row numbers follow the spreadsheet convention: the header is row 1, the first data row is 2.
    ``limits`` maps field names to their database column width; a longer value is an error (SQLite
    would store it, Postgres would abort the import). Error strings carry row numbers and field
    names only, never values.
    """
    reader = csv.DictReader(io.StringIO(text))
    rows: list[tuple[int, RowModel]] = []
    errors: list[str] = []
    total = 0
    try:
        fieldnames = [name.strip() for name in (reader.fieldnames or [])]
        reader.fieldnames = fieldnames
        missing = [column for column in required_columns if column not in fieldnames]
        if missing:
            return [], [f"missing columns: {', '.join(missing)}"], 0
        for row_number, raw in enumerate(reader, start=2):
            data = {key: value for key, value in raw.items() if key is not None}
            if all(value is None or not str(value).strip() for value in data.values()):
                continue  # blank line
            total += 1
            if "units" in data and not str(data["units"] or "").strip():
                data["units"] = "1"  # a blank unit count means one unit
            try:
                row = model.model_validate(data)
            except ValidationError as exc:
                fields = sorted({".".join(str(part) for part in err["loc"]) or "row" for err in exc.errors()})
                errors.append(f"row {row_number} ({', '.join(fields)})")
                continue
            too_long = _too_long(row, limits or {})
            if too_long:
                errors.append(f"row {row_number} ({', '.join(too_long)}: longer than the field allows)")
                continue
            rows.append((row_number, row))
    except csv.Error:  # oversized field, stray quoting ...; the library message may quote the data
        errors.append(f"csv error near line {reader.line_num}")
    return rows, errors, total


def _too_long(row: BaseModel, limits: Mapping[str, int]) -> list[str]:
    """Fields whose value would not fit its database column."""
    return sorted(
        field
        for field, limit in limits.items()
        if isinstance((value := getattr(row, field, None)), str) and len(value) > limit
    )


def _header(text: str) -> list[str]:
    """The stripped column names of the first row (what ``_read_rows`` matched against)."""
    try:
        return [name.strip() for name in next(csv.reader(io.StringIO(text)), [])]
    except csv.Error:
        return []


def _check_claim_structure(rows: list[tuple[int, ClaimExportRow]]) -> list[str]:
    """Rows of one claim must be contiguous and must not repeat a line number."""
    errors: list[str] = []
    finished: set[str] = set()
    current: str | None = None
    seen_lines: set[tuple[str, int]] = set()
    for row_number, row in rows:
        ccn = row.claim_control_number
        if ccn != current:
            if current is not None:
                finished.add(current)
            if ccn in finished:
                errors.append(f"row {row_number} (claim rows not contiguous)")
            current = ccn
        key = (ccn, row.line_number)
        if key in seen_lines:
            errors.append(f"row {row_number} (duplicate line_number)")
        seen_lines.add(key)
    return errors


def _invalid_error(errors: list[str]) -> str:
    if len(errors) == 1 and errors[0].startswith("missing columns"):
        return errors[0]
    return "invalid rows: " + "; ".join(errors)


# --------------------------------------------------------------------------------------
# Patients, payers, claims
# --------------------------------------------------------------------------------------


def patient_token(clinic_id: int, account_number: str, length: int = 6) -> str:
    """Stable pseudonym: ``PT-`` + the first ``length`` hex chars (upper case) of sha256(clinic:account)."""
    digest = hashlib.sha256(f"{clinic_id}:{account_number}".encode()).hexdigest()
    return "PT-" + digest[:length].upper()


def _unique_patient_token(session: Session, clinic_id: int, account_number: str) -> str:
    for length in _TOKEN_LENGTHS:
        token = patient_token(clinic_id, account_number, length)
        taken = session.execute(select(Patient.id).where(Patient.token == token)).first()
        if taken is None:
            return token
    raise RuntimeError("could not derive a unique patient token")  # pragma: no cover - 32 hex chars


def _upsert_payer(
    session: Session, cache: dict[str, Payer], row: ClaimExportRow, counts: dict[str, int]
) -> Payer:
    payer = cache.get(row.payer_id)
    if payer is None:
        payer = session.execute(select(Payer).where(Payer.payer_id == row.payer_id)).scalars().first()
    if payer is None:
        settings = get_settings()
        payer = Payer(
            name=row.payer_name,
            payer_id=row.payer_id,
            timely_filing_days=settings.default_timely_filing_days,
            corrected_claim_days=settings.default_corrected_claim_days,
            appeal_days=settings.default_appeal_days,
        )
        session.add(payer)
        session.flush()
        counts["payers_created"] += 1
    elif row.payer_name and payer.name != row.payer_name:
        payer.name = row.payer_name
    cache[row.payer_id] = payer
    return payer


def _upsert_patient(
    session: Session, cache: dict[str, Patient], clinic_id: int, row: ClaimExportRow, counts: dict[str, int]
) -> Patient:
    account = row.patient_account_number
    patient = cache.get(account)
    if patient is None:
        stmt = select(Patient).where(Patient.clinic_id == clinic_id, Patient.account_number == account)
        patient = session.execute(stmt).scalars().first()
    created = patient is None
    if patient is None:
        patient = Patient(
            clinic_id=clinic_id,
            account_number=account,
            token=_unique_patient_token(session, clinic_id, account),
        )
    patient.first_name = row.patient_first_name
    patient.last_name = row.patient_last_name
    patient.dob = row.patient_dob
    patient.sex = row.patient_sex[:1].upper() if row.patient_sex else None
    patient.address_line1 = row.patient_address_line1
    patient.city = row.patient_city
    patient.state = row.patient_state
    patient.zip = row.patient_zip
    patient.member_id = row.member_id
    if created:
        session.add(patient)
        session.flush()
        counts["patients_created"] += 1
    cache[account] = patient
    return patient


def _find_claim(
    session: Session, cache: dict[str, Claim], clinic_id: int, control_number: str
) -> Claim | None:
    claim = cache.get(control_number)
    if claim is None:
        stmt = select(Claim).where(Claim.clinic_id == clinic_id, Claim.claim_control_number == control_number)
        claim = session.execute(stmt).scalars().first()
        if claim is not None:
            cache[control_number] = claim
    return claim


def _unlink_remittance_lines(session: Session, line: ClaimLine) -> int:
    """Detach every remittance line that points at ``line`` so the line can be dropped; returns how many."""
    if line.id is None:
        return 0
    stmt = select(RemittanceLine).where(RemittanceLine.claim_line_id == line.id)
    remittance_lines = list(session.execute(stmt).scalars())
    for remittance_line in remittance_lines:
        remittance_line.claim_line = None
        remittance_line.claim_line_id = None
    return len(remittance_lines)


def _apply_line(line: ClaimLine, row: ClaimExportRow) -> None:
    line.service_date = row.service_date
    line.procedure_code = row.procedure_code
    line.modifiers = list(row.modifiers)
    line.units = row.units
    line.charge_amount = row.charge_amount
    line.diagnosis_pointers = list(row.diagnosis_pointers)
    line.ndc = row.ndc
    line.line_item_control_number = row.line_item_control_number


def _upsert_claim(
    session: Session,
    *,
    clinic_id: int,
    batch: ImportBatch,
    group: list[ClaimExportRow],
    payer: Payer,
    patient: Patient,
    claims_cache: dict[str, Claim],
    counts: dict[str, int],
    pseudonymized_batch: bool = False,
) -> Claim:
    first = group[0]
    control_number = first.claim_control_number
    claim = _find_claim(session, claims_cache, clinic_id, control_number)
    created = claim is None
    if claim is None:
        claim = Claim(
            clinic_id=clinic_id, claim_control_number=control_number, status=str(ClaimStatus.SUBMITTED)
        )
        session.add(claim)

    if any(
        r.payer_id != first.payer_id or r.patient_account_number != first.patient_account_number
        for r in group
    ):
        counts["warnings"] += 1  # claim-level fields disagree between the rows of one claim; first row wins

    claim.payer = payer
    claim.patient = patient
    claim.frequency_code = first.claim_frequency_code[:1]
    claim.billing_provider_npi = first.billing_provider_npi
    claim.rendering_provider_npi = first.rendering_provider_npi
    claim.rendering_provider_name = first.rendering_provider_name
    claim.referring_provider_npi = first.referring_provider_npi
    claim.place_of_service = first.place_of_service
    claim.diagnosis_codes = list(first.diagnosis_codes)
    claim.prior_auth_number = first.prior_auth_number
    claim.original_submission_date = first.original_submission_date
    claim.import_batch_id = batch.id
    if pseudonymized_batch:  # sticky: a later import without the marker column never clears it
        claim.extra = {**(claim.extra or {}), pseudonymized.EXTRA_KEY: True}

    original: Claim | None = None
    if first.original_claim_control_number:
        original = _find_claim(session, claims_cache, clinic_id, first.original_claim_control_number)
        if original is None:
            counts["warnings"] += 1  # the referenced original is not (yet) known
    claim.original_claim_id = original.id if original is not None else None

    existing_lines = {line.line_number: line for line in claim.lines}
    file_line_numbers: set[int] = set()
    for row in group:
        line = existing_lines.get(row.line_number)
        if line is None:
            line = ClaimLine(line_number=row.line_number)
            claim.lines.append(line)
        _apply_line(line, row)
        file_line_numbers.add(row.line_number)
        counts["lines"] += 1
    dropped_referenced = 0
    for line_number, line in existing_lines.items():
        if line_number in file_line_numbers:
            continue
        # The export is the record of the claim: a line it no longer carries goes, even when a
        # remittance already adjudicated it (that remittance line is detached first, then re-linked
        # below to whichever line now carries its procedure, or left unlinked).
        if _unlink_remittance_lines(session, line):
            dropped_referenced += 1
            counts["warnings"] += 1
        claim.lines.remove(line)
    if dropped_referenced:
        session.flush()  # detach before the orphaned lines are deleted

    service_dates: list[date] = [row.service_date for row in group]
    claim.service_date_from = min(service_dates)
    claim.service_date_to = max(service_dates)
    line_total = sum((row.charge_amount for row in group), Decimal("0"))
    declared_total = next(
        (row.claim_total_charge for row in group if row.claim_total_charge is not None), None
    )
    if declared_total is not None and declared_total != line_total:
        counts["warnings"] += 1
    claim.total_charge = line_total

    session.flush()
    if not created:
        # Lines may have been renumbered, re-ordered or trimmed: remittance lines follow procedures.
        counts["remittance_lines_relinked"] += relink_remittance_lines(session, claim)
    claims_cache[control_number] = claim
    counts["claims_created" if created else "claims_updated"] += 1
    return claim


# --------------------------------------------------------------------------------------
# Entry points
# --------------------------------------------------------------------------------------


def _companion_duplicate(session: Session, kind: str, file_sha256: str) -> bool:
    """A notes / documents file is a duplicate only when its earlier import attached every row.

    Rows skipped because their claims were not known yet can be attached by importing the same
    file again once the claims export has arrived; row-level de-duplication keeps that re-import
    from writing anything twice.
    """
    previous = find_completed_batch(session, kind, file_sha256)
    return previous is not None and not int((previous.counts or {}).get("skipped") or 0)


def import_claims_csv(
    session: Session, source: str | Path, clinic_id: int, created_by: str = "system"
) -> ImportBatch:
    """Import a claim export (one row per claim line). Returns the ImportBatch; the caller commits.

    A file whose header carries the marker column of a pseudonymized batch marks every claim it
    carries (``Claim.extra``), through the browser upload and the CLI import alike.
    """
    kind = str(ImportKind.CLAIMS)
    src = read_source(source, default_name="claims.csv")
    sha = sha256_hex(src.raw)
    if find_completed_batch(session, kind, sha) is not None:
        return duplicate_batch(session, kind=kind, file_name=src.name, file_sha256=sha, created_by=created_by)
    batch = begin_batch(session, kind=kind, file_name=src.name, file_sha256=sha, created_by=created_by)

    rows, errors, total = _read_rows(src.text, ClaimExportRow, CLAIM_EXPORT_COLUMNS, CLAIM_FIELD_LIMITS)
    errors.extend(_check_claim_structure(rows))
    if errors:
        return complete_batch(
            session,
            batch,
            status=ImportStatus.FAILED,
            counts={"rows": total, "invalid_rows": len(errors)},
            created_by=created_by,
            error=_invalid_error(errors),
        )

    counts: dict[str, int] = {key: 0 for key in CLAIM_COUNT_KEYS}
    marked = pseudonymized.is_pseudonymized_header(_header(src.text))
    groups: dict[str, list[ClaimExportRow]] = {}
    for _, row in rows:
        groups.setdefault(row.claim_control_number, []).append(row)

    payers: dict[str, Payer] = {}
    patients: dict[str, Patient] = {}
    claims: dict[str, Claim] = {}
    for group in groups.values():
        first = group[0]
        payer = _upsert_payer(session, payers, first, counts)
        patient = _upsert_patient(session, patients, clinic_id, first, counts)
        _upsert_claim(
            session,
            clinic_id=clinic_id,
            batch=batch,
            group=group,
            payer=payer,
            patient=patient,
            claims_cache=claims,
            counts=counts,
            pseudonymized_batch=marked,
        )

    # An 835 imported before this export left its CLPs unmatched; they can be resolved now.
    counts["remittance_claims_matched"] = rematch_unmatched(session)
    return complete_batch(session, batch, status=ImportStatus.DONE, counts=counts, created_by=created_by)


def _finding_dict(finding: Any) -> dict[str, Any]:
    if hasattr(finding, "model_dump"):
        return finding.model_dump(mode="json")
    return dict(finding)


def import_notes_csv(
    session: Session, source: str | Path, clinic_id: int, created_by: str = "system"
) -> ImportBatch:
    """Attach quoted note excerpts to existing claims, scanning each for planted instructions.

    Notes are untrusted text like payer letters: a row with findings is stored with
    ``flagged_injection=True`` (counted under ``flagged``) so the pipeline, the drafter and the review
    page treat it as data to warn about, never as a sentence to quote. Rows for unknown claims are
    counted as skipped.
    """
    kind = str(ImportKind.NOTES)
    src = read_source(source, default_name="notes.csv")
    sha = sha256_hex(src.raw)
    if _companion_duplicate(session, kind, sha):
        return duplicate_batch(session, kind=kind, file_name=src.name, file_sha256=sha, created_by=created_by)
    batch = begin_batch(session, kind=kind, file_name=src.name, file_sha256=sha, created_by=created_by)

    rows, errors, total = _read_rows(src.text, NoteRow, NOTE_COLUMNS, NOTE_FIELD_LIMITS)
    if errors:
        return complete_batch(
            session,
            batch,
            status=ImportStatus.FAILED,
            counts={"rows": total, "invalid_rows": len(errors)},
            created_by=created_by,
            error=_invalid_error(errors),
        )

    counts: dict[str, int] = {"rows": total, "notes": 0, "skipped": 0, "duplicates": 0, "flagged": 0}
    claims: dict[str, Claim] = {}
    for _, row in rows:
        claim = _find_claim(session, claims, clinic_id, row.claim_control_number)
        if claim is None:
            counts["skipped"] += 1
            continue
        author = row.author or None
        if any(
            n.note_date == row.note_date and n.author == author and n.text == row.text for n in claim.notes
        ):
            counts["duplicates"] += 1
            continue
        findings = detect_injection(row.text)
        if findings:
            counts["flagged"] += 1
        claim.notes.append(
            NoteExcerpt(
                note_date=row.note_date,
                author=author,
                text=row.text,
                source=src.name[:100],
                flagged_injection=bool(findings),
                injection_findings=[_finding_dict(f) for f in findings],
            )
        )
        counts["notes"] += 1
    session.flush()
    return complete_batch(session, batch, status=ImportStatus.DONE, counts=counts, created_by=created_by)


def _normalize_document_type(value: str | None) -> str:
    candidate = (value or "").strip().lower()
    return candidate if candidate in _DOCUMENT_TYPES else str(DocumentType.OTHER)


def import_payer_documents_csv(
    session: Session, source: str | Path, clinic_id: int, created_by: str = "system"
) -> ImportBatch:
    """Attach payer letters / portal text to existing claims, scanning each for planted instructions."""
    kind = str(ImportKind.PAYER_DOCUMENTS)
    src = read_source(source, default_name="payer_documents.csv")
    sha = sha256_hex(src.raw)
    if _companion_duplicate(session, kind, sha):
        return duplicate_batch(session, kind=kind, file_name=src.name, file_sha256=sha, created_by=created_by)
    batch = begin_batch(session, kind=kind, file_name=src.name, file_sha256=sha, created_by=created_by)

    rows, errors, total = _read_rows(src.text, PayerDocumentRow, PAYER_DOCUMENT_COLUMNS)
    if errors:
        return complete_batch(
            session,
            batch,
            status=ImportStatus.FAILED,
            counts={"rows": total, "invalid_rows": len(errors)},
            created_by=created_by,
            error=_invalid_error(errors),
        )

    counts: dict[str, int] = {"rows": total, "documents": 0, "skipped": 0, "duplicates": 0, "flagged": 0}
    claims: dict[str, Claim] = {}
    for _, row in rows:
        claim = _find_claim(session, claims, clinic_id, row.claim_control_number)
        if claim is None:
            counts["skipped"] += 1
            continue
        document_type = _normalize_document_type(row.document_type)
        if any(
            d.document_type == document_type and d.received_date == row.received_date and d.text == row.text
            for d in claim.documents
        ):
            counts["duplicates"] += 1
            continue
        findings = detect_injection(row.text)
        if findings:
            counts["flagged"] += 1
        claim.documents.append(
            PayerDocument(
                document_type=document_type,
                received_date=row.received_date,
                text=row.text,
                flagged_injection=bool(findings),
                injection_findings=[_finding_dict(f) for f in findings],
            )
        )
        counts["documents"] += 1
    session.flush()
    return complete_batch(session, batch, status=ImportStatus.DONE, counts=counts, created_by=created_by)


__all__ = [
    "CLAIM_COUNT_KEYS",
    "NOTE_COLUMNS",
    "PAYER_DOCUMENT_COLUMNS",
    "import_claims_csv",
    "import_notes_csv",
    "import_payer_documents_csv",
    "patient_token",
]
