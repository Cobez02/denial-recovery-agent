"""Review queue pages and actions (M7), plus the imports, dashboard and audit pages (M8).

Every route depends on ``require_user``; every POST also on ``csrf_protect``. Actions call the
service, commit, then answer with the refreshed item panel (HTMX) or a redirect to the item page.
A ``ValueError`` from the service is a refused action: the page is re-rendered with the message and
status 400, never a 500, and nothing is committed.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import distinct, exists, func, select
from sqlalchemy.orm import Session, selectinload

from app import audit
from app.auth.deps import HX_HEADER, CurrentUser, csrf_protect, require_user
from app.auth.service import actor_for
from app.config import get_settings
from app.db import get_session
from app.enums import ActorType, DenialCategory, WorkItemStatus
from app.ingest.remit_import import rematch_unmatched
from app.jobs import JobHandle, enqueue
from app.models import AuditLog, Clinic, ImportBatch, Payer, Remittance, RemittanceClaim, WorkItem
from app.outcomes.metrics import compute_dashboard
from app.review import service
from app.review.export import build_export_package
from app.review.filters import flash, render, today_utc

log = logging.getLogger(__name__)

router = APIRouter(tags=["review"])

MAX_UPLOAD_BYTES = 20 * 1024 * 1024
UPLOAD_CHUNK = 1024 * 1024
AUDIT_PAGE_SIZE = 100
BATCH_LIST_LIMIT = 100
UNMATCHED_LIST_LIMIT = 200
QUEUE_PATH = "/queue"
IMPORTS_PATH = "/imports"

# upload kind -> (task path, stored extension)
IMPORT_TASKS: dict[str, tuple[str, str]] = {
    "835": ("app.jobs.tasks.import_835_file", ".835"),
    "claims": ("app.jobs.tasks.import_claims_file", ".csv"),
    "notes": ("app.jobs.tasks.import_notes_file", ".csv"),
    "documents": ("app.jobs.tasks.import_documents_file", ".csv"),
}
PROCESS_TASK = "app.jobs.tasks.process_pending"
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")

ActionFn = Callable[[WorkItem], Any]


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _is_hx(request: Request) -> bool:
    return bool(request.headers.get(HX_HEADER))


def _redirect(path: str) -> RedirectResponse:
    return RedirectResponse(url=path, status_code=303)


def _blank(value: str | None) -> str | None:
    text = (value or "").strip()
    return text or None


def _parse_int(value: str | None) -> int | None:
    text = _blank(value)
    if text is None:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _parse_flag(value: str | None) -> bool | str | None:
    text = _blank(value)
    if text is None:
        return None
    lowered = text.lower()
    if lowered in ("1", "true", "yes", "any", "flagged"):
        return True
    if lowered in ("0", "false", "no", "none", "unflagged"):
        return False
    return text


def _parse_date(value: str | None) -> date | None:
    text = _blank(value)
    if text is None:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("invalid_date") from exc


def _patient_name(work_item: WorkItem) -> str:
    patient = work_item.claim.patient if work_item.claim is not None else None
    if patient is None:
        return ""
    return ", ".join(part for part in (patient.last_name, patient.first_name) if part)


def _item_or_404(session: Session, item_id: int) -> WorkItem:
    work_item = service.get_work_item(session, item_id)
    if work_item is None:
        raise HTTPException(status_code=404, detail="work item not found")
    return work_item


def _render_item(
    request: Request,
    session: Session,
    item_id: int,
    *,
    error: str | None = None,
    status_code: int = 200,
    partial: bool = False,
) -> Response:
    detail = service.item_detail(session, item_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="work item not found")
    name = "partials/item_panel.html" if partial else "pages/item.html"
    return render(request, name, {"d": detail, "error": error}, status_code=status_code)


def _run_action(request: Request, session: Session, item_id: int, fn: ActionFn) -> Response:
    """Apply one service call to the item: commit and show the result, or 400 with the reason."""
    work_item = _item_or_404(session, item_id)
    try:
        fn(work_item)
    except ValueError as exc:
        session.rollback()
        code = str(exc) or "invalid_request"
        log.info("review action refused on work item %s: %s", item_id, code.split("\n", 1)[0][:60])
        return _render_item(request, session, item_id, error=code, status_code=400, partial=_is_hx(request))
    session.commit()
    if _is_hx(request):
        return _render_item(request, session, item_id, partial=True)
    return _redirect(f"/items/{item_id}")


# --------------------------------------------------------------------------------------
# Queue and items
# --------------------------------------------------------------------------------------


@router.get("/")
def home() -> Response:
    return _redirect(QUEUE_PATH)


@router.get("/queue")
def queue_page(
    request: Request,
    status: str | None = None,
    category: str | None = None,
    payer_id: str | None = None,
    flagged: str | None = None,
    sort: str | None = None,
    user: CurrentUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> Response:
    filters = {
        "status": _blank(status),
        "category": _blank(category),
        "payer_id": _parse_int(payer_id),
        "flagged": _blank(flagged),
        "sort": _blank(sort) or "default",
    }
    items = service.queue(
        session,
        status=filters["status"],
        category=filters["category"],
        payer_id=filters["payer_id"],
        flagged=_parse_flag(filters["flagged"]),
        sort=filters["sort"],
    )
    today = today_utc()
    rows = [
        {
            "item": wi,
            "badge": service.deadline_badge(wi, today),
            "days_left": service.days_left(wi, today),
            "proposal": wi.current_proposal,
            "patient_name": _patient_name(wi),
        }
        for wi in items
    ]
    context = {
        "rows": rows,
        "filters": filters,
        "counts": service.status_counts(session),
        "payers": list(session.execute(select(Payer).order_by(Payer.name)).scalars()),
        "categories": [str(c) for c in DenialCategory],
        "statuses": [str(s) for s in WorkItemStatus],
        "sorts": list(service.SORTS),
        "today": today,
    }
    name = "partials/queue_table.html" if _is_hx(request) else "pages/queue.html"
    return render(request, name, context)


@router.get("/items/{item_id}")
def item_page(
    item_id: int,
    request: Request,
    user: CurrentUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> Response:
    work_item = _item_or_404(session, item_id)
    service.record_view(session, work_item, user)
    session.commit()
    return _render_item(request, session, item_id, partial=_is_hx(request))


@router.post("/items/{item_id}/approve", dependencies=[Depends(csrf_protect)])
def approve_item(
    item_id: int,
    request: Request,
    note_excerpt: str | None = Form(None),
    edited_letter: str | None = Form(None),
    user: CurrentUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> Response:
    return _run_action(
        request,
        session,
        item_id,
        lambda wi: service.approve(session, wi, user, note_excerpt=note_excerpt, edited_letter=edited_letter),
    )


@router.post("/items/{item_id}/edit", dependencies=[Depends(csrf_protect)])
async def edit_item(
    item_id: int,
    request: Request,
    user: CurrentUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> Response:
    form = await request.form()
    reason = str(form.get("reason") or "")
    payload: dict[str, Any] = {}
    for name in service.EDITABLE_TEXT_FIELDS:
        if name in form:
            payload[name] = str(form.get(name) or "")
    corrected = {key[3:]: str(form.get(key) or "") for key in form.keys() if key.startswith("cc:")}
    if corrected:
        payload["corrected_claim"] = corrected
    return _run_action(
        request,
        session,
        item_id,
        lambda wi: service.edit(session, wi, user, reason=reason, edited_payload=payload),
    )


@router.post("/items/{item_id}/reject", dependencies=[Depends(csrf_protect)])
def reject_item(
    item_id: int,
    request: Request,
    reason: str = Form(""),
    user: CurrentUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> Response:
    return _run_action(request, session, item_id, lambda wi: service.reject(session, wi, user, reason=reason))


@router.post("/items/{item_id}/mark-submitted", dependencies=[Depends(csrf_protect)])
def mark_submitted_item(
    item_id: int,
    request: Request,
    submitted_on: str | None = Form(None),
    submitted_claim_control_number: str | None = Form(None),
    user: CurrentUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> Response:
    def fn(wi: WorkItem) -> Any:
        return service.mark_submitted(
            session,
            wi,
            user,
            submitted_on=_parse_date(submitted_on),
            submitted_claim_control_number=submitted_claim_control_number,
        )

    return _run_action(request, session, item_id, fn)


@router.post("/items/{item_id}/reopen", dependencies=[Depends(csrf_protect)])
def reopen_item(
    item_id: int,
    request: Request,
    reason: str | None = Form(None),
    user: CurrentUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> Response:
    return _run_action(request, session, item_id, lambda wi: service.reopen(session, wi, user, reason=reason))


@router.post("/items/{item_id}/close", dependencies=[Depends(csrf_protect)])
def close_item(
    item_id: int,
    request: Request,
    reason: str | None = Form(None),
    user: CurrentUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> Response:
    return _run_action(request, session, item_id, lambda wi: service.close(session, wi, user, reason=reason))


@router.post("/items/{item_id}/attach-note", dependencies=[Depends(csrf_protect)])
def attach_note_item(
    item_id: int,
    request: Request,
    text: str = Form(""),
    user: CurrentUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> Response:
    return _run_action(
        request, session, item_id, lambda wi: service.attach_note(session, wi, user, text=text)
    )


@router.post("/items/{item_id}/regenerate", dependencies=[Depends(csrf_protect)])
def regenerate_item(
    item_id: int,
    request: Request,
    user: CurrentUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> Response:
    return _run_action(request, session, item_id, lambda wi: service.regenerate(session, wi, user))


@router.get("/items/{item_id}/export")
def export_item(
    item_id: int,
    request: Request,
    user: CurrentUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> Response:
    work_item = _item_or_404(session, item_id)
    try:
        payload, file_name = build_export_package(session, work_item, user)
    except ValueError as exc:
        session.rollback()
        return _render_item(
            request, session, item_id, error=str(exc), status_code=400, partial=_is_hx(request)
        )
    session.commit()
    headers = {"Content-Disposition": f'attachment; filename="{file_name}"'}
    return Response(content=payload, media_type="application/zip", headers=headers)


# --------------------------------------------------------------------------------------
# Imports
# --------------------------------------------------------------------------------------


def _resolve_clinic(session: Session) -> Clinic | None:
    """CLINIC_ID from settings, else the only clinic in the database, else None."""
    clinic_id = get_settings().clinic_id
    if clinic_id is not None:
        return session.get(Clinic, int(clinic_id))
    clinics = list(session.execute(select(Clinic).order_by(Clinic.id)).scalars())
    return clinics[0] if len(clinics) == 1 else None


def _pending_denials(session: Session) -> int:
    has_item = exists().where(WorkItem.remittance_claim_id == RemittanceClaim.id)
    stmt = select(func.count(RemittanceClaim.id)).where(
        RemittanceClaim.is_denial.is_(True), RemittanceClaim.claim_id.is_not(None), ~has_item
    )
    return int(session.execute(stmt).scalar_one() or 0)


def _imports_context(
    session: Session, *, job: dict[str, Any] | None = None, error: str | None = None
) -> dict[str, Any]:
    batches = list(
        session.execute(select(ImportBatch).order_by(ImportBatch.id.desc()).limit(BATCH_LIST_LIMIT)).scalars()
    )
    unmatched_stmt = (
        select(RemittanceClaim)
        .where(RemittanceClaim.claim_id.is_(None))
        .options(selectinload(RemittanceClaim.remittance).selectinload(Remittance.payer))
        .order_by(RemittanceClaim.id.desc())
        .limit(UNMATCHED_LIST_LIMIT)
    )
    unmatched = list(session.execute(unmatched_stmt).scalars())
    unmatched_total = int(
        session.execute(
            select(func.count(RemittanceClaim.id)).where(RemittanceClaim.claim_id.is_(None))
        ).scalar_one()
        or 0
    )
    return {
        "batches": batches,
        "unmatched": unmatched,
        "unmatched_total": unmatched_total,
        "pending_denials": _pending_denials(session),
        "clinic": _resolve_clinic(session),
        "job": job,
        "error": error,
        "max_upload_mb": MAX_UPLOAD_BYTES // (1024 * 1024),
    }


def _job_view(handle: JobHandle, *, kind: str, file_name: str | None) -> dict[str, Any]:
    result = handle.result if isinstance(handle.result, dict) else {}
    return {
        "id": handle.id,
        "status": handle.status,
        "error": handle.error,
        "kind": kind,
        "file_name": file_name,
        "queued": handle.status == "queued",
        "batch_id": result.get("batch_id"),
        "batch_status": result.get("status"),
        "counts": dict(result.get("counts") or {}),
        "batch_error": result.get("error"),
        "pipeline": result.get("pipeline"),
        "pipeline_error": result.get("pipeline_error"),
        "work_items_created": result.get("work_items_created"),
        "work_item_ids": list(result.get("work_item_ids") or [])[:50],
    }


def _job_details(job: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": job["kind"],
        "job_status": job["status"],
        "job_error": job["error"],
        "batch_id": job["batch_id"],
        "batch_status": job["batch_status"],
        "counts": job["counts"],
        "pipeline": job["pipeline"],
        "pipeline_error": job["pipeline_error"],
        "work_items_created": job["work_items_created"],
    }


def _safe_file_name(original: str | None, extension: str) -> str:
    """The upload's own name reduced to [A-Za-z0-9._-] (it becomes part of the stored file name)."""
    base = Path(original or "").name
    cleaned = _UNSAFE_NAME.sub("_", base).strip("._")[:100]
    return cleaned or f"upload{extension}"


