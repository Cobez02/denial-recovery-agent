"""Remittance-claim to claim matching (docs/SPEC.md section 6).

Order of attempts, first hit wins and is recorded in ``RemittanceClaim.match_method``:

1. ``ccn``           CLP01 equals ``claims.claim_control_number``
2. ``submitted_ccn`` CLP01 equals a work item's ``submitted_claim_control_number`` (corrected claims)
3. ``payer_ccn``     CLP07 equals the payer claim control number of an already matched remittance claim
4. ``fuzzy``         same member id, statement date inside the claim's service period, charge within 0.01

A fuzzy match is only taken when exactly one claim fits (after preferring the remittance's payer);
an ambiguous candidate set stays unmatched for a person to resolve on the imports page.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Claim, Patient, Remittance, RemittanceClaim, WorkItem

MATCH_CCN = "ccn"
MATCH_SUBMITTED_CCN = "submitted_ccn"
MATCH_PAYER_CCN = "payer_ccn"
MATCH_FUZZY = "fuzzy"

FUZZY_CHARGE_TOLERANCE = Decimal("0.01")


def _by_control_number(session: Session, control_number: str) -> Claim | None:
    stmt = select(Claim).where(Claim.claim_control_number == control_number).order_by(Claim.id)
    return session.execute(stmt).scalars().first()


def _by_submitted_control_number(session: Session, control_number: str) -> Claim | None:
    stmt = (
        select(WorkItem)
        .where(WorkItem.submitted_claim_control_number == control_number)
        .order_by(WorkItem.id.desc())
    )
    work_item = session.execute(stmt).scalars().first()
    return work_item.claim if work_item is not None else None


def _by_payer_control_number(
    session: Session, rc: RemittanceClaim, remittance: Remittance | None
) -> Claim | None:
    payer_ccn = (rc.payer_claim_control_number or "").strip()
    if not payer_ccn:
        return None
    stmt = (
        select(RemittanceClaim)
        .where(
            RemittanceClaim.payer_claim_control_number == payer_ccn,
            RemittanceClaim.claim_id.is_not(None),
        )
        .order_by(RemittanceClaim.id.desc())
    )
    payer_id = remittance.payer_id if remittance is not None else None
    for earlier in session.execute(stmt).scalars():
        if rc.id is not None and earlier.id == rc.id:
            continue
        earlier_payer_id = earlier.remittance.payer_id if earlier.remittance is not None else None
        if payer_id is not None and earlier_payer_id is not None and earlier_payer_id != payer_id:
            continue  # the same number from a different payer is a coincidence, not a match
        if earlier.claim is not None:
            return earlier.claim
    return None


def _statement_date(rc: RemittanceClaim) -> date | None:
    if rc.statement_from is not None:
        return rc.statement_from
    line_dates = [line.service_date for line in rc.lines if line.service_date is not None]
    return min(line_dates) if line_dates else None


def _fuzzy(session: Session, rc: RemittanceClaim, remittance: Remittance | None) -> Claim | None:
    member_id = (rc.member_id or "").strip()
    statement_date = _statement_date(rc)
    if not member_id or statement_date is None:
        return None
    charge = Decimal(rc.charge_amount or 0)
    stmt = select(Claim).join(Patient, Claim.patient_id == Patient.id).where(Patient.member_id == member_id)
    candidates: list[Claim] = []
    for claim in session.execute(stmt).scalars():
        if claim.service_date_from is None or claim.service_date_to is None:
            continue
        if not (claim.service_date_from <= statement_date <= claim.service_date_to):
            continue
        if abs(Decimal(claim.total_charge or 0) - charge) > FUZZY_CHARGE_TOLERANCE:
            continue
        candidates.append(claim)
    if len(candidates) > 1 and remittance is not None and remittance.payer_id is not None:
        same_payer = [c for c in candidates if c.payer_id == remittance.payer_id]
        if same_payer:
            candidates = same_payer
    if len(candidates) != 1:
        return None
    return candidates[0]


def match_remittance_claim(
    session: Session, rc: RemittanceClaim, *, remittance: Remittance | None = None
) -> Claim | None:
    """Link ``rc`` to a claim, set ``rc.claim_id``/``rc.match_method``, and return the claim (or None)."""
    remittance = remittance if remittance is not None else rc.remittance
    control_number = (rc.patient_control_number or "").strip()

    strategies: list[tuple[str, Callable[[], Claim | None]]] = []
    if control_number:
        strategies.append((MATCH_CCN, lambda: _by_control_number(session, control_number)))
        strategies.append(
            (MATCH_SUBMITTED_CCN, lambda: _by_submitted_control_number(session, control_number))
        )
    strategies.append((MATCH_PAYER_CCN, lambda: _by_payer_control_number(session, rc, remittance)))
    strategies.append((MATCH_FUZZY, lambda: _fuzzy(session, rc, remittance)))

    for method, lookup in strategies:
        claim = lookup()
        if claim is not None:
            rc.claim = claim
            rc.claim_id = claim.id
            rc.match_method = method
            return claim

    rc.claim = None
    rc.claim_id = None
    rc.match_method = None
    return None


__all__ = [
    "FUZZY_CHARGE_TOLERANCE",
    "MATCH_CCN",
    "MATCH_FUZZY",
    "MATCH_PAYER_CCN",
    "MATCH_SUBMITTED_CCN",
    "match_remittance_claim",
]
