"""Outcome matching (milestone M8): later 835s answer submitted fixes, and take back their payments.

Every fixture work item was submitted on 2026-07-15 (noon UTC); the denial that created it is
dated 2026-07-01 and follow-up remittances 2026-08-15 unless a test says otherwise. The take-back
tests pay a fix on 2026-08-01, reverse the payment on 2026-08-20 and pay it again on 2026-09-10.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from decimal import Decimal
from itertools import count

import pytest
from sqlalchemy import func, select

from app import audit
from app.ingest.denials import detect_for, is_denial_adjustment
from app.ingest.remit_import import claim_status_for
from app.models import (
    Adjustment,
    AuditLog,
    Claim,
    ClaimLine,
    Outcome,
    Patient,
    Payer,
    Proposal,
    Remittance,
    RemittanceClaim,
    RemittanceLine,
    WorkItem,
)
from app.outcomes import compute_dashboard, record_outcome, record_outcomes_for_remittance
from app.outcomes.matching import (
    SKIP_BEFORE_OUTCOME,
    SKIP_BEFORE_SUBMISSION,
    SKIP_OTHER_PAYER,
    candidate_work_items,
    classify_outcome,
    latest_outcome,
    paid_threshold,
    remaining_denial_codes,
    takeback_candidates,
)

_seq = count(1)

DENIAL_DATE = date(2026, 7, 1)
SUBMITTED_AT = datetime(2026, 7, 15, 12, 0, tzinfo=UTC)
FOLLOWUP_DATE = date(2026, 8, 15)
LOGGER = "app.outcomes.matching"


@pytest.fixture()
def payer(session):
    p = Payer(name="Synthetic Health Plan", payer_id="SYN001")
    session.add(p)
    session.flush()
    return p


@pytest.fixture()
def other_payer(session):
    p = Payer(name="Other Synthetic Plan", payer_id="SYN002")
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


def make_remittance(session, payer, *, payment_date=FOLLOWUP_DATE, production_date=None, received_at=None):
    """A remittance from `payer` (None: the file named no payer we know)."""
    n = next(_seq)
    remittance = Remittance(
        payer_id=payer.id if payer is not None else None,
        file_name=f"remit_{n}.835",
        file_sha256=f"{n:064x}",
        trace_number=f"TRN{n}",
        payment_amount=Decimal("0"),
        payment_date=payment_date,
        production_date=production_date,
    )
    if received_at is not None:
        remittance.received_at = received_at
    session.add(remittance)
    session.flush()
    return remittance


def make_rc(
    session,
    remittance,
    *,
    pcn,
    claim=None,
    status_code="1",
    charge="100.00",
    paid="0.00",
    is_denial=None,
    adjustments=(),
):
    """One CLP. `adjustments` are (group, carc, amount) claim-level or (group, carc, amount,
    line_number, procedure_code) line-level CAS; `is_denial` defaults to what denial detection says."""
    rc = RemittanceClaim(
        remittance=remittance,
        claim=claim,
        sequence=len(remittance.claims) + 1,
        patient_control_number=pcn,
        claim_status_code=status_code,
        charge_amount=Decimal(charge),
        paid_amount=Decimal(paid),
        patient_responsibility=Decimal("0"),
        is_denial=False,
        denied_amount=Decimal("0"),
        match_method="exact_ccn" if claim is not None else None,
    )
    lines: dict[int, RemittanceLine] = {}
    for spec in adjustments:
        group, carc, amount = spec[0], spec[1], Decimal(str(spec[2]))
        row = Adjustment(
            level="claim",
            group_code=group,
            carc=carc,
            amount=amount,
            is_denial=is_denial_adjustment(group, carc, amount),
        )
        if len(spec) > 3:
            number, procedure = int(spec[3]), spec[4]
            line = lines.get(number)
            if line is None:
                line = RemittanceLine(line_number=number, procedure_code=procedure, charge_amount=amount)
                lines[number] = line
                rc.lines.append(line)
            row.level = "line"
            row.remittance_line = line
        rc.adjustments.append(row)
    if is_denial is None:
        is_denial, denied_amount, _reasons = detect_for(rc)
    else:
        denied_amount = Decimal(charge) if is_denial else Decimal("0")
    rc.is_denial = bool(is_denial)
    rc.denied_amount = denied_amount
    session.add(rc)
    session.flush()
    return rc


def make_work_item(
    session,
    claim,
    rc,
    *,
    status="submitted",
    dollar_value="100.00",
    submitted_ccn=None,
    reasons=None,
    submitted_at=SUBMITTED_AT,
):
    work_item = WorkItem(
        claim=claim,
        remittance_claim=rc,
        category="modifier",
        category_confidence=0.9,
        classification_source="rules",
        all_reasons=list(reasons or []),
        status=status,
        dollar_value=Decimal(dollar_value),
        decided_at=SUBMITTED_AT,
        submitted_at=submitted_at if status in ("submitted", "closed") else None,
        submitted_claim_control_number=submitted_ccn,
    )
    session.add(work_item)
    session.flush()
    return work_item


@pytest.fixture()
def submitted_item(session, clinic, payer, patient):
    """A denied claim whose corrected claim (CLM-0001-R1) the clinic has submitted."""
    claim = make_claim(session, clinic, payer, patient, "CLM-0001")
    denial = make_remittance(session, payer, payment_date=DENIAL_DATE)
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


def _classified(status_code="1", paid="0.00", *, is_denial=False):
    return RemittanceClaim(
        patient_control_number="X",
        claim_status_code=status_code,
        paid_amount=Decimal(paid),
        is_denial=is_denial,
    )


# --------------------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------------------


def test_paid_outcome_closes_work_item_and_claim(session, payer, submitted_item):
    claim, _denial, _denial_rc, work_item = submitted_item
    followup = make_remittance(session, payer, payment_date=FOLLOWUP_DATE)
    rc = make_rc(session, followup, pcn="CLM-0001-R1", claim=claim, status_code="1", paid="96.00")

    outcomes = record_outcomes_for_remittance(session, followup)

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.result == "paid"
    assert outcome.paid_amount == Decimal("96.00")
    assert outcome.payment_date == FOLLOWUP_DATE
    assert (outcome.work_item_id, outcome.remittance_claim_id) == (work_item.id, rc.id)
    assert outcome.details["reason"] == "no_denial_remaining"
    assert outcome.details["remaining_denials"] == []
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
    assert event.details["work_item_status"] == "closed"
    assert audit.verify_chain(session) is None


def test_overturned_denial_paid_at_the_contracted_rate_is_paid(session, payer, submitted_item):
    """A full-charge denial (dollar value = charge) overturned and paid at the allowed amount with a
    CO-45 write-off is a win, although the payment is below 95% of the dollar value."""
    claim, _, _, work_item = submitted_item
    followup = make_remittance(session, payer)
    rc = make_rc(
        session, followup, pcn="CLM-0001-R1", claim=claim, paid="72.00", adjustments=[("CO", "45", "28.00")]
    )
    assert rc.is_denial is False
    [outcome] = record_outcomes_for_remittance(session, followup)
    assert outcome.result == "paid"
    assert outcome.details["reason"] == "no_denial_remaining"
    assert Decimal(outcome.paid_amount) < paid_threshold(work_item.dollar_value) == Decimal("95.00")
    assert work_item.status == "closed" and claim.status == "paid"


def test_partial_payment_when_a_denial_remains(session, payer, submitted_item):
    claim, _, _, work_item = submitted_item
    followup = make_remittance(session, payer)
    make_rc(
        session, followup, pcn="CLM-0001-R1", claim=claim, paid="50.00", adjustments=[("CO", "16", "50.00")]
    )
    [outcome] = record_outcomes_for_remittance(session, followup)
    assert outcome.result == "partial"
    assert outcome.details["reason"] == "denial_remains"
    assert outcome.details["remaining_denials"] == ["CO-16"]
    assert work_item.status == "closed"
    assert claim.status == "partial"


def test_contractual_and_patient_responsibility_splits_are_not_remaining_denials(
    session, payer, submitted_item
):
    claim, _, _, work_item = submitted_item
    followup = make_remittance(session, payer)
    make_rc(
        session,
        followup,
        pcn="CLM-0001-R1",
        claim=claim,
        paid="60.00",
        adjustments=[("CO", "45", "25.00"), ("PR", "1", "10.00"), ("PR", "2", "5.00")],
    )
    [outcome] = record_outcomes_for_remittance(session, followup)
    assert outcome.result == "paid" and outcome.details["remaining_denials"] == []
    assert work_item.status == "closed" and claim.status == "paid"


def test_patient_responsibility_shift_is_paid_and_agrees_with_the_importer(session, payer, submitted_item):
    """The payer paid its share and moved the rest to the patient (PR-96, non-covered). The importer
    does not count that CLP as a denial and sets the claim to paid (app.ingest.denials); the outcome
    rule uses the same definition, so the claim is not flipped to partial within one import."""
    claim, _, _, work_item = submitted_item
    followup = make_remittance(session, payer)
    rc = make_rc(
        session, followup, pcn="CLM-0001-R1", claim=claim, paid="80.00", adjustments=[("PR", "96", "20.00")]
    )
    assert rc.is_denial is False and claim_status_for(rc) == "paid"
    assert remaining_denial_codes(rc) == []
    [outcome] = record_outcomes_for_remittance(session, followup)
    assert outcome.result == "paid" and outcome.details["reason"] == "no_denial_remaining"
    assert outcome.details["remaining_denials"] == []
    assert work_item.status == "closed" and claim.status == "paid"
    # the same code under CO is the payer's decision, not the patient's share: a denial remains
    other = make_remittance(session, payer)
    rc_co = make_rc(
        session, other, pcn="CLM-0001-R1", claim=claim, paid="80.00", adjustments=[("CO", "96", "20.00")]
    )
    assert rc_co.is_denial is True and remaining_denial_codes(rc_co) == ["CO-96"]
    assert claim_status_for(rc_co) == "partial"


def test_new_denial_on_an_untouched_line_does_not_make_the_fix_partial(session, clinic, payer, patient):
    """The work item disputed line 1 (CO-4 on 99213). The follow-up pays line 1 and denies line 2 for
    a new reason: the fix won; the new denial is the pipeline's business (a new work item)."""
    claim = make_claim(session, clinic, payer, patient, "CLM-LINE", total="150.00")
    denial = make_remittance(session, payer, payment_date=DENIAL_DATE)
    denial_rc = make_rc(
        session,
        denial,
        pcn="CLM-LINE",
        claim=claim,
        status_code="1",
        charge="150.00",
        paid="50.00",
        adjustments=[("CO", "4", "100.00", 1, "99213")],
    )
    reasons = [
        {"level": "line", "group_code": "CO", "carc": "4", "line_number": 1, "procedure_code": "99213"}
    ]
    work_item = make_work_item(session, claim, denial_rc, submitted_ccn="CLM-LINE-R1", reasons=reasons)
    followup = make_remittance(session, payer)
    rc = make_rc(
        session,
        followup,
        pcn="CLM-LINE-R1",
        claim=claim,
        charge="150.00",
        paid="80.00",
        adjustments=[("CO", "45", "20.00", 1, "99213"), ("CO", "97", "50.00", 2, "17110")],
    )
    assert rc.is_denial is True  # the new denial on line 2 still makes the CLP a denial for the pipeline
    assert remaining_denial_codes(rc, reasons) == []
    assert remaining_denial_codes(rc) == ["CO-97@2"]  # without the work item's scope the whole claim counts
    [outcome] = record_outcomes_for_remittance(session, followup)
    assert outcome.result == "paid" and outcome.details["reason"] == "no_denial_remaining"
    assert work_item.status == "closed" and claim.status == "paid"


