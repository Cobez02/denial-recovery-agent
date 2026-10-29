"""Dashboard metrics and the shadow-mode category agreement helper (milestone M8)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

import app.outcomes
from app import audit
from app.api.routes import metrics_json
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
from app.outcomes import category_agreement, compute_dashboard, matching
from app.outcomes.metrics import (
    deadline_label,
    deadline_labels,
    is_submitted_fix,
    labels_from_json,
    latest_outcome,
    taken_back_dollars,
)
from app.schemas import DashboardMetrics

TODAY = date(2026, 10, 28)
NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)
PAID_ON = date(2026, 9, 1)
PAID_LATER = date(2026, 9, 15)
PAID_AGAIN = date(2026, 9, 30)


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
        outcomes=None,
        review_seconds=None,
    ) -> WorkItem:
        """`outcome`/`paid` add one outcome row; `outcomes` adds several as (result, paid, payment_date)."""
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
        rows = list(outcomes or [])
        if outcome:
            rows.insert(0, (outcome, paid, PAID_ON))
        for index, (result, amount, payment_date) in enumerate(rows):
            paid_rc = RemittanceClaim(
                remittance=self.remittance,
                claim=claim,
                sequence=100 + 10 * self.n + index,
                patient_control_number=claim.claim_control_number,
                claim_status_code="22" if result == "reversed" else "1",
                charge_amount=Decimal(dollars),
                paid_amount=Decimal(amount),
            )
            self.session.add(paid_rc)
            self.session.flush()
            self.session.add(
                Outcome(
                    work_item=work_item,
                    remittance_claim=paid_rc,
                    result=result,
                    paid_amount=Decimal(amount),
                    payment_date=payment_date,
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
        # A reversal followed by the re-adjudication: one fix, counted once, by its latest outcome.
        "wi9": b.item(
            category="modifier", payer="Blue Synthetic", status="closed", dollars="80.00",
            submitted=True, decided=True,
            outcomes=[("reversed", "-80.00", PAID_ON), ("paid", "80.00", PAID_LATER)],
        ),
        # Reversed and not yet re-adjudicated: still a submitted fix, still open, nothing recovered.
        "wi10": b.item(
            category="eligibility", payer="Acme Synthetic", status="submitted", dollars="120.00",
            deadline=TODAY + timedelta(days=40), submitted=True, decided=True,
            outcomes=[("reversed", "-120.00", PAID_ON)],
        ),
        # Closed by the reviewer without ever being submitted: not a fix, whatever the payer did.
        "wi11": b.item(
            category="bundling", payer="Blue Synthetic", status="closed", dollars="40.00",
            outcomes=[("paid", "40.00", PAID_ON)],
        ),
        # Two closing outcomes (a re-delivered 835, say): the latest stands, the payment is not doubled.
        "wi12": b.item(
            category="modifier", payer="Blue Synthetic", status="closed", dollars="90.00",
            submitted=True, decided=True,
            outcomes=[("paid", "90.00", PAID_ON), ("partial", "30.00", PAID_LATER)],
        ),
        # Paid, then taken back by a later 835 (the take-back reopened it): a reversed fix, open
        # again, 80.00 taken back, nothing recovered. (wi10 is the other reversed fix: its only
        # outcome is the reversal of the original adjudication, so nothing was taken back.)
        "wi13": b.item(
            category="modifier", payer="Blue Synthetic", status="submitted", dollars="80.00",
            deadline=TODAY + timedelta(days=40), submitted=True, decided=True,
            outcomes=[("paid", "80.00", PAID_ON), ("reversed", "-80.00", PAID_LATER)],
        ),
        # Paid, taken back, paid again: one paid fix, recovered once, not a reversed fix.
        "wi14": b.item(
            category="modifier", payer="Blue Synthetic", status="closed", dollars="70.00",
            submitted=True, decided=True,
            outcomes=[("paid", "70.00", PAID_ON), ("reversed", "-70.00", PAID_LATER), ("paid", "70.00", PAID_AGAIN)],
        ),
        # Paid, then re-adjudicated as partial, then taken back: the most recent payment before the
        # reversal (30.00) is what was taken back.
        "wi15": b.item(
            category="authorization", payer="Acme Synthetic", status="submitted", dollars="100.00",
            deadline=TODAY + timedelta(days=5), submitted=True, decided=True,
            outcomes=[("paid", "80.00", PAID_ON), ("partial", "30.00", PAID_LATER), ("reversed", "-30.00", PAID_AGAIN)],
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
    assert m.total_work_items == 15
    assert m.items_by_status == {
        "new": 1, "proposed": 2, "in_review": 0, "approved": 0,
        "rejected": 1, "exported": 0, "submitted": 4, "closed": 7,
    }  # fmt: skip
    # Open items: wi3, wi4, wi6, wi8, wi10 (880.00) plus the taken-back wi13 and wi15, open again.
    assert m.dollars_at_stake == Decimal("1060.00")
    # wi1, wi2, wi3, wi7, wi9, wi10, wi12, wi13, wi14, wi15 were marked submitted; wi11 was closed
    # without a submission.
    assert m.submitted_fixes == 10
    # Per work item by its latest outcome: wi1 + wi9 + wi14 paid, wi2 + wi12 partial, wi7 denied
    # again; wi10, wi13 and wi15 (reversed, pending) are in the denominator only; wi11's payment is
    # nobody's fix.
    assert (m.paid_fixes, m.partial_fixes, m.denied_again) == (3, 2, 1)
    assert m.overturn_rate == round(5 / 10, 4)
    assert m.recovered_dollars == Decimal("100.00") + Decimal("50.00") + Decimal("80.00") + Decimal(
        "30.00"
    ) + Decimal("70.00")
    # The take-backs: wi13 (80.00 paid, then reversed), wi15 (80.00, then 30.00, then reversed: the
    # 30.00 is what was taken back) and wi10 (reversed before anything was paid: nothing).
    assert m.reversed_fixes == 3
    assert m.taken_back_dollars == Decimal("80.00") + Decimal("30.00")
    assert m.avg_review_minutes == 3.0
    assert m.reviewed_items == 11
    assert (m.model_calls, m.model_input_tokens, m.model_output_tokens) == (2, 150, 30)
    assert m.audit_chain_ok is True
    assert m.generated_at.tzinfo is not None


def test_dashboard_without_audit_verification(session, dataset):
    m = compute_dashboard(session, today=TODAY, verify_audit=False)
    assert m.audit_chain_ok is None and m.submitted_fixes == 10


def test_taken_back_dollars_is_the_last_payment_before_the_reversal(session, dataset):
    """Finding: what a reversed fix had been paid, 0 when nothing was, 0 when the fix is not reversed."""
    assert taken_back_dollars(dataset["wi13"]) == Decimal("80.00")
    assert taken_back_dollars(dataset["wi15"]) == Decimal("30.00")  # the partial 30.00, not the 80.00
    assert taken_back_dollars(dataset["wi10"]) == Decimal("0")  # reversed before any payment
    assert taken_back_dollars(dataset["wi14"]) == Decimal("0")  # paid again: not reversed any more
    assert taken_back_dollars(dataset["wi1"]) == Decimal("0")
    assert taken_back_dollars(dataset["wi4"]) == Decimal("0")  # no outcome at all
    assert latest_outcome(dataset["wi13"]).result == "reversed"
    assert latest_outcome(dataset["wi14"]).result == "paid"
    assert latest_outcome(dataset["wi15"]).result == "reversed"


def test_metrics_json_carries_the_take_back_fields(session, dataset):
    data = metrics_json(compute_dashboard(session, today=TODAY, verify_audit=False))
    assert data["reversed_fixes"] == 3
    assert Decimal(data["taken_back_dollars"]) == Decimal("110.00")
    assert Decimal(data["recovered_dollars"]) == Decimal("330.00")
    assert all("reversed" not in row and "taken_back_dollars" not in row for row in data["by_category"])


def test_latest_outcome_is_one_definition():
    """The matcher and the metrics share one latest_outcome; the old import paths keep working."""
    assert latest_outcome is matching.latest_outcome is app.outcomes.latest_outcome


def test_counts_are_per_work_item_by_latest_outcome(session, dataset):
    """Finding: one submitted item with two outcome rows counted once, as its latest outcome says."""
    reversed_then_paid, reversed_only = dataset["wi9"], dataset["wi10"]
    paid_then_partial, never_submitted = dataset["wi12"], dataset["wi11"]
    assert [o.result for o in sorted(reversed_then_paid.outcomes, key=lambda o: o.id)] == ["reversed", "paid"]
    assert latest_outcome(reversed_then_paid).result == "paid"
    assert latest_outcome(reversed_only).result == "reversed"
    assert latest_outcome(paid_then_partial).result == "partial"
    assert latest_outcome(dataset["wi4"]) is None
    assert is_submitted_fix(reversed_only) and is_submitted_fix(reversed_then_paid)
    assert not is_submitted_fix(never_submitted) and not is_submitted_fix(dataset["wi4"])

    taken_back, repaid, partial_then_taken_back = dataset["wi13"], dataset["wi14"], dataset["wi15"]
    assert latest_outcome(taken_back).result == "reversed" and is_submitted_fix(taken_back)
    assert (
        latest_outcome(repaid).result == "paid"
        and latest_outcome(partial_then_taken_back).result == "reversed"
    )

    m = compute_dashboard(session, today=TODAY)
    modifier = next(c for c in m.by_category if c.key == "modifier")
    assert (modifier.items, modifier.submitted, modifier.paid, modifier.partial) == (6, 6, 3, 2)
    # 100 + 50 + 80 + 30 + 70: never 90 + 30, never 80 twice, nothing for the taken-back wi13 and the
    # re-paid wi14 only once.
    assert modifier.recovered_dollars == Decimal("330.00")
    bundling = next(c for c in m.by_category if c.key == "bundling")
    assert (bundling.items, bundling.submitted, bundling.paid, bundling.recovered_dollars) == (
        1,
        0,
        0,
        Decimal("0"),
    )
    assert bundling.overturn_rate is None
    assert m.overturn_rate is not None and m.overturn_rate <= 1.0


def test_latest_outcome_orders_by_payment_date_then_record(session, dataset):
    work_item = dataset["wi12"]
    first, second = sorted(work_item.outcomes, key=lambda o: o.id)
    assert (first.result, second.result) == ("paid", "partial")
    second.payment_date = first.payment_date  # same day: the row recorded last stands
    assert latest_outcome(work_item) is second
    second.payment_date = None  # an undated outcome never outranks a dated one
    assert latest_outcome(work_item) is first


def test_by_category_and_payer(session, dataset):
    m = compute_dashboard(session, today=TODAY)

    assert [c.key for c in m.by_category] == [
        "eligibility",
        "authorization",
        "modifier",
        "other",
        "missing_information",
        "bundling",
    ]
    cats = {c.key: c for c in m.by_category}
    # The taken-back wi13 is open again: its 80.00 is at stake for the modifier category.
    assert (cats["modifier"].items, cats["modifier"].dollars_at_stake, cats["modifier"].submitted) == (
        6,
        Decimal("80.00"),
        6,
    )
    assert (cats["modifier"].paid, cats["modifier"].partial, cats["modifier"].recovered_dollars) == (
        3,
        2,
        Decimal("330.00"),
    )
    assert cats["modifier"].overturn_rate == pytest.approx(5 / 6)  # wi13, taken back: denominator only
    assert (cats["eligibility"].items, cats["eligibility"].dollars_at_stake) == (3, Decimal("820.00"))
    assert cats["eligibility"].submitted == 2 and cats["eligibility"].overturn_rate == 0.0
    assert (cats["authorization"].items, cats["authorization"].dollars_at_stake) == (3, Decimal("100.00"))
    assert cats["authorization"].submitted == 2 and cats["authorization"].overturn_rate == 0.0
    assert cats["authorization"].denied_again == 1 and cats["authorization"].recovered_dollars == Decimal("0")
    assert cats["other"].overturn_rate is None

    assert [p.key for p in m.by_payer] == ["Acme Synthetic", "Blue Synthetic"]
    payers = {p.key: p for p in m.by_payer}
    assert (payers["Blue Synthetic"].items, payers["Blue Synthetic"].submitted) == (8, 6)
    assert (payers["Blue Synthetic"].paid, payers["Blue Synthetic"].partial) == (3, 2)
    assert payers["Blue Synthetic"].recovered_dollars == Decimal("330.00")
    assert payers["Blue Synthetic"].dollars_at_stake == Decimal("80.00")
    assert (payers["Acme Synthetic"].items, payers["Acme Synthetic"].dollars_at_stake) == (
        7,
        Decimal("980.00"),
    )
    assert payers["Acme Synthetic"].submitted == 4 and payers["Acme Synthetic"].denied_again == 1
    assert payers["Acme Synthetic"].recovered_dollars == Decimal("0")


def test_deadline_buckets(session, dataset, settings):
    assert settings.warn_days == [30, 14]
    m = compute_dashboard(session, today=TODAY)
    assert [b.label for b in m.deadline_buckets] == ["overdue", "<=14 days", "<=30 days", ">30 days", "none"]
    buckets = {b.label: (b.items, b.dollars) for b in m.deadline_buckets}
    assert buckets["overdue"] == (1, Decimal("10.00"))
    assert buckets["<=14 days"] == (2, Decimal("400.00"))  # wi3 and the taken-back wi15, open again
    assert buckets["<=30 days"] == (1, Decimal("400.00"))
    # The reversed, still-submitted wi10 and the taken-back wi13 are open.
    assert buckets[">30 days"] == (2, Decimal("200.00"))
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