async def _store_upload(file: UploadFile, kind: str, original_name: str) -> tuple[Path, str, int]:
    """Save the upload under DATA_DIR/uploads/<kind>/<sha256 prefix>_<name>; refuse empty or oversized files.

    The stored name starts with the content hash, so the same bytes land in the same file, and ends
    with the sanitized original name, which the CSV importers record as the batch file name.
    """
    data = bytearray()
    while True:
        chunk = await file.read(UPLOAD_CHUNK)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > MAX_UPLOAD_BYTES:
            raise ValueError("file_too_large")
    if not data:
        raise ValueError("empty_file")
    sha = hashlib.sha256(data).hexdigest()
    target_dir = get_settings().data_path / "uploads" / kind
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{sha[:16]}_{original_name}"
    path.write_bytes(bytes(data))
    return path, sha, len(data)


async def _handle_upload(
    request: Request, session: Session, user: CurrentUser, kind: str, file: UploadFile
) -> Response:
    task_path, extension = IMPORT_TASKS[kind]
    original_name = _safe_file_name(file.filename, extension)
    actor = actor_for(user)
    try:
        path, sha, size = await _store_upload(file, kind, original_name)
        clinic_id: int | None = None
        if kind != "835":
            clinic = _resolve_clinic(session)
            if clinic is None:
                raise ValueError("no_clinic")
            clinic_id = clinic.id
    except ValueError as exc:
        code = str(exc)
        status_code = 413 if code == "file_too_large" else 400
        return render(
            request, "pages/imports.html", _imports_context(session, error=code), status_code=status_code
        )
    finally:
        await file.close()

    audit.record(
        session,
        actor_type=ActorType.USER,
        actor_id=actor,
        action="import_requested",
        details={"kind": kind, "bytes": size, "sha256": sha},
    )
    session.commit()  # release this transaction: the inline job commits in a session of its own
    if kind == "835":
        handle = enqueue(task_path, str(path), file_name=original_name, created_by=actor)
    else:
        handle = enqueue(task_path, str(path), clinic_id, created_by=actor)
    job = _job_view(handle, kind=kind, file_name=original_name)
    audit.record(
        session,
        actor_type=ActorType.USER,
        actor_id=actor,
        action="import_job_finished",
        entity_type="import_batch",
        entity_id=job["batch_id"],
        details=_job_details(job),
    )
    session.commit()
    log.info("%s upload by %s: job %s %s", kind, actor, job["id"], job["status"])
    return render(request, "pages/imports.html", _imports_context(session, job=job))