def test_denial_remaining_on_the_disputed_line_is_partial(session, clinic, payer, patient):
    claim = make_claim(session, clinic, payer, patient, "CLM-LINE2", total="150.00")
    denial = make_remittance(session, payer, payment_date=DENIAL_DATE)
    denial_rc = make_rc(
        session,
        denial,
        pcn="CLM-LINE2",
        claim=claim,
        charge="150.00",
        adjustments=[("CO", "4", "100.00", 1, "99213")],
    )
    reasons = [
        {"level": "line", "group_code": "CO", "carc": "4", "line_number": 1, "procedure_code": "99213"}
    ]
    work_item = make_work_item(session, claim, denial_rc, submitted_ccn="CLM-LINE2-R1", reasons=reasons)
    followup = make_remittance(session, payer)
    make_rc(
        session,
        followup,
        pcn="CLM-LINE2-R1",
        claim=claim,
        charge="150.00",
        paid="50.00",
        adjustments=[("CO", "4", "100.00", 1, "99213")],
    )
    [outcome] = record_outcomes_for_remittance(session, followup)
    assert outcome.result == "partial" and outcome.details["remaining_denials"] == ["CO-4@1"]
    assert work_item.status == "closed" and claim.status == "partial"


def test_line_scope_follows_the_procedure_code_not_the_svc_position(session, clinic, payer, patient):
    """Payers do not keep SVC order between adjudications. The work item disputed line 2 (11102,
    CO-4); the re-adjudication lists another procedure at line 2 with a new denial: the fix won and
    that denial is the pipeline's business. The disputed procedure denied again at another line
    number is the same denial remaining. Line numbers decide only when a procedure code is missing."""
    claim = make_claim(session, clinic, payer, patient, "CLM-SVC", total="200.00")
    denial = make_remittance(session, payer, payment_date=DENIAL_DATE)
    denial_rc = make_rc(
        session,
        denial,
        pcn="CLM-SVC",
        claim=claim,
        charge="200.00",
        paid="100.00",
        adjustments=[("CO", "4", "100.00", 2, "11102")],
    )
    reasons = [
        {"level": "line", "group_code": "CO", "carc": "4", "line_number": 2, "procedure_code": "11102"}
    ]
    work_item = make_work_item(session, claim, denial_rc, submitted_ccn="CLM-SVC-R1", reasons=reasons)
    followup = make_remittance(session, payer)
    rc = make_rc(
        session,
        followup,
        pcn="CLM-SVC-R1",
        claim=claim,
        charge="200.00",
        paid="190.00",
        adjustments=[("CO", "4", "10.00", 2, "17000")],
    )
    assert remaining_denial_codes(rc, reasons) == []
    assert remaining_denial_codes(rc) == ["CO-4@2"]  # without the work item's scope the whole claim counts
    [outcome] = record_outcomes_for_remittance(session, followup)
    assert outcome.result == "paid" and outcome.details["reason"] == "no_denial_remaining"
    assert work_item.status == "closed" and claim.status == "paid"

    moved = make_rc(
        session,
        make_remittance(session, payer),
        pcn="CLM-SVC-R1",
        claim=claim,
        charge="200.00",
        paid="100.00",
        adjustments=[("CO", "4", "100.00", 3, "11102")],
    )
    assert remaining_denial_codes(moved, reasons) == ["CO-4@3"]
    unnamed = make_rc(
        session,
        make_remittance(session, payer),
        pcn="CLM-SVC-R1",
        claim=claim,
        charge="200.00",
        paid="100.00",
        adjustments=[("CO", "4", "100.00", 2, None)],
    )
    assert remaining_denial_codes(unnamed, reasons) == ["CO-4@2"]  # no procedure on the SVC: the line decides
    bare = [{"level": "line", "group_code": "CO", "carc": "4", "line_number": 2}]
    assert remaining_denial_codes(rc, bare) == ["CO-4@2"]  # no procedure in the reasons: the line decides
    assert remaining_denial_codes(moved, bare) == []


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


