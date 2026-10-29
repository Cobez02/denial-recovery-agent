"""Dashboard metrics (milestone M8) and the Phase 2 shadow-mode comparison helper.

Definitions from the plan, counted per work item (a work item with several outcomes stands for its
latest one, so a reversal followed by a re-adjudication counts once):
    submitted fixes    = work items the reviewer marked submitted (submitted_at), open or closed since
    overturn rate      = (fixes whose latest outcome is paid or partial) / submitted fixes
    recovered dollars  = sum of the latest outcome's paid_amount over fixes paid or partially paid
    review time        = mean ReviewAction.review_seconds / 60
    category agreement = share of work items whose category equals the biller's label
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app import audit
from app.config import get_settings
from app.enums import OutcomeResult, WorkItemStatus
from app.models import Claim, ModelCall, Outcome, ReviewAction, WorkItem
from app.schemas import CategoryMetric, DashboardMetrics, DeadlineBucket

ZERO = Decimal("0")
CLOSED_STATUSES: frozenset[str] = frozenset({str(WorkItemStatus.CLOSED), str(WorkItemStatus.REJECTED)})
SUBMITTED_STATUSES: frozenset[str] = frozenset({str(WorkItemStatus.SUBMITTED), str(WorkItemStatus.CLOSED)})
RECOVERED_RESULTS: frozenset[str] = frozenset({str(OutcomeResult.PAID), str(OutcomeResult.PARTIAL)})
UNKNOWN_KEY = "unknown"
TOTAL_KEY = "all"


def is_open(work_item: WorkItem) -> bool:
    """Open = still represents money at stake (not closed, not rejected)."""
    return str(work_item.status) not in CLOSED_STATUSES


def is_submitted_fix(work_item: WorkItem) -> bool:
    """A fix the clinic sent to the payer: marked submitted, whether still submitted or closed since.

    An exported package the clinic never marked submitted is not a fix, whatever happened to the
    claim later; neither is an item the reviewer closed by hand without submitting it.
    """
    return str(work_item.status) in SUBMITTED_STATUSES and work_item.submitted_at is not None


def latest_outcome(work_item: WorkItem) -> Outcome | None:
    """The outcome that stands for the work item: the latest payment date, then the last recorded."""
    outcomes = list(work_item.outcomes)
    if not outcomes:
        return None
    return max(outcomes, key=lambda outcome: (outcome.payment_date or date.min, outcome.id or 0))


class _Group:
    """Accumulator behind one CategoryMetric (per category, per payer, or everything)."""

    def __init__(self, key: str) -> None:
        self.key = key
        self.items = 0
        self.dollars_at_stake = ZERO
        self.submitted = 0
        self.paid = 0
        self.partial = 0
        self.denied_again = 0
        self.recovered_dollars = ZERO

    def add(self, work_item: WorkItem) -> None:
        self.items += 1
        if is_open(work_item):
            self.dollars_at_stake += Decimal(work_item.dollar_value or 0)
        if not is_submitted_fix(work_item):
            return
        self.submitted += 1
        outcome = latest_outcome(work_item)
        if outcome is None:
            return
        result = str(outcome.result)
        if result == OutcomeResult.PAID:
            self.paid += 1
        elif result == OutcomeResult.PARTIAL:
            self.partial += 1
        elif result == OutcomeResult.DENIED_AGAIN:
            self.denied_again += 1
        if result in RECOVERED_RESULTS:
            self.recovered_dollars += Decimal(outcome.paid_amount or 0)

    @property
    def overturn_rate(self) -> float | None:
        return round((self.paid + self.partial) / self.submitted, 4) if self.submitted else None

    def to_metric(self) -> CategoryMetric:
        return CategoryMetric(
            key=self.key,
            items=self.items,
            dollars_at_stake=self.dollars_at_stake,
            submitted=self.submitted,
            paid=self.paid,
            partial=self.partial,
            denied_again=self.denied_again,
            recovered_dollars=self.recovered_dollars,
        )


def _grouped(items: list[WorkItem], key_of: Callable[[WorkItem], str | None]) -> list[CategoryMetric]:
    groups: dict[str, _Group] = {}
    for work_item in items:
        key = key_of(work_item) or UNKNOWN_KEY
        groups.setdefault(key, _Group(key)).add(work_item)
    metrics = [g.to_metric() for g in groups.values()]
    metrics.sort(key=lambda m: (-m.dollars_at_stake, m.key))
    return metrics


def _payer_key(work_item: WorkItem) -> str:
    claim = work_item.claim
    if claim is None or claim.payer is None:
        return UNKNOWN_KEY
    return claim.payer.name or claim.payer.payer_id or UNKNOWN_KEY


def deadline_labels(warn_days: list[int]) -> list[str]:
    """Bucket labels in display order, e.g. overdue, <=14 days, <=30 days, >30 days, none."""
    thresholds = sorted(set(warn_days))
    return ["overdue", *[f"<={d} days" for d in thresholds], f">{thresholds[-1]} days", "none"]


def deadline_label(deadline: date | None, today: date, warn_days: list[int]) -> str:
    thresholds = sorted(set(warn_days))
    if deadline is None:
        return "none"
    days = (deadline - today).days
    if days < 0:
        return "overdue"
    for d in thresholds:
        if days <= d:
            return f"<={d} days"
    return f">{thresholds[-1]} days"


def compute_dashboard(
    session: Session, *, today: date | None = None, verify_audit: bool = True
) -> DashboardMetrics:
    """Everything the dashboard page and GET /api/metrics show (docs/SPEC.md section 10).

    `verify_audit=False` leaves `audit_chain_ok` as None instead of walking the whole audit log
    (the audit page does the full verification).
    """
    settings = get_settings()
    warn_days = settings.warn_days
    today = today or datetime.now(UTC).date()

    stmt = (
        select(WorkItem)
        .options(selectinload(WorkItem.outcomes), selectinload(WorkItem.claim).selectinload(Claim.payer))
        .order_by(WorkItem.id)
    )
    items: list[WorkItem] = list(session.execute(stmt).scalars().unique())

    items_by_status: dict[str, int] = {str(s): 0 for s in WorkItemStatus}
    totals = _Group(TOTAL_KEY)
    reviewed_items = 0
    labels = deadline_labels(warn_days)
    bucket_items: dict[str, int] = {label: 0 for label in labels}
    bucket_dollars: dict[str, Decimal] = {label: ZERO for label in labels}

    for work_item in items:
        status = str(work_item.status)
        items_by_status[status] = items_by_status.get(status, 0) + 1
        totals.add(work_item)
        if is_open(work_item):
            label = deadline_label(work_item.deadline, today, warn_days)
            bucket_items[label] += 1
            bucket_dollars[label] += Decimal(work_item.dollar_value or 0)
        if work_item.decided_at is not None:
            reviewed_items += 1

    avg_seconds = session.execute(
        select(func.avg(ReviewAction.review_seconds)).where(ReviewAction.review_seconds.is_not(None))
    ).scalar()
    avg_review_minutes = round(float(avg_seconds) / 60.0, 2) if avg_seconds is not None else None

    calls, input_tokens, output_tokens = session.execute(
        select(
            func.count(ModelCall.id),
            func.coalesce(func.sum(ModelCall.input_tokens), 0),
            func.coalesce(func.sum(ModelCall.output_tokens), 0),
        )
    ).one()

    return DashboardMetrics(
        generated_at=datetime.now(UTC),
        total_work_items=len(items),
        items_by_status=items_by_status,
        dollars_at_stake=totals.dollars_at_stake,
        submitted_fixes=totals.submitted,
        paid_fixes=totals.paid,
        partial_fixes=totals.partial,
        denied_again=totals.denied_again,
        overturn_rate=totals.overturn_rate,
        recovered_dollars=totals.recovered_dollars,
        avg_review_minutes=avg_review_minutes,
        reviewed_items=reviewed_items,
        by_category=_grouped(items, lambda wi: str(wi.category) if wi.category else None),
        by_payer=_grouped(items, _payer_key),
        deadline_buckets=[
            DeadlineBucket(label=label, items=bucket_items[label], dollars=bucket_dollars[label])
            for label in labels
        ],
        model_calls=int(calls or 0),
        model_input_tokens=int(input_tokens or 0),
        model_output_tokens=int(output_tokens or 0),
        audit_chain_ok=(audit.verify_chain(session) is None) if verify_audit else None,
    )


def _label_category(label: Any) -> str | None:
    if label is None:
        return None
    if isinstance(label, Mapping):
        label = label.get("category")
        if label is None:
            return None
    return str(label)


def labels_from_json(data: Any) -> dict[str, str]:
    """Normalise a labels.json payload to {claim_control_number: category}.

    Accepts a mapping ccn -> category (or ccn -> record with a "category" key), a mapping with a
    "labels" entry holding either form, or a list of records with `claim_control_number`.
    """
    if isinstance(data, Mapping) and isinstance(data.get("labels"), Mapping | list):
        data = data["labels"]
    out: dict[str, str] = {}
    if isinstance(data, Mapping):
        for ccn, label in data.items():
            category = _label_category(label)
            if category is not None:
                out[str(ccn)] = category
        return out
    for record in data or []:
        if not isinstance(record, Mapping):
            continue
        ccn = record.get("claim_control_number") or record.get("ccn")
        category = _label_category(record)
        if ccn and category is not None:
            out[str(ccn)] = category
    return out


def category_agreement(session: Session, labels: Mapping[str, Any]) -> tuple[float, int]:
    """Share of work items whose category equals the label for their claim, and how many compared.

    `labels` maps claim_control_number -> category (a label may also be a dict with a "category"
    key, as in labels.json records). Work items whose claim has no label are skipped.
    """
    if not labels:
        return 0.0, 0
    stmt = (
        select(Claim.claim_control_number, WorkItem.category)
        .join(WorkItem, WorkItem.claim_id == Claim.id)
        .order_by(WorkItem.id)
    )
    compared = agreed = 0
    for ccn, category in session.execute(stmt):
        expected = _label_category(labels.get(ccn))
        if expected is None:
            continue
        compared += 1
        if str(category) == expected:
            agreed += 1
    return (agreed / compared if compared else 0.0), compared


__all__ = [
    "category_agreement",
    "compute_dashboard",
    "deadline_label",
    "deadline_labels",
    "is_open",
    "is_submitted_fix",
    "labels_from_json",
    "latest_outcome",
]
