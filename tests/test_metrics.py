"""Dashboard metrics and the shadow-mode category agreement helper (milestone M8)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app import audit
from app.models import (
    Claim,
    ModelCall,
    Outcome,
    Patient,
    Payer,
    Remittance,
    RemittanceClaim,
    ReviewAction,
    WorkItem,
)
from app.outcomes import category_agreement, compute_dashboard
from app.outcomes.metrics import deadline_label, deadline_labels, labels_from_json
from app.schemas import DashboardMetrics

TODAY = date(2026, 10, 28)
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


class Builder:
    def __init__(self, session, clinic):
        self.session = session
        self.clinic = clinic
        self.payers: dict[str, Payer] = {}
        self.n = 0
        self.patient = Patient(
            clinic_id=clinic.id,
            token="PT-METRIC1",
            account_number="ACC-M1",
            first_name="Sam",
            last_name="Synthetic",
        )
        session.add(self.patient)
        self.remittance = Remittance(
            file_name="remit_m.835",
            file_sha256="m" * 64,
            payment_amount=Decimal("0"),
            payment_date=date(2026, 9, 1),
        )
        session.add(self.remittance)
        session.flush()

    def payer(self, name: str) -> Payer:
        if name not in self.payers:
            payer = Payer(name=name, payer_id=f"PAY-{len(self.payers) + 1}")
            self.session.add(payer)
            self.session.flush()
            self.payers[name] = payer
        return self.payers[name]

    def item(
        self,
        *,
        category,
        payer,
        status,
        dollars,
        deadline=None,
        submitted=False,
        decided=False,
        outcome=None,
        paid="0",
        review_seconds=None,
    ) -> WorkItem:
        self.n += 1
        claim = Claim(
            clinic_id=self.clinic.id,
            payer_id=self.payer(payer).id,
            patient_id=self.patient.id,
            claim_control_number=f"CLM-M{self.n:03d}",
            total_charge=Decimal(dollars),
            status="denied",
        )
        self.session.add(claim)
        self.session.flush()
        rc = RemittanceClaim(
            remittance=self.remittance,
            claim=claim,
            sequence=self.n,
            patient_control_number=claim.claim_control_number,
            claim_status_code="4",
            charge_amount=Decimal(dollars),
            paid_amount=Decimal("0"),
            is_denial=True,
            denied_amount=Decimal(dollars),
        )
        self.session.add(rc)
        self.session.flush()
        work_item = WorkItem(
            claim=claim,
            remittance_claim=rc,
            category=category,
            category_confidence=0.9,
            status=status,
            dollar_value=Decimal(dollars),
            deadline=deadline,
            submitted_at=NOW if submitted else None,
            decided_at=NOW if decided else None,
        )
        self.session.add(work_item)
        self.session.flush()
        if outcome:
            paid_rc = RemittanceClaim(
                remittance=self.remittance,
                claim=claim,
                sequence=100 + self.n,
                patient_control_number=claim.claim_control_number,
                claim_status_code="1",
                charge_amount=Decimal(dollars),
                paid_amount=Decimal(paid),
            )
            self.session.add(paid_rc)
            self.session.flush()
            self.session.add(
                Outcome(
                    work_item=work_item,
                    remittance_claim=paid_rc,
                    result=outcome,
                    paid_amount=Decimal(paid),
                    payment_date=date(2026, 9, 1),
                )
            )
        if review_seconds is not None:
            self.session.add(
                ReviewAction(work_item=work_item, action="approve", review_seconds=review_seconds)
            )
        self.session.flush()
        return work_item


@pytest.fixture()
def dataset(session, clinic):
    b = Builder(session, clinic)
    items = {
        "wi1": b.item(
            category="modifier", payer="Blue Synthetic", status="closed", dollars="100.00",
            submitted=True, decided=True, outcome="paid", paid="100.00", review_seconds=120,
        ),
        "wi2": b.item(
            category="modifier", payer="Blue Synthetic", status="closed", dollars="200.00",
            submitted=True, decided=True, outcome="partial", paid="50.00", review_seconds=240,
        ),
        "wi3": b.item(
            category="eligibility", payer="Acme Synthetic", status="submitted", dollars="300.00",
            deadline=TODAY + timedelta(days=10), submitted=True, decided=True,
        ),
        "wi4": b.item(
            category="eligibility", payer="Acme Synthetic", status="proposed", dollars="400.00",
            deadline=TODAY + timedelta(days=20),
        ),
        "wi5": b.item(
            category="authorization", payer="Blue Synthetic", status="rejected", dollars="500.00",
            deadline=TODAY - timedelta(days=1), decided=True,
        ),
        "wi6": b.item(category="other", payer="Acme Synthetic", status="proposed", dollars="50.00"),
        "wi7": b.item(
            category="authorization", payer="Acme Synthetic", status="closed", dollars="150.00",
            deadline=TODAY + timedelta(days=60), submitted=True, decided=True, outcome="denied_again", paid="0.00",
        ),
        "wi8": b.item(
            category="missing_information", payer="Acme Synthetic", status="new", dollars="10.00",
            deadline=TODAY - timedelta(days=5),
        ),
    }  # fmt: skip
    session.add_all(
        [
            ModelCall(
                prompt_name="classify_denial", prompt_version="classify_denial.v1@deadbeef", model_id="stub",
                purpose="classify", input_tokens=100, output_tokens=20,
            ),
            ModelCall(
                prompt_name="draft_appeal", prompt_version="draft_appeal.v1@deadbeef", model_id="stub",
                purpose="draft", input_tokens=50, output_tokens=10,
            ),
        ]
    )  # fmt: skip
    audit.record(session, actor_type="system", actor_id="tests", action="seeded")
    session.commit()
    return items


def test_dashboard_numbers(session, dataset):
    m = compute_dashboard(session, today=TODAY)
    assert isinstance(m, DashboardMetrics)
    assert m.total_work_items == 8
    assert m.items_by_status == {
        "new": 1, "proposed": 2, "in_review": 0, "approved": 0,
        "rejected": 1, "exported": 0, "submitted": 1, "closed": 3,
    }  # fmt: skip
    assert m.dollars_at_stake == Decimal("760.00")
    assert m.submitted_fixes == 4
    assert (m.paid_fixes, m.partial_fixes, m.denied_again) == (1, 1, 1)
    assert m.overturn_rate == 0.5
    assert m.recovered_dollars == Decimal("150.00")
    assert m.avg_review_minutes == 3.0
    assert m.reviewed_items == 5
    assert (m.model_calls, m.model_input_tokens, m.model_output_tokens) == (2, 150, 30)
    assert m.audit_chain_ok is True
    assert m.generated_at.tzinfo is not None


def test_by_category_and_payer(session, dataset):
    m = compute_dashboard(session, today=TODAY)

    assert [c.key for c in m.by_category] == [
        "eligibility",
        "other",
        "missing_information",
        "authorization",
        "modifier",
    ]
    cats = {c.key: c for c in m.by_category}
    assert (cats["modifier"].items, cats["modifier"].dollars_at_stake, cats["modifier"].submitted) == (
        2,
        Decimal("0"),
        2,
    )
    assert (cats["modifier"].paid, cats["modifier"].partial, cats["modifier"].recovered_dollars) == (
        1,
        1,
        Decimal("150.00"),
    )
    assert cats["modifier"].overturn_rate == 1.0
    assert (cats["eligibility"].items, cats["eligibility"].dollars_at_stake) == (2, Decimal("700.00"))
    assert cats["eligibility"].submitted == 1
    assert cats["authorization"].denied_again == 1 and cats["authorization"].overturn_rate == 0.0
    assert cats["other"].overturn_rate is None

    assert [p.key for p in m.by_payer] == ["Acme Synthetic", "Blue Synthetic"]
    payers = {p.key: p for p in m.by_payer}
    assert (payers["Blue Synthetic"].items, payers["Blue Synthetic"].submitted) == (3, 2)
    assert payers["Blue Synthetic"].recovered_dollars == Decimal("150.00")
    assert (payers["Acme Synthetic"].items, payers["Acme Synthetic"].dollars_at_stake) == (
        5,
        Decimal("760.00"),
    )
    assert payers["Acme Synthetic"].denied_again == 1


def test_deadline_buckets(session, dataset, settings):
    assert settings.warn_days == [30, 14]
    m = compute_dashboard(session, today=TODAY)
    assert [b.label for b in m.deadline_buckets] == ["overdue", "<=14 days", "<=30 days", ">30 days", "none"]
    buckets = {b.label: (b.items, b.dollars) for b in m.deadline_buckets}
    assert buckets["overdue"] == (1, Decimal("10.00"))
    assert buckets["<=14 days"] == (1, Decimal("300.00"))
    assert buckets["<=30 days"] == (1, Decimal("400.00"))
    assert buckets[">30 days"] == (0, Decimal("0"))
    assert buckets["none"] == (1, Decimal("50.00"))


def test_deadline_label_helper():
    assert deadline_labels([30, 14]) == ["overdue", "<=14 days", "<=30 days", ">30 days", "none"]
    assert deadline_labels([30]) == ["overdue", "<=30 days", ">30 days", "none"]
    assert deadline_label(None, TODAY, [30, 14]) == "none"
    assert deadline_label(TODAY - timedelta(days=1), TODAY, [30, 14]) == "overdue"
    assert deadline_label(TODAY, TODAY, [30, 14]) == "<=14 days"
    assert deadline_label(TODAY + timedelta(days=14), TODAY, [30, 14]) == "<=14 days"
    assert deadline_label(TODAY + timedelta(days=15), TODAY, [30, 14]) == "<=30 days"
    assert deadline_label(TODAY + timedelta(days=31), TODAY, [30, 14]) == ">30 days"


def test_empty_database(session):
    m = compute_dashboard(session, today=TODAY)
    assert m.total_work_items == 0
    assert m.overturn_rate is None and m.avg_review_minutes is None
    assert m.recovered_dollars == Decimal("0") and m.dollars_at_stake == Decimal("0")
    assert m.by_category == [] and m.by_payer == []
    assert sum(b.items for b in m.deadline_buckets) == 0
    assert (m.model_calls, m.model_input_tokens, m.model_output_tokens) == (0, 0, 0)
    assert m.audit_chain_ok is True
    assert m.model_dump(mode="json")["dollars_at_stake"] == "0"


def test_category_agreement(session, dataset):
    labels = {
        "CLM-M001": "modifier",
        "CLM-M002": "modifier",
        "CLM-M003": "authorization",  # disagrees (eligibility)
        "CLM-M007": {"category": "authorization", "expected_action": "appeal"},
        "CLM-M999": "other",  # no work item
    }
    share, compared = category_agreement(session, labels)
    assert compared == 4
    assert share == pytest.approx(0.75)
    assert category_agreement(session, {}) == (0.0, 0)
    assert category_agreement(session, {"CLM-M999": "other"}) == (0.0, 0)


def test_labels_from_json():
    records = [
        {"claim_control_number": "A", "category": "modifier", "expected_action": "corrected_claim"},
        {"claim_control_number": "B", "category": "other"},
        {"nope": 1},
    ]
    assert labels_from_json(records) == {"A": "modifier", "B": "other"}
    assert labels_from_json({"A": "modifier", "B": {"category": "other"}}) == {"A": "modifier", "B": "other"}
    assert labels_from_json({"labels": records}) == {"A": "modifier", "B": "other"}
    assert labels_from_json([]) == {}