@router.get("/imports")
def imports_page(
    request: Request,
    user: CurrentUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> Response:
    return render(request, "pages/imports.html", _imports_context(session))


@router.post("/imports/835", dependencies=[Depends(csrf_protect)])
async def import_835(
    request: Request,
    file: UploadFile = File(...),
    user: CurrentUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> Response:
    return await _handle_upload(request, session, user, "835", file)


@router.post("/imports/claims", dependencies=[Depends(csrf_protect)])
async def import_claims(
    request: Request,
    file: UploadFile = File(...),
    user: CurrentUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> Response:
    return await _handle_upload(request, session, user, "claims", file)


@router.post("/imports/notes", dependencies=[Depends(csrf_protect)])
async def import_notes(
    request: Request,
    file: UploadFile = File(...),
    user: CurrentUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> Response:
    return await _handle_upload(request, session, user, "notes", file)


@router.post("/imports/documents", dependencies=[Depends(csrf_protect)])
async def import_documents(
    request: Request,
    file: UploadFile = File(...),
    user: CurrentUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> Response:
    return await _handle_upload(request, session, user, "documents", file)


@router.post("/imports/process", dependencies=[Depends(csrf_protect)])
def process_pending(
    request: Request,
    user: CurrentUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> Response:
    actor = actor_for(user)
    audit.record(session, actor_type=ActorType.USER, actor_id=actor, action="process_requested")
    session.commit()
    handle = enqueue(PROCESS_TASK, created_by=actor)
    job = _job_view(handle, kind="process", file_name=None)
    audit.record(
        session,
        actor_type=ActorType.USER,
        actor_id=actor,
        action="process_job_finished",
        details=_job_details(job),
    )
    session.commit()
    return render(request, "pages/imports.html", _imports_context(session, job=job))


@router.post("/imports/rematch", dependencies=[Depends(csrf_protect)])
def rematch(
    request: Request,
    user: CurrentUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> Response:
    matched = rematch_unmatched(session)
    audit.record(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_for(user),
        action="remittance_rematched",
        details={"matched": matched},
    )
    session.commit()
    flash(
        request,
        f"Re-matched {matched} remittance claim(s). Newly matched denials appear under pending denials.",
    )
    return _redirect(IMPORTS_PATH)


# --------------------------------------------------------------------------------------
# Dashboard and audit
# --------------------------------------------------------------------------------------


@router.get("/dashboard")
def dashboard(
    request: Request,
    user: CurrentUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> Response:
    return render(request, "pages/dashboard.html", {"m": compute_dashboard(session)})


@router.get("/audit")
def audit_page(
    request: Request,
    page: str | None = None,
    claim_id: str | None = None,
    action: str | None = None,
    user: CurrentUser = Depends(require_user),
    session: Session = Depends(get_session),
) -> Response:
    page_number = max(_parse_int(page) or 1, 1)
    claim_filter = _parse_int(claim_id)
    action_filter = _blank(action)
    stmt = select(AuditLog)
    if claim_filter is not None:
        stmt = stmt.where(AuditLog.claim_id == claim_filter)
    if action_filter:
        stmt = stmt.where(AuditLog.action == action_filter)
    total = int(session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one() or 0)
    pages = max(1, math.ceil(total / AUDIT_PAGE_SIZE))
    page_number = min(page_number, pages)
    rows = list(
        session.execute(
            stmt.order_by(AuditLog.id.desc())
            .offset((page_number - 1) * AUDIT_PAGE_SIZE)
            .limit(AUDIT_PAGE_SIZE)
        ).scalars()
    )
    broken = audit.verify_chain(session)
    actions = [row[0] for row in session.execute(select(distinct(AuditLog.action)).order_by(AuditLog.action))]
    context = {
        "rows": rows,
        "page": page_number,
        "pages": pages,
        "total": total,
        "page_size": AUDIT_PAGE_SIZE,
        "chain_ok": broken is None,
        "broken_id": broken,
        "claim_id": claim_filter,
        "action": action_filter,
        "actions": actions,
    }
    return render(request, "pages/audit.html", context)


__all__ = ["router"]
