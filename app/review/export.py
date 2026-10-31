"""Export package for an approved work item: the zip a human takes to the clearinghouse or payer.

Nothing is submitted by the software. The package holds ``summary.json``, the corrected claim as
JSON and CSV (claim-export columns, patient block from the patient record) for a corrected claim,
the appeal letter as text, HTML and PDF (or a README when the PDF renderer is missing) for an
appeal, ``clinic_question.txt`` for a question to the clinic, and ``audit_excerpt.json`` with the
audit rows of the claim. The first export moves the item to ``exported``; later downloads of an
exported or submitted item are allowed and audited again.

A claim imported from a pseudonymized batch (``app/review/pseudonymized.py``) gets a package that
cannot pass for a real one: ``PSEUDONYMIZED_`` in front of the zip name, a banner on top of the
letter and the clinic question, ``"pseudonymized": true`` in ``summary.json`` and a marker column in
the corrected claim CSV. ``denial-agent deanonymize`` removes them while it restores the package.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import zipfile
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.drafting.corrected_claim import corrected_claim_to_csv_rows
from app.drafting.pdf import letter_html, render_pdf
from app.enums import ActionType, ReviewActionType, WorkItemStatus
from app.models import AuditLog, Claim, ImportBatch, Patient, WorkItem
from app.review import pseudonymized, service
from app.schemas import CLAIM_EXPORT_COLUMNS

log = logging.getLogger(__name__)

PDF_README = (
    "appeal_letter.pdf was not produced: the PDF renderer (WeasyPrint) is not installed in this "
    "environment. Print appeal_letter.html instead; it carries the same text.\n"
)
_SAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return str(value)


def _dumps(data: Any) -> bytes:
    return json.dumps(data, indent=2, sort_keys=True, default=_json_default, ensure_ascii=False).encode(
        "utf-8"
    )


def _money(value: Decimal | None) -> str | None:
    """A dollar amount at a fixed two-decimal scale: '385.00' whether or not the row was reloaded."""
    return None if value is None else str(Decimal(value).quantize(Decimal("0.01")))


def patient_fields(patient: Patient | None) -> dict[str, Any]:
    """The patient block of the claim export, taken from the patient record (not from the proposal)."""
    if patient is None:
        return {}
    return {
        "patient_account_number": patient.account_number,
        "patient_first_name": patient.first_name,
        "patient_last_name": patient.last_name,
        "patient_dob": patient.dob,
        "patient_sex": patient.sex,
        "patient_address_line1": patient.address_line1,
        "patient_city": patient.city,
        "patient_state": patient.state,
        "patient_zip": patient.zip,
        "member_id": patient.member_id,
    }


def csv_bytes(rows: list[dict[str, str]], *, pseudonymized_marker: bool = False) -> bytes:
    columns = [*CLAIM_EXPORT_COLUMNS, *([pseudonymized.CSV_COLUMN] if pseudonymized_marker else [])]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        out = {column: row.get(column, "") for column in CLAIM_EXPORT_COLUMNS}
        if pseudonymized_marker:
            out[pseudonymized.CSV_COLUMN] = pseudonymized.CSV_VALUE
        writer.writerow(out)
    return buffer.getvalue().encode("utf-8")


def from_pseudonymized_batch(session: Session, claim: Claim) -> bool:
    """True when the claim's last import came from a file `denial-agent anonymize` wrote."""
    if claim.import_batch_id is None:
        return False
    batch = session.get(ImportBatch, claim.import_batch_id)
    return batch is not None and pseudonymized.is_pseudonymized_file(batch.file_name)


def _bannered(text: str, marked: bool) -> str:
    return f"{pseudonymized.BANNER}\n\n{text}" if marked else text


def audit_rows_for_claim(session: Session, claim_id: int | None) -> list[dict[str, Any]]:
    if claim_id is None:
        return []
    stmt = select(AuditLog).where(AuditLog.claim_id == claim_id).order_by(AuditLog.id)
    return [
        {
            "id": row.id,
            "ts": row.ts,
            "actor_type": row.actor_type,
            "actor_id": row.actor_id,
            "action": row.action,
            "entity_type": row.entity_type,
            "entity_id": row.entity_id,
            "claim_id": row.claim_id,
            "reason": row.reason,
            "details": row.details,
            "prev_hash": row.prev_hash,
            "hash": row.hash,
        }
        for row in session.execute(stmt).scalars()
    ]


def package_file_name(work_item: WorkItem, version: int, now: datetime, *, marked: bool = False) -> str:
    ccn = _SAFE.sub("-", work_item.claim.claim_control_number or "claim").strip("-")[:40]
    prefix = pseudonymized.ZIP_PREFIX if marked else ""
    return f"{prefix}work_item_{work_item.id}_{ccn}_v{version}_{now.strftime('%Y%m%dT%H%M%SZ')}.zip"


