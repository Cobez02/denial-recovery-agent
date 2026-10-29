"""Outcome matching (milestone M8): later 835s close submitted fixes."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from itertools import count

import pytest
from sqlalchemy import func, select

from app import audit
from app.models import (
    AuditLog,
    Claim,
    ClaimLine,
    Outcome,
    Patient,
    Payer,
    Remittance,
    RemittanceClaim,
    WorkItem,
)
from app.outcomes import record_outcome, record_outcomes_for_remittance
from app.outcomes.matching import candidate_work_items, classify_outcome

_seq = count(1)


@pytest.fixture()
def payer(session):
    p = Payer(name="Synthetic Health Plan", payer_id="SYN001")
    session.add(p)
    session.flush()
    return p


@pytest.fixture()
def patient(session, clinic):
    p = Patient(
        clinic_id=clinic.id,
        token="PT-TEST01",
        account_number="ACC-0001",
        first_name="Pat",
        last_name="Synthetic",
        dob=date(1980, 1, 1),
    )
    session.add(p)
    session.flush()
    return p


def make_claim(session, clinic, payer, patient, ccn, *, total="100.00", frequency_code="1", original=None):
    claim = Claim(
        clinic_id=clinic.id,
        payer_id=payer.id,
        patient_id=patient.id,
        claim_control_number=ccn,
        frequency_code=frequency_code,
        original_claim_id=original.id if original is not None else None,
        total_charge=Decimal(total),
        status="denied",
        service_date_from=date(2026, 6, 1),
        service_date_to=date(2026, 6, 1),
    )
    session.add(claim)
    session.flush()
    session.add(
        ClaimLine(
            claim_id=claim.id,
            line_number=1,
            service_date=date(2026, 6, 1),
            procedure_code="99213",
            charge_amount=Decimal(total),
        )
    )
    session.flush()
    return claim


def make_remittance(session, payer, *, payment_date=date(2026, 8, 15)):
    n = next(_seq)
    remittance = Remittance(
        payer_id=payer.id,
        file_name=f"remit_{n}.835",
        file_sha256=f"{n:064x}",
        trace_number=f"TRN{n}",
        payment_amount=Decimal("0"),
        payment_date=payment_date,
    )
    session.add(remittance)
    session.flush()
    return remittance


def make_rc(
    session, remittance, *, pcn, claim=None, status_code="1", charge="100.00", paid="0.00", is_denial=False
):
    rc = RemittanceClaim(
        remittance=remittance,
        claim=claim,
        sequence=len(remittance.claims) + 1,
        patient_control_number=pcn,
        claim_status_code=status_code,
        charge_amount=Decimal(charge),
        paid_amount=Decimal(paid),
        patient_responsibility=Decimal("0"),
        is_denial=is_denial,
        denied_amount=Decimal(charge) if is_denial else Decimal("0"),
        match_method="exact_ccn" if claim is not None else None,
    )
    session.add(rc)
    session.flush()
    return rc


def make_work_item(session, claim, rc, *, status="submitted", dollar_value="100.00", submitted_ccn=None):
    now = datetime.now(UTC)
    work_item = WorkItem(
        claim=claim,
        remittance_claim=rc,
        category="modifier",
        category_confidence=0.9,
        classification_source="rules",
        status=status,
        dollar_value=Decimal(dollar_value),
        decided_at=now,
        submitted_at=now if status in ("submitted", "closed") else None,
        submitted_claim_control_number=submitted_ccn,
    )
    session.add(work_item)
    session.flush()
    return work_item


@pytest.fixture()
def submitted_item(session, clinic, payer, patient):
    """A denied claim whose corrected claim (CLM-0001-R1) the clinic has submitted."""
    claim = make_claim(session, clinic, payer, patient, "CLM-0001")
    denial = make_remittance(session, payer, payment_date=date(2026, 7, 1))
    denial_rc = make_rc(
        session, denial, pcn="CLM-0001", claim=claim, status_code="4", paid="0.00", is_denial=True
    )
    work_item = make_work_item(session, claim, denial_rc, submitted_ccn="CLM-0001-R1")
    session.commit()
    return claim, denial, denial_rc, work_item


def _outcome_events(session, work_item_id):
    stmt = select(AuditLog).where(AuditLog.action == "outcome_recorded", AuditLog.entity_id == work_item_id)
    return list(session.execute(stmt).scalars())


def _outcome_count(session):
    return session.execute(select(func.count(Outcome.id))).scalar_one()


def test_paid_outcome_closes_work_item_and_claim(session, payer, submitted_item):
    claim, _denial, _denial_rc, work_item = submitted_item
    followup = make_remittance(session, payer, payment_date=date(2026, 8, 15))
    rc = make_rc(session, followup, pcn="CLM-0001-R1", claim=claim, status_code="1", paid="96.00")

    outcomes = record_outcomes_for_remittance(session, followup)

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.result == "paid"
    assert outcome.paid_amount == Decimal("96.00")
    assert outcome.payment_date == date(2026, 8, 15)
    assert (outcome.work_item_id, outcome.remittance_claim_id) == (work_item.id, rc.id)
    assert outcome.details["reason"] == "paid_at_least_95pct"
    assert outcome.details["match"] == "claim_id"
    assert work_item.status == "closed" and work_item.closed_at is not None
    assert claim.status == "paid"

    events = _outcome_events(session, work_item.id)
    assert len(events) == 1
    event = events[0]
    assert (event.actor_type, event.actor_id, event.entity_type) == ("system", "outcomes", "work_item")
    assert event.claim_id == claim.id
    assert event.details["result"] == "paid"
    assert event.details["paid_amount"] == "96.00"
    assert event.details["remittance_claim_id"] == rc.id
    assert audit.verify_chain(session) is None


def test_partial_payment(session, payer, submitted_item):
    claim, _, _, work_item = submitted_item
    followup = make_remittance(session, payer)
    make_rc(session, followup, pcn="CLM-0001-R1", claim=claim, status_code="1", paid="50.00")
    [outcome] = record_outcomes_for_remittance(session, followup)
    assert outcome.result == "partial"
    assert outcome.details["reason"] == "paid_below_95pct"
    assert work_item.status == "closed"
    assert claim.status == "partial"


def test_denied_again_with_denial_adjustments(session, payer, submitted_item):
    claim, _, _, work_item = submitted_item
    followup = make_remittance(session, payer)
    make_rc(session, followup, pcn="CLM-0001-R1", claim=claim, status_code="4", paid="0.00", is_denial=True)
    [outcome] = record_outcomes_for_remittance(session, followup)
    assert outcome.result == "denied_again"
    assert outcome.details["reason"] == "denial"
    assert outcome.paid_amount == Decimal("0.00")
    assert work_item.status == "closed" and claim.status == "denied"


def test_zero_paid_without_denial_adjustments(session, payer, submitted_item):
    claim, _, _, _work_item = submitted_item
    followup = make_remittance(session, payer)
    make_rc(session, followup, pcn="CLM-0001-R1", claim=claim, status_code="1", paid="0.00", is_denial=False)
    [outcome] = record_outcomes_for_remittance(session, followup)
    assert outcome.result == "denied_again"
    assert outcome.details["reason"] == "zero_paid"
    assert claim.status == "denied"


def test_reversal(session, payer, submitted_item):
    claim, _, _, work_item = submitted_item
    followup = make_remittance(session, payer)
    make_rc(session, followup, pcn="CLM-0001-R1", claim=claim, status_code="22", paid="-100.00")
    [outcome] = record_outcomes_for_remittance(session, followup)
    assert outcome.result == "reversed"
    assert outcome.details["reason"] == "reversal"
    assert work_item.status == "closed" and claim.status == "denied"


def test_threshold_is_95_percent_of_dollar_value():
    rc = RemittanceClaim(patient_control_number="X", claim_status_code="1", paid_amount=Decimal("95.00"))
    assert classify_outcome(rc, Decimal("100.00"))[0] == "paid"
    rc.paid_amount = Decimal("94.99")
    assert classify_outcome(rc, Decimal("100.00"))[0] == "partial"
    rc.paid_amount = Decimal("0.00")
    result, details = classify_outcome(rc, Decimal("100.00"))
    assert result == "denied_again" and details["paid_threshold"] == "95.00"
    rc.claim_status_code = "22"
    assert classify_outcome(rc, Decimal("100.00"))[0] == "reversed"


def test_match_via_submitted_control_number_on_corrected_claim_row(
    session, clinic, payer, patient, submitted_item
):
    claim, _, _, work_item = submitted_item
    corrected = make_claim(session, clinic, payer, patient, "CLM-0001-R1", frequency_code="7", original=claim)
    followup = make_remittance(session, payer)
    rc = make_rc(session, followup, pcn="CLM-0001-R1", claim=corrected, status_code="1", paid="100.00")

    assert [(wi.id, how) for wi, how in candidate_work_items(session, rc)] == [
        (work_item.id, "submitted_ccn")
    ]
    [outcome] = record_outcomes_for_remittance(session, followup)
    assert outcome.result == "paid"
    assert outcome.details["match"] == "submitted_ccn"
    assert work_item.status == "closed"
    assert claim.status == "paid" and corrected.status == "paid"


def test_match_unlinked_clp_by_submitted_control_number(session, payer, submitted_item):
    claim, _, _, work_item = submitted_item
    followup = make_remittance(session, payer)
    rc = make_rc(session, followup, pcn="CLM-0001-R1", claim=None, status_code="1", paid="100.00")
    [outcome] = record_outcomes_for_remittance(session, followup)
    assert outcome.work_item_id == work_item.id
    assert rc.claim_id == claim.id and rc.match_method == "submitted_ccn"


def test_second_run_is_idempotent(session, payer, submitted_item):
    claim, _, _, work_item = submitted_item
    followup = make_remittance(session, payer)
    make_rc(session, followup, pcn="CLM-0001-R1", claim=claim, status_code="1", paid="100.00")
    first = record_outcomes_for_remittance(session, followup)
    session.commit()
    second = record_outcomes_for_remittance(session, followup)
    assert len(first) == 1 and second == []
    assert _outcome_count(session) == 1
    assert len(_outcome_events(session, work_item.id)) == 1
    assert audit.verify_chain(session) is None


def test_originating_denial_is_not_an_outcome(session, submitted_item):
    _, denial, _, work_item = submitted_item
    assert record_outcomes_for_remittance(session, denial) == []
    assert work_item.status == "submitted"
    assert _outcome_count(session) == 0


@pytest.mark.parametrize("status", ["new", "proposed", "in_review", "approved", "rejected", "closed"])
def test_only_submitted_or_exported_items_match(session, clinic, payer, patient, status):
    claim = make_claim(session, clinic, payer, patient, f"CLM-{status}")
    denial = make_remittance(session, payer)
    denial_rc = make_rc(
        session, denial, pcn=claim.claim_control_number, claim=claim, status_code="4", is_denial=True
    )
    work_item = make_work_item(session, claim, denial_rc, status=status)
    followup = make_remittance(session, payer)
    make_rc(session, followup, pcn=claim.claim_control_number, claim=claim, status_code="1", paid="100.00")
    assert record_outcomes_for_remittance(session, followup) == []
    assert work_item.status == status
    assert _outcome_count(session) == 0


def test_exported_items_match(session, clinic, payer, patient):
    claim = make_claim(session, clinic, payer, patient, "CLM-EXP")
    denial = make_remittance(session, payer)
    denial_rc = make_rc(session, denial, pcn="CLM-EXP", claim=claim, status_code="4", is_denial=True)
    work_item = make_work_item(session, claim, denial_rc, status="exported")
    followup = make_remittance(session, payer)
    make_rc(session, followup, pcn="CLM-EXP", claim=claim, status_code="1", paid="100.00")
    [outcome] = record_outcomes_for_remittance(session, followup)
    assert outcome.result == "paid" and work_item.status == "closed"


def test_record_outcome_directly_is_idempotent(session, payer, submitted_item):
    claim, _, _, work_item = submitted_item
    followup = make_remittance(session, payer, payment_date=date(2026, 9, 1))
    rc = make_rc(session, followup, pcn="CLM-0001-R1", claim=claim, status_code="1", paid="100.00")
    outcome = record_outcome(session, work_item, rc)
    assert outcome.payment_date == date(2026, 9, 1)  # taken from the remittance when not given
    assert outcome.result == "paid" and work_item.status == "closed"
    again = record_outcome(session, work_item, rc)
    assert again.id == outcome.id
    assert _outcome_count(session) == 1
    assert len(_outcome_events(session, work_item.id)) == 1


def test_record_outcome_explicit_payment_date(session, clinic, payer, patient):
    claim = make_claim(session, clinic, payer, patient, "CLM-DATE")
    denial = make_remittance(session, payer)
    denial_rc = make_rc(session, denial, pcn="CLM-DATE", claim=claim, status_code="4", is_denial=True)
    work_item = make_work_item(session, claim, denial_rc)
    followup = make_remittance(session, payer, payment_date=date(2026, 9, 1))
    rc = make_rc(session, followup, pcn="CLM-DATE", claim=claim, paid="100.00")
    outcome = record_outcome(session, work_item, rc, payment_date=date(2026, 9, 3))
    assert outcome.payment_date == date(2026, 9, 3)


def test_reversal_and_readjudication_in_same_remittance(session, payer, submitted_item):
    claim, _, _, _work_item = submitted_item
    followup = make_remittance(session, payer)
    make_rc(session, followup, pcn="CLM-0001-R1", claim=claim, status_code="22", paid="-100.00")
    make_rc(session, followup, pcn="CLM-0001-R1", claim=claim, status_code="1", paid="100.00")
    outcomes = record_outcomes_for_remittance(session, followup)
    assert [o.result for o in outcomes] == ["paid"]
    assert claim.status == "paid"