def test_reversal_leaves_the_work_item_submitted_and_the_claim_status_alone(session, payer, submitted_item):
    """A CLP02 22 only takes the earlier adjudication back: the fix is still in flight, so the item
    stays submitted and the claim keeps the status mark-submitted gave it (nothing was denied)."""
    claim, _, _, work_item = submitted_item
    claim.status = "corrected"  # what mark_submitted leaves behind for a corrected claim
    session.flush()
    followup = make_remittance(session, payer)
    make_rc(session, followup, pcn="CLM-0001-R1", claim=claim, status_code="22", paid="-100.00")
    [outcome] = record_outcomes_for_remittance(session, followup)
    assert outcome.result == "reversed"
    assert outcome.details["reason"] == "reversal"
    assert work_item.status == "submitted" and work_item.closed_at is None
    assert claim.status == "corrected"
    [event] = _outcome_events(session, work_item.id)
    assert event.details["result"] == "reversed" and event.details["work_item_status"] == "submitted"


def test_reversal_then_readjudication_in_a_later_remittance(session, payer, submitted_item):
    """The common payer pattern: one 835 takes the original adjudication back, a later one pays the
    replacement claim. Both are recorded; the payment is what closes the work item."""
    claim, _, _, work_item = submitted_item
    reversal = make_remittance(session, payer, payment_date=date(2026, 8, 1))
    make_rc(session, reversal, pcn="CLM-0001-R1", claim=claim, status_code="22", paid="-100.00")
    [reversed_outcome] = record_outcomes_for_remittance(session, reversal)
    session.commit()
    assert reversed_outcome.result == "reversed" and work_item.status == "submitted"

    payment = make_remittance(session, payer, payment_date=date(2026, 8, 20))
    make_rc(
        session, payment, pcn="CLM-0001-R1", claim=claim, paid="80.00", adjustments=[("CO", "45", "20.00")]
    )
    [paid_outcome] = record_outcomes_for_remittance(session, payment)
    assert paid_outcome.result == "paid" and paid_outcome.paid_amount == Decimal("80.00")
    assert work_item.status == "closed" and work_item.closed_at is not None
    assert claim.status == "paid"
    assert [o.result for o in sorted(work_item.outcomes, key=lambda o: o.id)] == ["reversed", "paid"]
    assert len(_outcome_events(session, work_item.id)) == 2
    assert audit.verify_chain(session) is None


def test_threshold_is_95_percent_of_dollar_value():
    """With a denial remaining, 95% of the dollar value is the bar; without one any payment is paid."""
    rc = _classified(paid="95.00", is_denial=True)
    result, details = classify_outcome(rc, Decimal("100.00"))
    assert (result, details["reason"]) == ("paid", "paid_at_least_95pct")
    rc.paid_amount = Decimal("94.99")
    result, details = classify_outcome(rc, Decimal("100.00"))
    assert (result, details["reason"]) == ("partial", "denial_remains")
    rc.paid_amount = Decimal("0.00")
    result, details = classify_outcome(rc, Decimal("100.00"))
    assert result == "denied_again" and details["paid_threshold"] == "95.00"
    rc.claim_status_code = "22"
    assert classify_outcome(rc, Decimal("100.00"))[0] == "reversed"
    clean = _classified(paid="50.00", is_denial=False)
    result, details = classify_outcome(clean, Decimal("100.00"))
    assert (result, details["reason"]) == ("paid", "no_denial_remaining")


def test_threshold_is_rounded_to_the_cent_once():
    """The threshold compared is the threshold recorded: a payment equal to the shown figure passes."""
    rc = _classified(paid="31.66", is_denial=True)
    result, details = classify_outcome(rc, Decimal("33.33"))  # 0.95 * 33.33 = 31.6635
    assert details["paid_threshold"] == "31.66" and result == "paid"
    rc.paid_amount = Decimal("31.65")
    assert classify_outcome(rc, Decimal("33.33"))[0] == "partial"
    assert paid_threshold(Decimal("33.33")) == Decimal("31.66")
    assert paid_threshold(Decimal("10.01")) == Decimal("9.51")  # 9.5095 rounds half up
    assert paid_threshold(None) == Decimal("0.00")


@pytest.mark.parametrize("value", [Decimal("0"), None])
def test_without_a_dollar_value_a_remaining_denial_is_partial(value):
    rc = _classified(paid="10.00", is_denial=True)
    result, details = classify_outcome(rc, value)
    assert (result, details["reason"]) == ("partial", "denial_remains")


# --------------------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------------------


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


def test_a_corrected_claim_the_clinic_numbered_itself_answers_the_item(
    session, clinic, payer, patient, submitted_item
):
    """The clinic's system gave the resubmission a number of its own (not CLM-0001-R1) and the later
    claim export links it to the original claim: the payment reaches the item through that link."""
    claim, _, _, work_item = submitted_item
    corrected = make_claim(session, clinic, payer, patient, "CL7-000042", frequency_code="7", original=claim)
    followup = make_remittance(session, payer)
    rc = make_rc(session, followup, pcn="CL7-000042", claim=corrected, status_code="1", paid="100.00")

    assert [(wi.id, how) for wi, how in candidate_work_items(session, rc)] == [
        (work_item.id, "original_claim")
    ]
    [outcome] = record_outcomes_for_remittance(session, followup)
    assert outcome.result == "paid"
    assert outcome.details["match"] == "original_claim"
    assert work_item.status == "closed"
    assert claim.status == "paid" and corrected.status == "paid"


