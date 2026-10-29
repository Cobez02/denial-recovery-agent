"""JSON API: health (no login), dashboard metrics and a work item without patient identifiers.

The item view leaves out everything that names the patient: names, DOB, member id, address,
account number, the appeal letter (its header carries them), note text and payer-letter text.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.auth.deps import require_user
from app.db import get_session
from app.outcomes.metrics import compute_dashboard
from app.review import service
from app.schemas import DashboardMetrics

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["api"])


def metrics_json(metrics: DashboardMetrics) -> dict[str, Any]:
    """DashboardMetrics as JSON with the overturn_rate property added to every group."""
    data = metrics.model_dump(mode="json")
    for key in ("by_category", "by_payer"):
        for entry, metric in zip(data.get(key, []), getattr(metrics, key), strict=False):
            entry["overturn_rate"] = metric.overturn_rate
    return data


def _money(value: Any) -> str | None:
    return None if value is None else str(value)


def _citations_without_note_text(citations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Note-excerpt citations quote clinical text; the API keeps the reference, not the quote."""
    out: list[dict[str, Any]] = []
    for citation in citations:
        if str(citation.get("kind")) == "note_excerpt":
            out.append({**citation, "text": None})
        else:
            out.append(dict(citation))
    return out


def item_json(detail: service.ItemDetail) -> dict[str, Any]:
    wi = detail.work_item
    claim = detail.claim
    rc = detail.remittance_claim
    proposal = detail.proposal
    return {
        "work_item": {
            "id": wi.id,
            "status": str(wi.status),
            "category": str(wi.category),
            "category_confidence": float(wi.category_confidence or 0),
            "classification_source": wi.classification_source,
            "classification_rationale": wi.classification_rationale,
            "dollar_value": _money(wi.dollar_value),
            "deadline": wi.deadline.isoformat() if wi.deadline else None,
            "deadline_kind": wi.deadline_kind,
            "deadline_badge": detail.badge,
            "days_left": detail.days_left,
            "priority_score": float(wi.priority_score or 0),
            "flags": list(wi.flags or []),
            "first_viewed_at": wi.first_viewed_at.isoformat() if wi.first_viewed_at else None,
            "decided_at": wi.decided_at.isoformat() if wi.decided_at else None,
            "submitted_at": wi.submitted_at.isoformat() if wi.submitted_at else None,
            "submitted_claim_control_number": wi.submitted_claim_control_number,
            "closed_at": wi.closed_at.isoformat() if wi.closed_at else None,
            "reasons": detail.reasons,
        },
        "claim": {
            "id": claim.id,
            "claim_control_number": claim.claim_control_number,
            "patient_token": detail.patient.token if detail.patient else None,
            "frequency_code": claim.frequency_code,
            "status": claim.status,
            "service_date_from": claim.service_date_from.isoformat() if claim.service_date_from else None,
            "service_date_to": claim.service_date_to.isoformat() if claim.service_date_to else None,
            "total_charge": _money(claim.total_charge),
            "billing_provider_npi": claim.billing_provider_npi,
            "rendering_provider_npi": claim.rendering_provider_npi,
            "referring_provider_npi": claim.referring_provider_npi,
            "place_of_service": claim.place_of_service,
            "diagnosis_codes": list(claim.diagnosis_codes or []),
            "prior_auth_number": claim.prior_auth_number,
            "original_submission_date": (
                claim.original_submission_date.isoformat() if claim.original_submission_date else None
            ),
            "lines": [
                {
                    "line_number": line.line_number,
                    "service_date": line.service_date.isoformat() if line.service_date else None,
                    "procedure_code": line.procedure_code,
                    "modifiers": list(line.modifiers or []),
                    "units": _money(line.units),
                    "charge_amount": _money(line.charge_amount),
                    "diagnosis_pointers": list(line.diagnosis_pointers or []),
                    "ndc": line.ndc,
                }
                for line in claim.lines
            ],
        },
        "payer": {
            "id": detail.payer.id if detail.payer else None,
            "name": detail.payer.name if detail.payer else None,
            "payer_id": detail.payer.payer_id if detail.payer else None,
        },
        "remittance": {
            "remittance_claim_id": rc.id if rc else None,
            "file_name": detail.remittance.file_name if detail.remittance else None,
            "payment_date": (
                detail.remittance.payment_date.isoformat()
                if detail.remittance and detail.remittance.payment_date
                else None
            ),
            "trace_number": detail.remittance.trace_number if detail.remittance else None,
            "claim_status_code": rc.claim_status_code if rc else None,
            "charge_amount": _money(rc.charge_amount) if rc else None,
            "paid_amount": _money(rc.paid_amount) if rc else None,
            "patient_responsibility": _money(rc.patient_responsibility) if rc else None,
            "denied_amount": _money(rc.denied_amount) if rc else None,
            "payer_claim_control_number": rc.payer_claim_control_number if rc else None,
            "adjustments": [
                {**a, "amount": _money(a["amount"]), "quantity": _money(a["quantity"])}
                for a in detail.adjustments
            ],
            "remark_codes": detail.remark_codes,
        },
        "documents": [
            {
                "id": d.id,
                "document_type": d.document_type,
                "received_date": d.received_date.isoformat() if d.received_date else None,
                "flagged_injection": d.flagged,
                "injection_findings": [f.get("pattern") for f in d.findings],
                "characters": len(d.text),
            }
            for d in detail.documents
        ],
        "notes": {"count": len(detail.notes)},
        "proposal": (
            {
                "id": proposal.id,
                "version": proposal.version,
                "action_type": str(proposal.action_type),
                "summary": proposal.summary,
                "rationale": proposal.rationale,
                "citations": _citations_without_note_text(detail.citations),
                "corrected_claim_diff": detail.diff_rows,
                "clinic_question": proposal.clinic_question,
                "has_appeal_letter": bool(proposal.appeal_letter),
                "coding_change": bool(proposal.coding_change),
                "requires_note_excerpt": bool(proposal.requires_note_excerpt),
                "note_excerpt_attached": detail.note_excerpt_attached,
                "confidence": float(proposal.confidence or 0),
                "prompt_version": proposal.prompt_version,
                "model_id": proposal.model_id,
                "generated_by": proposal.generated_by,
            }
            if proposal
            else None
        ),
        "proposal_versions": [p.version for p in detail.proposals],
        "actions": [
            {
                "id": a.id,
                "action": str(a.action),
                "user_id": a.user_id,
                "reason": a.reason,
                "review_seconds": a.review_seconds,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in detail.actions
        ],
        "outcomes": [
            {
                "id": o.id,
                "result": str(o.result),
                "paid_amount": _money(o.paid_amount),
                "payment_date": o.payment_date.isoformat() if o.payment_date else None,
            }
            for o in detail.outcomes
        ],
        "model_calls": detail.model_calls,
    }


@router.get("/health")
def health(session: Session = Depends(get_session)) -> JSONResponse:
    db_ok = True
    chain_ok: bool | None = None
    try:
        session.execute(select(1))
    except Exception as exc:  # the database is down or unreachable
        log.warning("health check: database unavailable (%s)", type(exc).__name__)
        db_ok = False
    if db_ok:
        try:
            chain_ok = audit.verify_chain(session) is None
        except Exception as exc:
            log.warning("health check: audit verification failed (%s)", type(exc).__name__)
            chain_ok = False
    status = "ok" if db_ok and chain_ok else "degraded"
    return JSONResponse(
        {"status": status, "db_ok": db_ok, "audit_chain_ok": chain_ok},
        status_code=200 if status == "ok" else 503,
    )


@router.get("/metrics")
def metrics(user: Any = Depends(require_user), session: Session = Depends(get_session)) -> dict[str, Any]:
    return metrics_json(compute_dashboard(session))


@router.get("/items/{item_id}")
def item(
    item_id: int, user: Any = Depends(require_user), session: Session = Depends(get_session)
) -> dict[str, Any]:
    detail = service.item_detail(session, item_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="work item not found")
    return item_json(detail)


__all__ = ["item_json", "metrics_json", "router"]
