"""Remittance-claim matching, one test per method (docs/SPEC.md section 6). Rows are built by hand."""

from __future__ import annotations

import hashlib
from datetime import date
from decimal import Decimal

from app.ingest.matching import match_remittance_claim
from app.models import Claim, ClaimLine, Patient, Payer, Remittance, RemittanceClaim, RemittanceLine, WorkItem

# --------------------------------------------------------------------------------------
# Row builders (synthetic names only)
# --------------------------------------------------------------------------------------


def _payer(session, payer_id: str = "NHP001", name: str = "Northwind Health Plan") -> Payer:
    payer = Payer(name=name, payer_id=payer_id)
    session.add(payer)
    session.flush()
    return payer


def _patient(session, clinic, account: str = "ACC-1", member_id: str | None = "NHP0001") -> Patient:
    token = "PT-" + hashlib.sha256(f"{clinic.id}:{account}".encode()).hexdigest()[:6].upper()
    patient = Patient(
        clinic_id=clinic.id,
        token=token,
        account_number=account,
        first_name="Avery",
        last_name="Brightwater",
        member_id=member_id,
    )
    session.add(patient)
    session.flush()
    return patient


def _claim(
    session,
    clinic,
    payer: Payer,
    patient: Patient,
    ccn: str,
    *,
    total: Decimal = Decimal("270.00"),
    date_from: date = date(2026, 8, 4),
    date_to: date = date(2026, 8, 4),
) -> Claim:
    claim = Claim(
        clinic_id=clinic.id,
        payer_id=payer.id,
        patient_id=patient.id,
        claim_control_number=ccn,
        service_date_from=date_from,
        service_date_to=date_to,
        total_charge=total,
        status="submitted",
    )
    claim.lines.append(
        ClaimLine(line_number=1, service_date=date_from, procedure_code="99213", charge_amount=total)
    )
    session.add(claim)
    session.flush()
    return claim


def _remittance(session, payer: Payer | None, tag: str) -> Remittance:
    remittance = Remittance(
        payer_id=payer.id if payer is not None else None,
        file_name=f"{tag}.835",
        file_sha256=hashlib.sha256(tag.encode()).hexdigest(),
    )
    session.add(remittance)
    session.flush()
    return remittance


def _rc(
    session,
    remittance: Remittance,
    pcn: str,
    *,
    sequence: int = 1,
    charge: Decimal = Decimal("270.00"),
    paid: Decimal = Decimal("0"),
    member_id: str | None = None,
    statement_from: date | None = None,
    payer_ccn: str | None = None,
) -> RemittanceClaim:
    rc = RemittanceClaim(
        remittance_id=remittance.id,
        sequence=sequence,
        patient_control_number=pcn,
        claim_status_code="1",
        charge_amount=charge,
        paid_amount=paid,
        member_id=member_id,
        statement_from=statement_from,
        payer_claim_control_number=payer_ccn,
    )
    session.add(rc)
    session.flush()
    return rc


# --------------------------------------------------------------------------------------
# Methods, in the order they are tried
# --------------------------------------------------------------------------------------


def test_match_by_claim_control_number(session, clinic):
    payer = _payer(session)
    claim = _claim(session, clinic, payer, _patient(session, clinic), "CLM-1")
    _claim(session, clinic, payer, _patient(session, clinic, "ACC-2"), "CLM-2")
    rc = _rc(session, _remittance(session, payer, "r1"), "CLM-1")
    assert match_remittance_claim(session, rc) is claim
    assert rc.claim_id == claim.id and rc.match_method == "ccn"
    assert rc.claim is claim


def test_match_by_submitted_claim_control_number(session, clinic):
    payer = _payer(session)
    claim = _claim(session, clinic, payer, _patient(session, clinic), "CLM-1")
    first = _remittance(session, payer, "r1")
    original_rc = _rc(session, first, "CLM-1", payer_ccn="ICN-1")
    assert match_remittance_claim(session, original_rc) is claim
    work_item = WorkItem(
        claim_id=claim.id,
        remittance_claim_id=original_rc.id,
        category="authorization",
        status="submitted",
        submitted_claim_control_number="CLM-1-R1",
    )
    session.add(work_item)
    session.flush()

    rc = _rc(session, _remittance(session, payer, "r2"), "CLM-1-R1", charge=Decimal("999.00"))
    assert match_remittance_claim(session, rc) is claim
    assert rc.match_method == "submitted_ccn" and rc.claim_id == claim.id


def test_claim_control_number_wins_over_submitted_number(session, clinic):
    payer = _payer(session)
    patient = _patient(session, clinic)
    original = _claim(session, clinic, payer, patient, "CLM-1")
    corrected = _claim(session, clinic, payer, patient, "CLM-1-R1")  # the clinic re-exported it
    rc0 = _rc(session, _remittance(session, payer, "r1"), "CLM-1")
    match_remittance_claim(session, rc0)
    session.add(
        WorkItem(
            claim_id=original.id,
            remittance_claim_id=rc0.id,
            category="modifier",
            status="submitted",
            submitted_claim_control_number="CLM-1-R1",
        )
    )
    session.flush()
    rc = _rc(session, _remittance(session, payer, "r2"), "CLM-1-R1")
    assert match_remittance_claim(session, rc) is corrected
    assert rc.match_method == "ccn"