def test_a_replacement_sent_to_another_payer_answers_through_that_payer(
    session, clinic, payer, other_payer, patient, submitted_item
):
    """The clinic sent its own replacement claim to another payer: that payer's answer reaches the
    item through the original-claim link, while a third payer's CLP on the same claim is still skipped."""
    claim, _, _, work_item = submitted_item
    corrected = make_claim(
        session, clinic, other_payer, patient, "CL7-000043", frequency_code="7", original=claim
    )
    third = Payer(name="Third Synthetic Plan", payer_id="SYN003")
    session.add(third)
    session.flush()
    stray = make_remittance(session, third)
    stray_rc = make_rc(session, stray, pcn="CL7-000043", claim=corrected, status_code="1", paid="100.00")
    assert candidate_work_items(session, stray_rc) == []
    assert record_outcomes_for_remittance(session, stray) == []

    followup = make_remittance(session, other_payer)
    rc = make_rc(session, followup, pcn="CL7-000043", claim=corrected, status_code="1", paid="100.00")
    assert [(wi.id, how) for wi, how in candidate_work_items(session, rc)] == [
        (work_item.id, "original_claim")
    ]
    [outcome] = record_outcomes_for_remittance(session, followup)
    assert outcome.result == "paid" and work_item.status == "closed"


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


@pytest.mark.parametrize(
    "status", ["new", "proposed", "in_review", "approved", "rejected", "exported", "closed"]
)
def test_only_submitted_items_match(session, clinic, payer, patient, status):
    """SPEC 10: outcomes are recorded for work items the reviewer marked submitted; an exported
    package the clinic never sent is left alone when the payer reprocesses the claim on its own."""
    claim = make_claim(session, clinic, payer, patient, f"CLM-{status}")
    denial = make_remittance(session, payer, payment_date=DENIAL_DATE)
    denial_rc = make_rc(
        session, denial, pcn=claim.claim_control_number, claim=claim, status_code="4", is_denial=True
    )
    work_item = make_work_item(session, claim, denial_rc, status=status)
    followup = make_remittance(session, payer)
    make_rc(session, followup, pcn=claim.claim_control_number, claim=claim, status_code="1", paid="100.00")
    assert record_outcomes_for_remittance(session, followup) == []
    assert work_item.status == status and work_item.closed_at is None
    assert claim.status == "denied"  # what the import set; nothing here touched it
    assert _outcome_count(session) == 0


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
    denial = make_remittance(session, payer, payment_date=DENIAL_DATE)
    denial_rc = make_rc(session, denial, pcn="CLM-DATE", claim=claim, status_code="4", is_denial=True)
    work_item = make_work_item(session, claim, denial_rc)
    followup = make_remittance(session, payer, payment_date=date(2026, 9, 1))
    rc = make_rc(session, followup, pcn="CLM-DATE", claim=claim, paid="100.00")
    outcome = record_outcome(session, work_item, rc, payment_date=date(2026, 9, 3))
    assert outcome.payment_date == date(2026, 9, 3)


def test_reversal_and_readjudication_in_same_remittance(session, payer, submitted_item):
    claim, _, _, work_item = submitted_item
    followup = make_remittance(session, payer)
    make_rc(session, followup, pcn="CLM-0001-R1", claim=claim, status_code="22", paid="-100.00")
    make_rc(session, followup, pcn="CLM-0001-R1", claim=claim, status_code="1", paid="100.00")
    outcomes = record_outcomes_for_remittance(session, followup)
    assert [o.result for o in outcomes] == ["paid"]
    assert work_item.status == "closed" and claim.status == "paid"


# --------------------------------------------------------------------------------------
# Which remittances may answer a fix (date and payer)
# --------------------------------------------------------------------------------------


def test_remittance_dated_before_the_submission_is_ignored(session, payer, submitted_item, caplog):
    """A backfilled or re-delivered 835 older than the fix cannot be its outcome."""
    claim, _, _, work_item = submitted_item
    older = make_remittance(session, payer, payment_date=date(2026, 7, 10))
    rc = make_rc(session, older, pcn="CLM-0001", claim=claim, status_code="4", is_denial=True)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert record_outcomes_for_remittance(session, older) == []
    assert work_item.status == "submitted" and _outcome_count(session) == 0
    assert any(
        SKIP_BEFORE_SUBMISSION in record.getMessage()
        and str(rc.id) in record.getMessage()
        and str(work_item.id) in record.getMessage()
        for record in caplog.records
        if record.name == LOGGER
    )
    # Dated on the day of the submission: accepted.
    same_day = make_remittance(session, payer, payment_date=SUBMITTED_AT.date())
    make_rc(session, same_day, pcn="CLM-0001-R1", claim=claim, paid="100.00")
    [outcome] = record_outcomes_for_remittance(session, same_day)
    assert outcome.result == "paid" and work_item.status == "closed"


def test_remittance_date_falls_back_to_production_date_then_receipt(session, payer, submitted_item):
    claim, _, _, work_item = submitted_item
    undated = make_remittance(session, payer, payment_date=None, production_date=date(2026, 7, 1))
    make_rc(session, undated, pcn="CLM-0001-R1", claim=claim, paid="100.00")
    assert record_outcomes_for_remittance(session, undated) == []
    assert work_item.status == "submitted"

    received = make_remittance(
        session,
        payer,
        payment_date=None,
        production_date=None,
        received_at=datetime(2026, 8, 1, 9, 0, tzinfo=UTC),
    )
    make_rc(session, received, pcn="CLM-0001-R1", claim=claim, paid="100.00")
    [outcome] = record_outcomes_for_remittance(session, received)
    # The outcome carries the date the gate used: the day the file arrived.
    assert outcome.result == "paid" and outcome.payment_date == date(2026, 8, 1)
    assert work_item.status == "closed"


def test_another_payers_remittance_is_ignored_unless_it_names_the_corrected_claim(
    session, payer, other_payer, submitted_item, caplog
):
    """A secondary payer's CLP on the original claim is a coincidence; a CLP carrying the corrected
    claim's own control number is the answer to a fix that may have been redirected to that payer."""
    claim, _, _, work_item = submitted_item
    foreign = make_remittance(session, other_payer)
    rc = make_rc(session, foreign, pcn="CLM-0001", claim=claim, status_code="2", paid="15.00")
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert record_outcomes_for_remittance(session, foreign) == []
    assert work_item.status == "submitted" and _outcome_count(session) == 0
    assert any(
        SKIP_OTHER_PAYER in record.getMessage() and str(rc.id) in record.getMessage()
        for record in caplog.records
        if record.name == LOGGER
    )

    redirected = make_remittance(session, other_payer)
    make_rc(session, redirected, pcn="CLM-0001-R1", claim=claim, paid="100.00")
    [outcome] = record_outcomes_for_remittance(session, redirected)
    assert outcome.result == "paid" and work_item.status == "closed"


def test_remittance_from_an_unknown_payer_is_accepted(session, payer, submitted_item):
    claim, _, _, work_item = submitted_item
    unknown = make_remittance(session, None)
    make_rc(session, unknown, pcn="CLM-0001", claim=claim, paid="100.00")
    [outcome] = record_outcomes_for_remittance(session, unknown)
    assert outcome.result == "paid" and outcome.details["match"] == "claim_id"
    assert work_item.status == "closed"


