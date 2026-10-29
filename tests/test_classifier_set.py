"""Classifier test set (plan testing table; docs/SPEC.md section 7, M4 done-when).

Every labeled denial of the committed synthetic set is imported and processed with the rules engine
plus the stub model. The work item category must agree with the generator's ground truth on at least
90% of the claims, and the proposed action must agree with the expected action on at least 80%.

The bars are fixed. When one is missed the fix belongs in app/classify/rules.yaml or app/playbooks,
and the failure output lists every mismatch (claim, scenario, label, prediction, rule id, codes) plus
a confusion summary so the rule or playbook at fault can be found directly.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import select

from app.models import AuditLog
from app.outcomes.metrics import category_agreement, labels_from_json
from tests import helpers

CATEGORY_ACCURACY_BAR = 0.90
ACTION_AGREEMENT_BAR = 0.80


@pytest.fixture(scope="module")
def run(tmp_path_factory) -> Iterator[helpers.FixtureRun]:
    with helpers.fixture_run(tmp_path_factory.mktemp("classifier_set")) as state:
        assert len(state.created) == len(state.labels) == len(state.by_ccn), "one work item per label"
        yield state


@pytest.fixture(scope="module")
def classified_details(run) -> dict[int, dict[str, Any]]:
    """The ``classified`` audit event details (rule id, source, confidence) by work item id."""
    stmt = select(AuditLog).where(AuditLog.action == "classified")
    return {event.entity_id: dict(event.details) for event in run.session.execute(stmt).scalars()}


def _codes(work_item) -> str:
    """``CO-16|M119; CO-45`` from the work item's stored reasons."""
    parts = []
    for reason in work_item.all_reasons or []:
        rarcs = "|".join(str(code) for code in reason.get("rarcs") or [])
        parts.append(f"{reason.get('group_code')}-{reason.get('carc')}" + (f"|{rarcs}" if rarcs else ""))
    return "; ".join(parts)


def _label_codes(label: dict[str, Any]) -> str:
    rarcs = "|".join(label.get("rarcs") or [])
    return f"{label['carc']}" + (f"|{rarcs}" if rarcs else "") + f" ({label['level']})"


def _format_confusion(confusion: Counter) -> str:
    rows = [
        f"  {expected:>26} -> {predicted:<26} {count:>4}"
        for (expected, predicted), count in sorted(confusion.items())
    ]
    return "confusion (label -> predicted: count):\n" + "\n".join(rows)


def _format_mismatches(title: str, rows: list[str]) -> str:
    return f"{title} ({len(rows)}):\n" + ("\n".join(f"  {row}" for row in rows) if rows else "  none")


def test_category_accuracy_meets_bar(run, classified_details):
    labels = run.labels
    confusion: Counter = Counter()
    per_category_total: Counter = Counter()
    per_category_hits: Counter = Counter()
    mismatches: list[str] = []
    agreed = 0
    for label in labels:
        ccn = label["claim_control_number"]
        work_item = run.by_ccn[ccn]
        expected, predicted = label["category"], str(work_item.category)
        confusion[(expected, predicted)] += 1
        per_category_total[expected] += 1
        if predicted == expected:
            agreed += 1
            per_category_hits[expected] += 1
        else:
            details = classified_details.get(work_item.id, {})
            mismatches.append(
                f"{ccn} scenario={label['scenario_id']} label={expected} predicted={predicted} "
                f"rule_id={details.get('rule_id')} source={details.get('source')} "
                f"confidence={details.get('confidence')} label_codes={_label_codes(label)} "
                f"remit_codes={_codes(work_item)}"
            )
    accuracy = agreed / len(labels)
    per_category = "\n".join(
        f"  {category:<26} {per_category_hits[category]:>3}/{per_category_total[category]:<3}"
        for category in sorted(per_category_total)
    )
    report = (
        f"category accuracy {accuracy:.3f} ({agreed}/{len(labels)}), bar {CATEGORY_ACCURACY_BAR:.2f}\n"
        f"per category:\n{per_category}\n{_format_confusion(confusion)}\n"
        f"{_format_mismatches('mismatches', mismatches)}"
    )
    print(report)
    assert accuracy >= CATEGORY_ACCURACY_BAR, report

    # The dashboard's shadow-mode helper measures the same thing from the database.
    agreement, compared = category_agreement(run.session, labels_from_json(labels))
    assert compared == len(labels)
    assert agreement == pytest.approx(accuracy)


def test_expected_action_agreement_meets_bar(run):
    labels = run.labels
    confusion: Counter = Counter()
    mismatches: list[str] = []
    agreed = 0
    for label in labels:
        ccn = label["claim_control_number"]
        work_item = run.by_ccn[ccn]
        proposal = work_item.current_proposal
        expected, proposed = label["expected_action"], str(proposal.action_type)
        confusion[(expected, proposed)] += 1
        if proposed == expected:
            agreed += 1
        else:
            mismatches.append(
                f"{ccn} scenario={label['scenario_id']} expected={expected} proposed={proposed} "
                f"category={work_item.category} flags={list(work_item.flags)} "
                f"label_codes={_label_codes(label)} remit_codes={_codes(work_item)}"
            )
    agreement = agreed / len(labels)
    report = (
        f"expected-action agreement {agreement:.3f} ({agreed}/{len(labels)}), bar {ACTION_AGREEMENT_BAR:.2f}\n"
        f"{_format_confusion(confusion)}\n{_format_mismatches('action mismatches', mismatches)}"
    )
    print(report)
    assert agreement >= ACTION_AGREEMENT_BAR, report