def test_match_by_payer_claim_control_number(session, clinic):
    payer = _payer(session)
    claim = _claim(session, clinic, payer, _patient(session, clinic), "CLM-1")
    first = _remittance(session, payer, "r1")
    earlier = _rc(session, first, "CLM-1", payer_ccn="ICN-9")
    assert match_remittance_claim(session, earlier, remittance=first) is claim

    later = _remittance(session, payer, "r2")
    rc = _rc(session, later, "GARBLED-1", sequence=1, payer_ccn="ICN-9", charge=Decimal("1.00"))
    assert match_remittance_claim(session, rc, remittance=later) is claim
    assert rc.match_method == "payer_ccn" and rc.claim_id == claim.id


def test_payer_claim_control_number_from_another_payer_is_ignored(session, clinic):
    payer = _payer(session)
    other = _payer(session, "CB4321", "Contoso Benefits")
    claim = _claim(session, clinic, payer, _patient(session, clinic), "CLM-1")
    first = _remittance(session, payer, "r1")
    earlier = _rc(session, first, "CLM-1", payer_ccn="ICN-9")
    assert match_remittance_claim(session, earlier, remittance=first) is claim

    foreign = _remittance(session, other, "r2")
    rc = _rc(session, foreign, "GARBLED-2", payer_ccn="ICN-9")
    assert match_remittance_claim(session, rc, remittance=foreign) is None
    assert rc.claim_id is None and rc.match_method is None


def test_fuzzy_match(session, clinic):
    payer = _payer(session)
    patient = _patient(session, clinic, member_id="NHP0007")
    claim = _claim(
        session,
        clinic,
        payer,
        patient,
        "CLM-7",
        total=Decimal("270.00"),
        date_from=date(2026, 8, 4),
        date_to=date(2026, 8, 6),
    )
    remittance = _remittance(session, payer, "r1")
    rc = _rc(
        session,
        remittance,
        "UNKNOWN-7",
        charge=Decimal("270.01"),
        member_id="NHP0007",
        statement_from=date(2026, 8, 5),
    )
    assert match_remittance_claim(session, rc, remittance=remittance) is claim
    assert rc.match_method == "fuzzy" and rc.claim_id == claim.id


def test_fuzzy_uses_line_service_date_when_statement_date_missing(session, clinic):
    payer = _payer(session)
    claim = _claim(session, clinic, payer, _patient(session, clinic, member_id="NHP0007"), "CLM-7")
    remittance = _remittance(session, payer, "r1")
    rc = _rc(session, remittance, "UNKNOWN-7", member_id="NHP0007")
    rc.lines.append(RemittanceLine(line_number=1, procedure_code="99213", service_date=date(2026, 8, 4)))
    session.flush()
    assert match_remittance_claim(session, rc, remittance=remittance) is claim
    assert rc.match_method == "fuzzy"


def test_fuzzy_rejects_near_misses(session, clinic):
    payer = _payer(session)
    _claim(session, clinic, payer, _patient(session, clinic, member_id="NHP0007"), "CLM-7")
    remittance = _remittance(session, payer, "r1")
    cases = [
        {
            "charge": Decimal("271.00"),
            "member_id": "NHP0007",
            "statement_from": date(2026, 8, 4),
        },  # charge off
        {
            "charge": Decimal("270.00"),
            "member_id": "NHP0007",
            "statement_from": date(2026, 8, 5),
        },  # date outside
        {"charge": Decimal("270.00"), "member_id": "NHP0008", "statement_from": date(2026, 8, 4)},  # member
        {"charge": Decimal("270.00"), "member_id": None, "statement_from": date(2026, 8, 4)},  # no member id
        {"charge": Decimal("270.00"), "member_id": "NHP0007", "statement_from": None},  # no date at all
    ]
    for index, case in enumerate(cases, start=1):
        rc = _rc(session, remittance, f"UNKNOWN-{index}", sequence=index, **case)
        assert match_remittance_claim(session, rc, remittance=remittance) is None, case
        assert rc.claim_id is None and rc.match_method is None


def test_fuzzy_prefers_the_remittance_payer_and_refuses_ambiguity(session, clinic):
    payer = _payer(session)
    other = _payer(session, "CB4321", "Contoso Benefits")
    patient = _patient(session, clinic, member_id="NHP0007")
    with_payer = _claim(session, clinic, payer, patient, "CLM-A")
    _claim(session, clinic, other, patient, "CLM-B")  # same patient, charge and dates, other payer
    remittance = _remittance(session, payer, "r1")
    rc = _rc(session, remittance, "UNKNOWN-A", member_id="NHP0007", statement_from=date(2026, 8, 4))
    assert match_remittance_claim(session, rc, remittance=remittance) is with_payer
    assert rc.match_method == "fuzzy"

    _claim(session, clinic, payer, patient, "CLM-C")  # now two candidates for the same payer
    rc2 = _rc(
        session, remittance, "UNKNOWN-B", sequence=2, member_id="NHP0007", statement_from=date(2026, 8, 4)
    )
    assert match_remittance_claim(session, rc2, remittance=remittance) is None
    assert rc2.match_method is None


def test_no_match_leaves_the_remittance_claim_unlinked(session, clinic):
    payer = _payer(session)
    _claim(session, clinic, payer, _patient(session, clinic), "CLM-1")
    rc = _rc(session, _remittance(session, payer, "r1"), "CLM-2")
    assert match_remittance_claim(session, rc) is None
    assert rc.claim_id is None and rc.match_method is None and rc.claim is None


def test_rematch_replaces_a_previous_link(session, clinic):
    payer = _payer(session)
    claim = _claim(session, clinic, payer, _patient(session, clinic), "CLM-1")
    rc = _rc(session, _remittance(session, payer, "r1"), "CLM-1")
    assert match_remittance_claim(session, rc) is claim
    rc.patient_control_number = "CLM-404"
    assert match_remittance_claim(session, rc) is None
    assert rc.claim_id is None and rc.match_method is None
