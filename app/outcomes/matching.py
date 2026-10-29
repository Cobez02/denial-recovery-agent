"""Outcome matching (milestone M8): close the loop on submitted fixes.

A later 835 tells us what the payer did with a corrected claim or appeal the clinic submitted.
For every CLP in that remittance we look for work items that were exported or marked submitted,
either on the same claim or carrying the CLP's patient control number as their submitted
(corrected) claim control number, and record one `outcomes` row per (work item, CLP):

    reversed      CLP02 == 22 (reversal of a previous payment)
    denied_again  nothing paid: denial adjustments present (reason "denial") or simply zero paid
                  (reason "zero_paid")
    paid          paid >= 95% of the work item's dollar value
    partial       0 < paid < 95% of the dollar value

The work item is closed, the claim status follows the result and an `outcome_recorded` audit
event is appended. Everything is idempotent: re-running on the same remittance changes nothing.
A work item's own originating remittance claim never counts as its outcome.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app import audit
from app.enums import ActorType, ClaimStatus, OutcomeResult, WorkItemStatus
from app.models import Claim, Outcome, Remittance, RemittanceClaim, WorkItem

log = logging.getLogger(__name__)

OUTCOMES_ACTOR = "outcomes"
PAID_THRESHOLD = Decimal("0.95")
REVERSAL_STATUS_CODE = "22"
MATCHABLE_STATUSES: tuple[str, ...] = (str(WorkItemStatus.SUBMITTED), str(WorkItemStatus.EXPORTED))

CLAIM_STATUS_FOR_RESULT: dict[OutcomeResult, ClaimStatus] = {
    OutcomeResult.PAID: ClaimStatus.PAID,
    OutcomeResult.PARTIAL: ClaimStatus.PARTIAL,
    OutcomeResult.DENIED_AGAIN: ClaimStatus.DENIED,
    OutcomeResult.REVERSED: ClaimStatus.DENIED,
}

_CENTS = Decimal("0.01")


def classify_outcome(
    rc: RemittanceClaim, dollar_value: Decimal | None
) -> tuple[OutcomeResult, dict[str, Any]]:
    """Decide the outcome of one CLP for a work item worth `dollar_value` (docs/SPEC.md section 10)."""
    paid = Decimal(rc.paid_amount or 0)
    value = Decimal(dollar_value or 0)
    threshold = PAID_THRESHOLD * value
    details: dict[str, Any] = {
        "paid_amount": str(paid),
        "dollar_value": str(value),
        "paid_threshold": str(threshold.quantize(_CENTS)),
        "claim_status_code": rc.claim_status_code,
        "is_denial": bool(rc.is_denial),
    }
    if rc.claim_status_code == REVERSAL_STATUS_CODE:
        result, reason = OutcomeResult.REVERSED, "reversal"
    elif paid <= 0:
        result, reason = OutcomeResult.DENIED_AGAIN, ("denial" if rc.is_denial else "zero_paid")
    elif paid >= threshold:
        result, reason = OutcomeResult.PAID, "paid_at_least_95pct"
    else:
        result, reason = OutcomeResult.PARTIAL, "paid_below_95pct"
    details["reason"] = reason
    return result, details


def candidate_work_items(session: Session, rc: RemittanceClaim) -> list[tuple[WorkItem, str]]:
    """Submitted/exported work items this CLP can close, with how each matched.

    Match methods: "claim_id" (same claim) or "submitted_ccn" (CLP01 equals the control number
    of the corrected claim the clinic submitted for the work item).
    """
    conditions = []
    if rc.claim_id is not None:
        conditions.append(WorkItem.claim_id == rc.claim_id)
    pcn = (rc.patient_control_number or "").strip()
    if pcn:
        conditions.append(WorkItem.submitted_claim_control_number == pcn)
    if not conditions:
        return []
    stmt = (
        select(WorkItem)
        .where(or_(*conditions), WorkItem.status.in_(MATCHABLE_STATUSES))
        .order_by(WorkItem.id)
    )
    matched: list[tuple[WorkItem, str]] = []
    for work_item in session.execute(stmt).scalars():
        if work_item.remittance_claim_id == rc.id:
            continue  # the denial that created the work item is not its outcome
        how = "claim_id" if rc.claim_id is not None and work_item.claim_id == rc.claim_id else "submitted_ccn"
        matched.append((work_item, how))
    return matched


def existing_outcome(session: Session, work_item: WorkItem, rc: RemittanceClaim) -> Outcome | None:
    if work_item.id is None or rc.id is None:
        return None
    stmt = select(Outcome).where(Outcome.work_item_id == work_item.id, Outcome.remittance_claim_id == rc.id)
    return session.execute(stmt).scalars().first()


def record_outcome(
    session: Session,
    work_item: WorkItem,
    rc: RemittanceClaim,
    payment_date: date | None = None,
    *,
    match: str | None = None,
    actor_id: str = OUTCOMES_ACTOR,
) -> Outcome:
    """Record the outcome of `rc` for `work_item`; returns the existing row when already recorded.

    Closes the work item, updates the claim status and appends an `outcome_recorded` audit event.
    """
    if work_item.id is None or rc.id is None:
        session.flush()
    existing = existing_outcome(session, work_item, rc)
    if existing is not None:
        return existing

    if payment_date is None and rc.remittance is not None:
        payment_date = rc.remittance.payment_date

    result, details = classify_outcome(rc, work_item.dollar_value)
    details["match"] = match or ("claim_id" if rc.claim_id == work_item.claim_id else "submitted_ccn")
    details["remittance_id"] = rc.remittance_id
    now = datetime.now(UTC)
    paid = Decimal(rc.paid_amount or 0)

    outcome = Outcome(
        work_item=work_item,
        remittance_claim=rc,
        result=str(result),
        paid_amount=paid,
        payment_date=payment_date,
        matched_at=now,
        details=details,
    )
    session.add(outcome)

    work_item.status = str(WorkItemStatus.CLOSED)
    work_item.closed_at = now
    claim_status = str(CLAIM_STATUS_FOR_RESULT[result])
    for claim in _claims_for(work_item, rc):
        claim.status = claim_status
    if rc.claim_id is None:
        # Matched through the submitted control number: link the CLP to the claim it paid.
        rc.claim_id = work_item.claim_id
        rc.match_method = rc.match_method or "submitted_ccn"
    session.flush()

    audit.record(
        session,
        actor_type=ActorType.SYSTEM,
        actor_id=actor_id,
        action="outcome_recorded",
        entity_type="work_item",
        entity_id=work_item.id,
        claim_id=work_item.claim_id,
        details={
            "result": str(result),
            "paid_amount": str(paid),
            "remittance_claim_id": rc.id,
            "remittance_id": rc.remittance_id,
            "outcome_id": outcome.id,
            "match": details["match"],
            "reason": details["reason"],
        },
    )
    log.info("outcome %s recorded for work item %s from remittance claim %s", result, work_item.id, rc.id)
    return outcome


def record_outcomes_for_remittance(
    session: Session, remittance: Remittance, *, actor_id: str = OUTCOMES_ACTOR
) -> list[Outcome]:
    """Record outcomes for every CLP in `remittance` that pays, denies or reverses a submitted fix.

    Reversal CLPs (CLP02 22) are evaluated last so that, when the same 835 reverses a payment and
    re-adjudicates the claim, the re-adjudication is the recorded outcome.
    """
    created: list[Outcome] = []
    ordered = sorted(
        remittance.claims,
        key=lambda rc: (rc.claim_status_code == REVERSAL_STATUS_CODE, rc.sequence or 0, rc.id or 0),
    )
    for rc in ordered:
        for work_item, how in candidate_work_items(session, rc):
            if existing_outcome(session, work_item, rc) is not None:
                continue
            created.append(
                record_outcome(
                    session, work_item, rc, payment_date=remittance.payment_date, match=how, actor_id=actor_id
                )
            )
    log.info("remittance %s: %d outcome(s) recorded", remittance.id, len(created))
    return created


def _claims_for(work_item: WorkItem, rc: RemittanceClaim) -> list[Claim]:
    claims: list[Claim] = []
    if work_item.claim is not None:
        claims.append(work_item.claim)
    if rc.claim is not None and all(c.id != rc.claim.id for c in claims):
        claims.append(rc.claim)
    return claims


__all__ = [
    "CLAIM_STATUS_FOR_RESULT",
    "MATCHABLE_STATUSES",
    "PAID_THRESHOLD",
    "candidate_work_items",
    "classify_outcome",
    "existing_outcome",
    "record_outcome",
    "record_outcomes_for_remittance",
]
