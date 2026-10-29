"""Outcome matching (milestone M8): close the loop on submitted fixes.

A later 835 tells us what the payer did with a corrected claim or appeal the clinic submitted.
For every CLP in that remittance we look for work items in status `submitted`, either on the same
claim or carrying the CLP's patient control number as their submitted (corrected) claim control
number, and record one `outcomes` row per (work item, CLP):

    reversed      CLP02 == 22 (reversal of a previous payment). The work item stays submitted so the
                  re-adjudication a later 835 brings is recorded as well.
    denied_again  nothing paid: denial adjustments present (reason "denial") or simply zero paid
                  (reason "zero_paid")
    paid          no denial adjustment remains on the claim or on the lines the work item disputed
                  (only contractual/informational CARCs such as CO-45 or PR-1/2/3 are left, or
                  PR-group patient-responsibility CARCs such as PR-96 that app.ingest.denials does
                  not count as a denial either: an overturned denial paid at the contracted rate is
                  a win), or the payment reaches 95% of the work item's dollar value
    partial       something paid, but a denial adjustment remains on a disputed line or on the claim
                  and the payment stays below 95% of the dollar value

A line-level denial counts as remaining when it falls on a disputed procedure code; the SVC line
number decides only when the adjustment or the disputed scope carries no procedure code, because
payers do not keep SVC order between adjudications. paid, partial and denied_again close the work
item and set the claim status (paid, partial, denied); a reversal leaves the claim status as the
submission left it (corrected or appealed) until the re-adjudication arrives. An `outcome_recorded`
audit event is appended for every outcome. A CLP answers a work item only when its remittance is
dated (payment date, else production date, else the day it was received) on or after the day the
fix was submitted and, for a same-claim match, comes from the claim's payer or from a payer the
file did not name; a CLP that carries the corrected claim's own control number is accepted from
any payer, because a corrected claim may have been redirected to the payer a letter named. Older
or foreign CLPs are skipped and logged by id. Everything is idempotent: re-running on the same
remittance changes nothing. A work item's own originating remittance claim never counts as its
outcome.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app import audit
from app.enums import ActorType, ClaimStatus, OutcomeResult, WorkItemStatus
from app.ingest.denials import (
    DENIED_CLAIM_STATUS,
    AdjustmentInput,
    adjustment_inputs_for,
    is_patient_responsibility_adjustment,
)
from app.models import Claim, Outcome, Remittance, RemittanceClaim, WorkItem

log = logging.getLogger(__name__)

OUTCOMES_ACTOR = "outcomes"
PAID_THRESHOLD = Decimal("0.95")
REVERSAL_STATUS_CODE = "22"
MATCHABLE_STATUSES: tuple[str, ...] = (str(WorkItemStatus.SUBMITTED),)
# Results that end the work item; a reversal leaves it submitted (see the module docstring).
CLOSING_RESULTS: frozenset[OutcomeResult] = frozenset(
    {OutcomeResult.PAID, OutcomeResult.PARTIAL, OutcomeResult.DENIED_AGAIN}
)
MATCH_CLAIM_ID = "claim_id"
MATCH_SUBMITTED_CCN = "submitted_ccn"
# Why a candidate work item was not given an outcome (logged with the ids, stored nowhere).
SKIP_BEFORE_SUBMISSION = "remittance_before_submission"
SKIP_OTHER_PAYER = "other_payer"

# Claim status a closing result sets. A reversal is not in the map: the claim keeps the status the
# submission gave it (corrected or appealed) while the fix is still in flight.
CLAIM_STATUS_FOR_RESULT: dict[OutcomeResult, ClaimStatus] = {
    OutcomeResult.PAID: ClaimStatus.PAID,
    OutcomeResult.PARTIAL: ClaimStatus.PARTIAL,
    OutcomeResult.DENIED_AGAIN: ClaimStatus.DENIED,
}

_CENTS = Decimal("0.01")


def paid_threshold(dollar_value: Decimal | None) -> Decimal:
    """95% of the dollar value, rounded to the cent once: the value compared and the value recorded."""
    return (PAID_THRESHOLD * Decimal(dollar_value or 0)).quantize(_CENTS, rounding=ROUND_HALF_UP)


def disputed_scope(reasons: Iterable[Any] | None) -> tuple[set[int], set[str]] | None:
    """The line numbers and procedure codes a work item's stored reasons dispute.

    Returns None when the whole claim is disputed: no reasons at all, a claim-level reason, or a
    line-level reason that names no line.
    """
    lines: set[int] = set()
    procedures: set[str] = set()
    for reason in reasons or []:
        if not isinstance(reason, Mapping):
            return None
        if str(reason.get("level") or "claim") != "line":
            return None
        number = reason.get("line_number")
        if number is None:
            return None
        try:
            lines.add(int(number))
        except (TypeError, ValueError):
            return None
        procedure = reason.get("procedure_code")
        if procedure:
            procedures.add(str(procedure))
    if not lines:
        return None
    return lines, procedures


def on_disputed_lines(adjustment: AdjustmentInput, lines: set[int], procedures: set[str]) -> bool:
    """Whether a line-level adjustment falls on what the work item disputed.

    The procedure code decides whenever both the adjustment and the disputed scope know one: payers
    do not keep SVC order between adjudications, so a line number is trusted only when either side
    carries no procedure code. An adjustment that names neither counts (nothing places it elsewhere).
    """
    procedure = adjustment.procedure_code
    if procedure and procedures:
        return procedure in procedures
    if adjustment.line_number is not None:
        return adjustment.line_number in lines
    return not procedure


def remaining_denial_codes(rc: RemittanceClaim, reasons: Iterable[Any] | None = None) -> list[str]:
    """Codes of the denial adjustments on `rc` that fall on the claim or the lines `reasons` disputed.

    `reasons` is the work item's stored `all_reasons`. Claim-level reasons (or none) dispute the
    whole claim, so every denial adjustment counts; line-level reasons dispute their procedure codes
    and line numbers (:func:`on_disputed_lines`), so a new denial on a line the fix never touched is
    left to the pipeline (it becomes a work item of its own) and does not make the fix's outcome
    partial. PR-group patient-responsibility CARCs (PR-96, PR-204, PR-49, PR-119) are the patient's
    share, not a denial, exactly as in app.ingest.denials, so the outcome agrees with the claim
    status the importer sets for the same CLP. A CLP02 4 or a row flagged as a denial without any
    adjustment rows is a claim-level denial. Codes look like "CO-97" (claim level) or "CO-4@2"
    (line 2); nothing in them identifies a patient.
    """
    denials = [
        adjustment
        for adjustment in adjustment_inputs_for(rc, denials_only=True)
        if not is_patient_responsibility_adjustment(adjustment.group_code, adjustment.carc)
    ]
    if not denials:
        if (rc.claim_status_code or "").strip() == DENIED_CLAIM_STATUS:
            return [f"CLP02-{DENIED_CLAIM_STATUS}"]
        return ["denial"] if rc.is_denial else []
    scope = disputed_scope(reasons)
    codes: list[str] = []
    for adjustment in denials:
        if scope is not None and adjustment.level == "line" and not on_disputed_lines(adjustment, *scope):
            continue
        code = f"{adjustment.group_code}-{adjustment.carc}"
        if adjustment.level == "line" and adjustment.line_number is not None:
            code = f"{code}@{adjustment.line_number}"
        codes.append(code)
    return codes


def classify_outcome(
    rc: RemittanceClaim, dollar_value: Decimal | None, *, reasons: Iterable[Any] | None = None
) -> tuple[OutcomeResult, dict[str, Any]]:
    """Decide the outcome of one CLP for a work item worth `dollar_value` (docs/SPEC.md section 10).

    `reasons` are the work item's stored denial reasons; they scope which remaining denial
    adjustments count (see :func:`remaining_denial_codes`). Without them the whole claim counts.
    """
    paid = Decimal(rc.paid_amount or 0)
    value = Decimal(dollar_value or 0)
    threshold = paid_threshold(value)
    remaining = remaining_denial_codes(rc, reasons)
    details: dict[str, Any] = {
        "paid_amount": str(paid),
        "dollar_value": str(value),
        "paid_threshold": str(threshold),
        "claim_status_code": rc.claim_status_code,
        "is_denial": bool(rc.is_denial),
        "remaining_denials": remaining,
    }
    if rc.claim_status_code == REVERSAL_STATUS_CODE:
        result, reason = OutcomeResult.REVERSED, "reversal"
    elif paid <= 0:
        result, reason = (
            OutcomeResult.DENIED_AGAIN,
            ("denial" if (rc.is_denial or remaining) else "zero_paid"),
        )
    elif not remaining:
        result, reason = OutcomeResult.PAID, "no_denial_remaining"
    elif value > 0 and paid >= threshold:
        result, reason = OutcomeResult.PAID, "paid_at_least_95pct"
    else:
        result, reason = OutcomeResult.PARTIAL, "denial_remains"
    details["reason"] = reason
    return result, details


def remittance_date(remittance: Remittance | None) -> date | None:
    """When the payer adjudicated: BPR16, else the production date, else the day the file arrived."""
    if remittance is None:
        return None
    if remittance.payment_date is not None:
        return remittance.payment_date
    if remittance.production_date is not None:
        return remittance.production_date
    received = remittance.received_at
    return received.date() if received is not None else None


def names_corrected_claim(work_item: WorkItem, rc: RemittanceClaim) -> bool:
    """CLP01 is the control number the clinic gave the corrected claim (not the original claim's)."""
    pcn = (rc.patient_control_number or "").strip()
    submitted = (work_item.submitted_claim_control_number or "").strip()
    original = work_item.claim.claim_control_number if work_item.claim is not None else None
    return bool(pcn) and submitted == pcn and submitted != original


def skip_reason(work_item: WorkItem, rc: RemittanceClaim) -> str | None:
    """Why `rc` cannot be the outcome of `work_item`, or None when it can.

    A remittance dated before the submission cannot answer the fix (backfilled or re-delivered
    835s). A same-claim CLP from another payer is a coincidence (a secondary payer's remittance,
    say), unless it names the corrected claim's own control number: that claim may have been sent
    to the payer a letter named. A remittance whose payer is unknown is accepted.
    """
    remittance = rc.remittance
    dated = remittance_date(remittance)
    if dated is not None and work_item.submitted_at is not None and dated < work_item.submitted_at.date():
        return SKIP_BEFORE_SUBMISSION
    remittance_payer_id = remittance.payer_id if remittance is not None else None
    claim_payer_id = work_item.claim.payer_id if work_item.claim is not None else None
    if (
        remittance_payer_id is not None
        and claim_payer_id is not None
        and remittance_payer_id != claim_payer_id
        and not names_corrected_claim(work_item, rc)
    ):
        return SKIP_OTHER_PAYER
    return None


def candidate_work_items(session: Session, rc: RemittanceClaim) -> list[tuple[WorkItem, str]]:
    """Submitted work items this CLP can answer, with how each matched.

    Match methods: "claim_id" (same claim) or "submitted_ccn" (CLP01 equals the control number of
    the corrected claim the clinic submitted for the work item). Work items the remittance cannot
    answer (see :func:`skip_reason`) are left out and logged by id.
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
        skipped = skip_reason(work_item, rc)
        if skipped is not None:
            log.info("remittance claim %s skipped for work item %s: %s", rc.id, work_item.id, skipped)
            continue
        how = (
            MATCH_CLAIM_ID
            if rc.claim_id is not None and work_item.claim_id == rc.claim_id
            else MATCH_SUBMITTED_CCN
        )
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

    Closes the work item and sets the claim status unless the result is a reversal (the item stays
    submitted and the claim keeps the status its submission gave it), then appends an
    `outcome_recorded` audit event. The caller decides whether `rc` may answer the work item
    (:func:`candidate_work_items` does); this records what it is given.
    """
    if work_item.id is None or rc.id is None:
        session.flush()
    existing = existing_outcome(session, work_item, rc)
    if existing is not None:
        return existing

    if payment_date is None and rc.remittance is not None:
        payment_date = rc.remittance.payment_date

    result, details = classify_outcome(rc, work_item.dollar_value, reasons=work_item.all_reasons)
    details["match"] = match or (MATCH_CLAIM_ID if rc.claim_id == work_item.claim_id else MATCH_SUBMITTED_CCN)
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

    closes = result in CLOSING_RESULTS
    if closes:
        work_item.status = str(WorkItemStatus.CLOSED)
        work_item.closed_at = now
    claim_status = CLAIM_STATUS_FOR_RESULT.get(result)
    if claim_status is not None:
        for claim in _claims_for(work_item, rc):
            claim.status = str(claim_status)
    if rc.claim_id is None:
        # Matched through the submitted control number: link the CLP to the claim it paid.
        rc.claim_id = work_item.claim_id
        rc.match_method = rc.match_method or MATCH_SUBMITTED_CCN
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
            "remaining_denials": details["remaining_denials"],
            "work_item_status": str(work_item.status),
        },
    )
    log.info(
        "outcome %s recorded for work item %s from remittance claim %s (%s)",
        result,
        work_item.id,
        rc.id,
        "closed" if closes else "still submitted",
    )
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
    "CLOSING_RESULTS",
    "MATCHABLE_STATUSES",
    "PAID_THRESHOLD",
    "SKIP_BEFORE_SUBMISSION",
    "SKIP_OTHER_PAYER",
    "candidate_work_items",
    "classify_outcome",
    "disputed_scope",
    "existing_outcome",
    "names_corrected_claim",
    "on_disputed_lines",
    "paid_threshold",
    "record_outcome",
    "record_outcomes_for_remittance",
    "remaining_denial_codes",
    "remittance_date",
    "skip_reason",
]
