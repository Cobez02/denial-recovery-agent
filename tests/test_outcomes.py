"""Outcome matching (milestone M8): later 835s answer submitted fixes.

Every fixture work item was submitted on 2026-07-15 (noon UTC); the denial that created it is
dated 2026-07-01 and follow-up remittances 2026-08-15 unless a test says otherwise.
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
    Remittance,
    RemittanceClaim,
    RemittanceLine,
    WorkItem,
)
from app.outcomes import record_outcome, record_outcomes_for_remittance
from app.outcomes.matching import (
    SKIP_BEFORE_SUBMISSION,
    SKIP_OTHER_PAYER,
    candidate_work_items,
    classify_outcome,
    paid_threshold,
    remaining_denial_codes,
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
    assert outcome.result == "paid" and outcome.payment_date is None
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