def test_appeal_answered_under_the_original_control_number_needs_the_claims_payer(
    session, clinic, payer, other_payer, patient
):
    """An appeal is submitted under the claim's own number, so a same-number CLP from another payer
    is not its answer (the corrected-claim exception does not apply)."""
    claim = make_claim(session, clinic, payer, patient, "CLM-APPEAL")
    denial = make_remittance(session, payer, payment_date=DENIAL_DATE)
    denial_rc = make_rc(session, denial, pcn="CLM-APPEAL", claim=claim, status_code="4", is_denial=True)
    work_item = make_work_item(session, claim, denial_rc, submitted_ccn="CLM-APPEAL")
    foreign = make_remittance(session, other_payer)
    make_rc(session, foreign, pcn="CLM-APPEAL", claim=claim, paid="100.00")
    assert record_outcomes_for_remittance(session, foreign) == []
    own = make_remittance(session, payer)
    make_rc(session, own, pcn="CLM-APPEAL", claim=claim, paid="100.00")
    [outcome] = record_outcomes_for_remittance(session, own)
    assert outcome.result == "paid" and work_item.status == "closed"


# --------------------------------------------------------------------------------------
# Take-backs: a reversal after a closing payment reopens the work item
# --------------------------------------------------------------------------------------

PAID_DATE = date(2026, 8, 1)
TAKEBACK_DATE = date(2026, 8, 20)
REPAID_DATE = date(2026, 9, 10)
CONTRACTUAL = [("CO", "45", "20.00")]


def make_proposal(session, work_item, action_type="corrected_claim"):
    """The current proposal of a fixture item (its action type decides the claim status on reopen)."""
    proposal = Proposal(
        version=1,
        is_current=True,
        action_type=action_type,
        summary="synthetic proposal",
        rationale="synthetic rationale",
        citations=[],
        confidence=0.9,
    )
    work_item.proposals.append(proposal)
    session.flush()
    return proposal


def pay(session, payer, claim, *, pcn="CLM-0001-R1", payment_date=PAID_DATE, production_date=None):
    """A follow-up that pays the corrected claim at the contracted rate: 80.00 paid, CO-45 20.00."""
    remittance = make_remittance(session, payer, payment_date=payment_date, production_date=production_date)
    make_rc(session, remittance, pcn=pcn, claim=claim, paid="80.00", adjustments=CONTRACTUAL)
    return remittance


def take_back(session, payer, claim, *, pcn="CLM-0001-R1", payment_date=TAKEBACK_DATE):
    """A later 835 that reverses that payment: CLP02 22, negative amounts."""
    remittance = make_remittance(session, payer, payment_date=payment_date)
    make_rc(session, remittance, pcn=pcn, claim=claim, status_code="22", charge="-100.00", paid="-80.00")
    return remittance


def close_by_payment(session, payer, submitted_item, *, action_type="corrected_claim"):
    """The fixture item with a proposal of `action_type` (None: no proposal), paid and closed."""
    claim, _denial, _denial_rc, work_item = submitted_item
    if action_type is not None:
        make_proposal(session, work_item, action_type)
    # What mark_submitted leaves behind: corrected / appealed, or the import's status for the rest.
    claim.status = {"corrected_claim": "corrected", "appeal": "appealed"}.get(action_type, "denied")
    [paid] = record_outcomes_for_remittance(session, pay(session, payer, claim))
    session.commit()
    assert paid.result == "paid" and work_item.status == "closed" and claim.status == "paid"
    return claim, work_item, paid


def add_paid_outcome(session, payer, claim, work_item, *, pcn):
    """A paid outcome row written directly (for items in statuses the matcher never closes itself)."""
    paid_rc = make_rc(
        session, make_remittance(session, payer, payment_date=PAID_DATE), pcn=pcn, claim=claim, paid="80.00"
    )
    session.add(
        Outcome(
            work_item=work_item, remittance_claim=paid_rc, result="paid", paid_amount=Decimal("80.00"),
            payment_date=PAID_DATE,
        )
    )  # fmt: skip
    session.commit()


def _reopen_events(session, work_item_id):
    stmt = (
        select(AuditLog)
        .where(AuditLog.action == "work_item_reopened", AuditLog.entity_id == work_item_id)
        .order_by(AuditLog.id)
    )
    return list(session.execute(stmt).scalars())


def _audit_count(session):
    return session.execute(select(func.count(AuditLog.id))).scalar_one()


def _results(work_item):
    return [o.result for o in sorted(work_item.outcomes, key=lambda o: o.id)]


def _skip_logged(caplog, reason, *ids):
    return any(
        reason in record.getMessage() and all(str(i) in record.getMessage() for i in ids)
        for record in caplog.records
        if record.name == LOGGER
    )


@pytest.mark.parametrize(
    ("action_type", "claim_status_after"),
    [("corrected_claim", "corrected"), ("appeal", "appealed"), ("clinic_question", "paid"), (None, "paid")],
)
def test_a_reversal_after_the_payment_reopens_the_closed_work_item(
    session, payer, submitted_item, action_type, claim_status_after
):
    """Paid in one remittance, taken back in a later one: the item goes back to submitted with the
    submission facts untouched, the claim to what mark_submitted gave it (left alone when the
    proposal was not a corrected claim or an appeal), and two audit events say so."""
    claim, work_item, _paid = close_by_payment(session, payer, submitted_item, action_type=action_type)
    untouched = (
        work_item.submitted_at,
        work_item.decided_at,
        work_item.first_viewed_at,
        work_item.submitted_claim_control_number,
    )
    assert untouched[0] is not None and work_item.closed_at is not None
    events_before = _audit_count(session)

    [outcome] = record_outcomes_for_remittance(session, take_back(session, payer, claim))

    assert outcome.result == "reversed" and outcome.details["reason"] == "reversal"
    assert outcome.paid_amount == Decimal("-80.00") and outcome.payment_date == TAKEBACK_DATE
    assert _results(work_item) == ["paid", "reversed"] and latest_outcome(work_item) is outcome
    assert work_item.status == "submitted" and work_item.closed_at is None
    assert (
        work_item.submitted_at,
        work_item.decided_at,
        work_item.first_viewed_at,
        work_item.submitted_claim_control_number,
    ) == untouched
    assert claim.status == claim_status_after

    assert _audit_count(session) == events_before + 2
    recorded = sorted(_outcome_events(session, work_item.id), key=lambda e: e.id)
    assert [e.details["result"] for e in recorded] == ["paid", "reversed"]
    assert "previous_work_item_status" not in recorded[0].details
    assert recorded[1].details["work_item_status"] == "submitted"
    assert recorded[1].details["previous_work_item_status"] == "closed"
    assert recorded[1].details["outcome_id"] == outcome.id and recorded[1].actor_type == "system"
    [reopened] = _reopen_events(session, work_item.id)
    assert reopened.id > recorded[1].id
    assert (reopened.actor_type, reopened.actor_id, reopened.entity_type, reopened.entity_id) == (
        "system",
        "outcomes",
        "work_item",
        work_item.id,
    )
    assert reopened.claim_id == claim.id
    assert reopened.details == {
        "previous_status": "closed",
        "reason": "payer_reversal",
        "remittance_claim_id": outcome.remittance_claim_id,
        "remittance_id": outcome.remittance_claim.remittance_id,
        "outcome_id": outcome.id,
        "claim_status": claim_status_after,
    }
    assert audit.verify_chain(session) is None