def build_export_package(session: Session, work_item: WorkItem, user: Any) -> tuple[bytes, str]:
    """Build (zip bytes, file name) for an approved item; also written under settings.export_path."""
    service._require_status(work_item, "export")
    proposal = service._current_proposal_or_raise(work_item)
    claim = work_item.claim
    patient = claim.patient
    payer = claim.payer
    now = datetime.now(UTC)
    action_type = str(proposal.action_type)
    marked = from_pseudonymized_batch(session, claim)
    members: dict[str, bytes] = {}

    summary = {
        "work_item_id": work_item.id,
        "claim_id": claim.id,
        "claim_control_number": claim.claim_control_number,
        "payer": {"name": payer.name if payer else None, "payer_id": payer.payer_id if payer else None},
        "category": str(work_item.category),
        "category_confidence": float(work_item.category_confidence or 0),
        "action_type": action_type,
        "summary": proposal.summary,
        "rationale": proposal.rationale,
        "deadline": work_item.deadline,
        "deadline_kind": work_item.deadline_kind,
        "dollar_value": _money(work_item.dollar_value),
        "citations": proposal.citations or [],
        "flags": list(work_item.flags or []),
        "coding_change": bool(proposal.coding_change),
        "requires_note_excerpt": bool(proposal.requires_note_excerpt),
        "note_excerpt_attached": service.has_note_excerpt(work_item),
        # The excerpts the reviewer attached, with their provenance: source "reviewer" means the
        # text was typed on the item page (it may quote a chart the system never saw).
        "note_excerpts": [
            {
                "note_id": note.id,
                "source": note.source,
                "author": note.author,
                "note_date": note.note_date,
                "typed_by_reviewer": str(note.source or "") == service.NOTE_SOURCE_REVIEWER,
                "text": note.text,
            }
            for note in service.attached_notes(session, work_item)
        ],
        "proposal_id": proposal.id,
        "proposal_version": proposal.version,
        "generated_by": proposal.generated_by,
        "reviewer_email": getattr(user, "email", None),
        "status_at_export": str(work_item.status),
        "first_viewed_at": work_item.first_viewed_at,
        "decided_at": work_item.decided_at,
        "exported_at": now,
        "submitted_by_software": False,
        "note": "Produced by the Denial Recovery Agent for a human to submit. Nothing has been sent.",
    }
    if marked:
        summary[pseudonymized.SUMMARY_KEY] = True
    members["summary.json"] = _dumps(summary)

    if action_type == str(ActionType.CORRECTED_CLAIM):
        corrected = service.load_corrected_claim(proposal)
        if corrected is not None:
            members["corrected_claim.json"] = _dumps(corrected.model_dump(mode="json"))
            members["corrected_claim.csv"] = csv_bytes(
                corrected_claim_to_csv_rows(corrected, patient_fields(patient)), pseudonymized_marker=marked
            )
    if action_type == str(ActionType.APPEAL) and proposal.appeal_letter:
        letter = _bannered(proposal.appeal_letter, marked)
        title = f"Appeal of claim {claim.claim_control_number}"
        members["appeal_letter.txt"] = letter.encode("utf-8")
        html = letter_html(letter, title)
        members["appeal_letter.html"] = html.encode("utf-8")
        pdf = render_pdf(html)
        if pdf:
            members["appeal_letter.pdf"] = pdf
        else:
            members["README.txt"] = PDF_README.encode("utf-8")
    if action_type == str(ActionType.CLINIC_QUESTION) and proposal.clinic_question:
        members["clinic_question.txt"] = _bannered(proposal.clinic_question, marked).encode("utf-8")
    members["audit_excerpt.json"] = _dumps(audit_rows_for_claim(session, work_item.claim_id))

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    payload = buffer.getvalue()

    file_name = package_file_name(work_item, proposal.version, now, marked=marked)
    export_dir: Path = get_settings().export_path
    export_dir.mkdir(parents=True, exist_ok=True)
    (export_dir / file_name).write_bytes(payload)

    first_export = str(work_item.status) == str(WorkItemStatus.APPROVED)
    if first_export:
        work_item.status = str(WorkItemStatus.EXPORTED)
    member_names = sorted(members)
    service._add_action(
        session,
        work_item,
        user,
        ReviewActionType.EXPORT,
        proposal=proposal,
        payload={"members": member_names, "file_name": file_name, "bytes": len(payload)},
    )
    service._audit(
        session,
        work_item,
        user,
        "package_exported",
        details={
            "members": member_names,
            "file_name": file_name,
            "bytes": len(payload),
            "proposal_version": proposal.version,
            "first_export": first_export,
        },
    )
    log.info("work item %s exported (%d members, %d bytes)", work_item.id, len(member_names), len(payload))
    return payload, file_name


__all__ = [
    "PDF_README",
    "audit_rows_for_claim",
    "build_export_package",
    "csv_bytes",
    "from_pseudonymized_batch",
    "patient_fields",
]