def test_a_later_payment_closes_the_reopened_work_item_again(session, payer, submitted_item):
    claim, work_item, _paid = close_by_payment(session, payer, submitted_item)
    record_outcomes_for_remittance(session, take_back(session, payer, claim))
    session.commit()
    assert work_item.status == "submitted" and claim.status == "corrected"

    [repaid] = record_outcomes_for_remittance(session, pay(session, payer, claim, payment_date=REPAID_DATE))

    assert repaid.result == "paid" and repaid.paid_amount == Decimal("80.00")
    assert _results(work_item) == ["paid", "reversed", "paid"] and latest_outcome(work_item) is repaid
    assert work_item.status == "closed" and work_item.closed_at is not None and claim.status == "paid"
    assert len(_outcome_events(session, work_item.id)) == 3
    assert len(_reopen_events(session, work_item.id)) == 1  # the reopen is not repeated
    assert audit.verify_chain(session) is None


@pytest.mark.parametrize("reversal_first", [True, False])
def test_a_take_back_and_a_re_adjudication_in_one_remittance_close_the_item_again(
    session, payer, submitted_item, reversal_first
):
    """CLP02 22 plus a paid CLP02 1 under the same control number, in either order: the take-back
    reopens the item first (pass one), the payment closes it again (pass two) and, on the same
    payment date, the row recorded last is the latest outcome. A re-run changes nothing."""
    claim, work_item, _paid = close_by_payment(session, payer, submitted_item)
    followup = make_remittance(session, payer, payment_date=TAKEBACK_DATE)
    clps = [
        lambda: make_rc(
            session,
            followup,
            pcn="CLM-0001-R1",
            claim=claim,
            status_code="22",
            charge="-100.00",
            paid="-80.00",
        ),
        lambda: make_rc(
            session, followup, pcn="CLM-0001-R1", claim=claim, paid="80.00", adjustments=CONTRACTUAL
        ),
    ]
    for build in clps if reversal_first else reversed(clps):
        build()

    outcomes = record_outcomes_for_remittance(session, followup)

    assert [o.result for o in outcomes] == ["reversed", "paid"]
    assert _results(work_item) == ["paid", "reversed", "paid"]
    assert outcomes[0].payment_date == outcomes[1].payment_date == TAKEBACK_DATE
    assert latest_outcome(work_item) is outcomes[1]
    assert work_item.status == "closed" and work_item.closed_at is not None and claim.status == "paid"
    assert len(_reopen_events(session, work_item.id)) == 1
    session.commit()
    snapshot = (_outcome_count(session), _audit_count(session))
    assert record_outcomes_for_remittance(session, followup) == []
    assert (_outcome_count(session), _audit_count(session)) == snapshot
    assert work_item.status == "closed" and _results(work_item) == ["paid", "reversed", "paid"]
    assert audit.verify_chain(session) is None


def test_a_reversal_dated_before_the_payment_is_skipped(session, payer, submitted_item, caplog):
    """A reversal older than the payment it would take back (a backfilled 835) is logged by id and
    ignored; one dated on the payment's own day is accepted."""
    claim, work_item, paid = close_by_payment(session, payer, submitted_item)
    assert paid.payment_date == PAID_DATE
    earlier = take_back(session, payer, claim, payment_date=date(2026, 7, 25))  # after the submission
    rc = earlier.claims[0]
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert record_outcomes_for_remittance(session, earlier) == []
    assert work_item.status == "closed" and _results(work_item) == ["paid"] and claim.status == "paid"
    assert not _reopen_events(session, work_item.id)
    assert _skip_logged(caplog, SKIP_BEFORE_OUTCOME, rc.id, work_item.id)
    assert SKIP_BEFORE_OUTCOME == "remittance_before_outcome"

    [outcome] = record_outcomes_for_remittance(
        session, take_back(session, payer, claim, payment_date=PAID_DATE)
    )
    assert outcome.result == "reversed" and work_item.status == "submitted"


def test_an_undated_payment_is_dated_by_its_remittance_for_the_take_back_gate(session, payer, submitted_item):
    """A payment from an 835 without BPR16 is dated by the production date when it is recorded, and
    the take-back gate compares against that date."""
    claim, _denial, _denial_rc, work_item = submitted_item
    make_proposal(session, work_item)
    [paid] = record_outcomes_for_remittance(
        session, pay(session, payer, claim, payment_date=None, production_date=PAID_DATE)
    )
    session.commit()
    assert paid.payment_date == PAID_DATE and work_item.status == "closed"

    assert (
        record_outcomes_for_remittance(
            session, take_back(session, payer, claim, payment_date=date(2026, 7, 25))
        )
        == []
    )
    assert work_item.status == "closed"
    [outcome] = record_outcomes_for_remittance(
        session, take_back(session, payer, claim, payment_date=PAID_DATE)
    )
    assert outcome.result == "reversed" and work_item.status == "submitted"


def test_an_outcome_row_without_a_date_is_dated_by_its_remittance_for_the_take_back_gate(
    session, clinic, payer, patient
):
    """An outcome row that carries no payment date (written directly, or before outcomes were dated
    by their remittance) is dated by its remittance claim's remittance for the gate."""
    claim = make_claim(session, clinic, payer, patient, "CLM-UNDATED")
    denial = make_remittance(session, payer, payment_date=DENIAL_DATE)
    denial_rc = make_rc(session, denial, pcn="CLM-UNDATED", claim=claim, status_code="4", is_denial=True)
    work_item = make_work_item(session, claim, denial_rc, status="closed", submitted_ccn="CLM-UNDATED-R1")
    make_proposal(session, work_item)
    undated = make_remittance(session, payer, payment_date=None, production_date=PAID_DATE)
    paid_rc = make_rc(
        session, undated, pcn="CLM-UNDATED-R1", claim=claim, paid="80.00", adjustments=CONTRACTUAL
    )
    session.add(
        Outcome(
            work_item=work_item, remittance_claim=paid_rc, result="paid", paid_amount=Decimal("80.00"),
            payment_date=None,
        )
    )  # fmt: skip
    session.commit()
    assert latest_outcome(work_item).payment_date is None

    earlier = take_back(session, payer, claim, pcn="CLM-UNDATED-R1", payment_date=date(2026, 7, 25))
    assert takeback_candidates(session, earlier.claims[0]) == []
    assert record_outcomes_for_remittance(session, earlier) == [] and work_item.status == "closed"
    [outcome] = record_outcomes_for_remittance(
        session, take_back(session, payer, claim, pcn="CLM-UNDATED-R1", payment_date=PAID_DATE)
    )
    assert outcome.result == "reversed" and work_item.status == "submitted"


@pytest.mark.parametrize("dated_by", ["production_date", "receipt"])
def test_an_undated_take_back_is_dated_by_its_remittance_and_leaves_recovered_dollars(
    session, payer, submitted_item, dated_by
):
    """An 835 whose BPR16 is missing or malformed still takes the payment back: the reversal is dated
    the way the gate dated its remittance (the production date, else the day the file arrived), so it
    is the latest outcome and the dashboard stops counting the payment as recovered."""
    claim, work_item, _paid = close_by_payment(session, payer, submitted_item)
    before = compute_dashboard(session, verify_audit=False)
    assert (before.paid_fixes, before.reversed_fixes, before.recovered_dollars) == (1, 0, Decimal("80.00"))
    if dated_by == "production_date":
        undated = make_remittance(session, payer, payment_date=None, production_date=TAKEBACK_DATE)
    else:
        received_at = datetime(TAKEBACK_DATE.year, TAKEBACK_DATE.month, TAKEBACK_DATE.day, 9, 0, tzinfo=UTC)
        undated = make_remittance(session, payer, payment_date=None, received_at=received_at)
    rc = make_rc(
        session, undated, pcn="CLM-0001-R1", claim=claim, status_code="22", charge="-100.00", paid="-80.00"
    )
    assert [(wi.id, how) for wi, how in takeback_candidates(session, rc)] == [(work_item.id, "claim_id")]

    [outcome] = record_outcomes_for_remittance(session, undated)
    session.commit()

    assert outcome.result == "reversed" and outcome.payment_date == TAKEBACK_DATE
    assert work_item.status == "submitted" and work_item.closed_at is None and claim.status == "corrected"
    assert _results(work_item) == ["paid", "reversed"] and latest_outcome(work_item) is outcome
    after = compute_dashboard(session, verify_audit=False)
    assert (after.submitted_fixes, after.paid_fixes, after.reversed_fixes) == (1, 0, 1)
    assert (after.recovered_dollars, after.taken_back_dollars) == (Decimal("0"), Decimal("80.00"))
    assert after.overturn_rate == 0.0


def test_a_payment_re_sent_for_a_closed_item_records_nothing(session, payer, submitted_item):
    """A non-reversal CLP never matches a closed item (the payer re-delivering the payment, say)."""
    claim, work_item, _paid = close_by_payment(session, payer, submitted_item)
    again = pay(session, payer, claim, payment_date=TAKEBACK_DATE)
    assert takeback_candidates(session, again.claims[0]) == []
    assert record_outcomes_for_remittance(session, again) == []
    assert _results(work_item) == ["paid"] and work_item.status == "closed" and _outcome_count(session) == 1


def test_a_take_back_matched_by_the_submitted_control_number_links_the_clp(session, payer, submitted_item):
    claim, work_item, _paid = close_by_payment(session, payer, submitted_item)
    takeback = make_remittance(session, payer, payment_date=TAKEBACK_DATE)
    rc = make_rc(
        session, takeback, pcn="CLM-0001-R1", claim=None, status_code="22", charge="-100.00", paid="-80.00"
    )
    assert [(wi.id, how) for wi, how in takeback_candidates(session, rc)] == [(work_item.id, "submitted_ccn")]
    [outcome] = record_outcomes_for_remittance(session, takeback)
    assert outcome.details["match"] == "submitted_ccn" and outcome.result == "reversed"
    assert rc.claim_id == claim.id and rc.match_method == "submitted_ccn"
    assert work_item.status == "submitted" and claim.status == "corrected"


def test_a_take_back_resets_the_corrected_claims_own_row_too(session, clinic, payer, patient, submitted_item):
    """When the corrected claim was imported as a claim of its own, the payment set both rows to
    paid and the take-back returns both to corrected: every claim the outcome touches."""
    claim, _, _, work_item = submitted_item
    corrected = make_claim(session, clinic, payer, patient, "CLM-0001-R1", frequency_code="7", original=claim)
    make_proposal(session, work_item, "corrected_claim")
    claim.status = "corrected"
    paid_remit = make_remittance(session, payer, payment_date=PAID_DATE)
    make_rc(session, paid_remit, pcn="CLM-0001-R1", claim=corrected, paid="80.00", adjustments=CONTRACTUAL)
    [paid] = record_outcomes_for_remittance(session, paid_remit)
    session.commit()
    assert paid.result == "paid" and paid.details["match"] == "submitted_ccn"
    assert work_item.status == "closed" and (claim.status, corrected.status) == ("paid", "paid")

    takeback = make_remittance(session, payer, payment_date=TAKEBACK_DATE)
    rc = make_rc(
        session,
        takeback,
        pcn="CLM-0001-R1",
        claim=corrected,
        status_code="22",
        charge="-100.00",
        paid="-80.00",
    )
    assert [(wi.id, how) for wi, how in takeback_candidates(session, rc)] == [(work_item.id, "submitted_ccn")]
    [outcome] = record_outcomes_for_remittance(session, takeback)

    assert outcome.result == "reversed" and work_item.status == "submitted"
    assert (claim.status, corrected.status) == ("corrected", "corrected")
    [event] = _reopen_events(session, work_item.id)
    assert event.details["claim_status"] == "corrected"


def test_an_item_closed_by_hand_is_not_reopened_by_a_reversal(session, clinic, payer, patient):
    claim = make_claim(session, clinic, payer, patient, "CLM-HAND")
    denial = make_remittance(session, payer, payment_date=DENIAL_DATE)
    denial_rc = make_rc(session, denial, pcn="CLM-HAND", claim=claim, status_code="4", is_denial=True)
    work_item = make_work_item(session, claim, denial_rc, status="closed", submitted_ccn="CLM-HAND-R1")
    work_item.closed_at = SUBMITTED_AT
    session.commit()
    takeback = take_back(session, payer, claim, pcn="CLM-HAND-R1")
    assert takeback_candidates(session, takeback.claims[0]) == []
    assert record_outcomes_for_remittance(session, takeback) == []
    assert work_item.status == "closed" and _outcome_count(session) == 0 and claim.status == "denied"


def test_an_item_closed_without_a_submission_is_not_reopened_by_a_reversal(session, clinic, payer, patient):
    """A payment on an item the reviewer closed without ever marking it submitted is nobody's fix
    (`submitted_at` must be set), so a reversal of that payment does not reopen it."""
    claim = make_claim(session, clinic, payer, patient, "CLM-NOSUB")
    denial = make_remittance(session, payer, payment_date=DENIAL_DATE)
    denial_rc = make_rc(session, denial, pcn="CLM-NOSUB", claim=claim, status_code="4", is_denial=True)
    work_item = make_work_item(session, claim, denial_rc, status="closed", submitted_at=None)
    work_item.closed_at = SUBMITTED_AT
    assert work_item.submitted_at is None
    add_paid_outcome(session, payer, claim, work_item, pcn="CLM-NOSUB")
    takeback = take_back(session, payer, claim, pcn="CLM-NOSUB")
    assert takeback_candidates(session, takeback.claims[0]) == []
    assert record_outcomes_for_remittance(session, takeback) == []
    assert work_item.status == "closed" and _results(work_item) == ["paid"] and _outcome_count(session) == 1
    assert not _reopen_events(session, work_item.id)


def test_an_item_denied_again_is_not_reopened_by_a_reversal(session, payer, submitted_item):
    claim, _, _, work_item = submitted_item
    denied = make_remittance(session, payer, payment_date=PAID_DATE)
    make_rc(session, denied, pcn="CLM-0001-R1", claim=claim, status_code="4", paid="0.00", is_denial=True)
    [outcome] = record_outcomes_for_remittance(session, denied)
    session.commit()
    assert outcome.result == "denied_again" and work_item.status == "closed"
    assert record_outcomes_for_remittance(session, take_back(session, payer, claim)) == []
    assert (
        work_item.status == "closed" and _results(work_item) == ["denied_again"] and claim.status == "denied"
    )
    assert not _reopen_events(session, work_item.id)


def test_an_item_closed_after_a_reversal_is_not_reopened_by_another_reversal(session, payer, submitted_item):
    """A reversal recorded while the fix was submitted, then the item closed by hand: its latest
    outcome is already a reversal, so a later CLP02 22 (the payer re-sending it, say) is not a
    take-back and the item stays closed."""
    claim, _, _, work_item = submitted_item
    make_proposal(session, work_item)
    claim.status = "corrected"
    [first] = record_outcomes_for_remittance(
        session, take_back(session, payer, claim, payment_date=PAID_DATE)
    )
    assert first.result == "reversed" and work_item.status == "submitted"
    work_item.status = "closed"
    work_item.closed_at = SUBMITTED_AT
    session.commit()

    later = take_back(session, payer, claim)
    assert takeback_candidates(session, later.claims[0]) == []
    assert record_outcomes_for_remittance(session, later) == []
    assert (
        work_item.status == "closed" and _results(work_item) == ["reversed"] and claim.status == "corrected"
    )
    assert not _reopen_events(session, work_item.id)


def test_a_reversal_for_a_submitted_item_still_takes_the_old_path(session, payer, submitted_item):
    """The fix was never closed: the reversal is recorded, the item stays submitted, the claim status
    is untouched and no reopen event is written. A second reversal behaves the same."""
    claim, _, _, work_item = submitted_item
    make_proposal(session, work_item)
    claim.status = "corrected"
    before = _audit_count(session)
    [outcome] = record_outcomes_for_remittance(session, take_back(session, payer, claim))
    assert outcome.result == "reversed" and work_item.status == "submitted" and claim.status == "corrected"
    assert _audit_count(session) == before + 1 and not _reopen_events(session, work_item.id)
    [event] = _outcome_events(session, work_item.id)
    assert "previous_work_item_status" not in event.details
    [again] = record_outcomes_for_remittance(
        session, take_back(session, payer, claim, payment_date=REPAID_DATE)
    )
    assert again.result == "reversed" and work_item.status == "submitted"
    assert not _reopen_events(session, work_item.id) and _results(work_item) == ["reversed", "reversed"]


@pytest.mark.parametrize("status", ["new", "proposed", "in_review", "approved", "rejected", "exported"])
def test_only_closed_items_can_be_taken_back(session, clinic, payer, patient, status):
    """A paid outcome on an item in any other status (submitted_at set, so the status alone decides)
    is not taken back by a reversal; nor does the reversal match it the old way."""
    claim = make_claim(session, clinic, payer, patient, f"CLM-TB-{status}")
    denial = make_remittance(session, payer, payment_date=DENIAL_DATE)
    denial_rc = make_rc(
        session, denial, pcn=claim.claim_control_number, claim=claim, status_code="4", is_denial=True
    )
    work_item = make_work_item(session, claim, denial_rc, status=status)
    work_item.submitted_at = SUBMITTED_AT
    add_paid_outcome(session, payer, claim, work_item, pcn=claim.claim_control_number)
    takeback = take_back(session, payer, claim, pcn=claim.claim_control_number)
    assert takeback_candidates(session, takeback.claims[0]) == []
    assert record_outcomes_for_remittance(session, takeback) == []
    assert work_item.status == status and _outcome_count(session) == 1


def test_the_payer_gate_applies_to_take_backs_too(session, payer, other_payer, submitted_item, caplog):
    claim, work_item, _paid = close_by_payment(session, payer, submitted_item)
    foreign = take_back(session, other_payer, claim, pcn="CLM-0001")  # the original number, another payer
    rc = foreign.claims[0]
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert record_outcomes_for_remittance(session, foreign) == []
    assert work_item.status == "closed" and _results(work_item) == ["paid"]
    assert _skip_logged(caplog, SKIP_OTHER_PAYER, rc.id, work_item.id)

    redirected = take_back(session, other_payer, claim)  # names the corrected claim: accepted
    [outcome] = record_outcomes_for_remittance(session, redirected)
    assert outcome.result == "reversed" and work_item.status == "submitted"


def test_the_originating_remittance_claim_never_takes_its_own_item_back(session, clinic, payer, patient):
    """A work item created from a CLP02 22 is not reopened by that same CLP, whatever its date."""
    claim = make_claim(session, clinic, payer, patient, "CLM-ORIG")
    origin = make_remittance(session, payer, payment_date=TAKEBACK_DATE)
    origin_rc = make_rc(
        session, origin, pcn="CLM-ORIG", claim=claim, status_code="22", charge="-100.00", paid="-80.00"
    )
    work_item = make_work_item(session, claim, origin_rc, status="closed", submitted_ccn="CLM-ORIG-R1")
    add_paid_outcome(session, payer, claim, work_item, pcn="CLM-ORIG-R1")
    assert takeback_candidates(session, origin_rc) == []
    assert record_outcomes_for_remittance(session, origin) == []
    assert work_item.status == "closed" and _outcome_count(session) == 1


def test_a_second_run_on_the_take_back_remittance_changes_nothing(session, payer, submitted_item):
    claim, work_item, _paid = close_by_payment(session, payer, submitted_item)
    takeback = take_back(session, payer, claim)
    first = record_outcomes_for_remittance(session, takeback)
    session.commit()
    snapshot = (_outcome_count(session), _audit_count(session), work_item.status, claim.status)
    assert len(first) == 1 and snapshot[0] == 2 and snapshot[2:] == ("submitted", "corrected")

    assert record_outcomes_for_remittance(session, takeback) == []

    assert (_outcome_count(session), _audit_count(session), work_item.status, claim.status) == snapshot
    assert len(_reopen_events(session, work_item.id)) == 1
    assert audit.verify_chain(session) is None


def test_record_outcome_reopens_a_closed_item_it_is_handed_a_reversal_for(session, payer, submitted_item):
    """record_outcome records what it is given: a reversal for a closed item is a take-back, once."""
    claim, work_item, _paid = close_by_payment(session, payer, submitted_item)
    rc = take_back(session, payer, claim).claims[0]
    outcome = record_outcome(session, work_item, rc)
    assert outcome.result == "reversed" and outcome.payment_date == TAKEBACK_DATE
    assert work_item.status == "submitted" and work_item.closed_at is None and claim.status == "corrected"
    assert record_outcome(session, work_item, rc).id == outcome.id
    assert _outcome_count(session) == 2 and len(_reopen_events(session, work_item.id)) == 1
