"""End-to-end synthetic loop (plan testing table; docs/SPEC.md section 13; M8 done-when).

The whole Phase 1 loop runs over HTTP against the committed 200-claim synthetic batch with the stub
model and inline jobs, in one private database that the phases of this module share (``flow``).
The phases run in file order and each one builds on the state the previous ones left behind:

1. imports through the UI: claims, notes, payer documents and the four 835 files; 150 proposals
2. queue order and filters, and the injection banner on every item with a planted payer letter
3. a representative review sample (every action type, every actionable category, and a planted-letter
   claim for every action type that has one) worked through the item pages: views, note excerpts,
   approvals, rejections, an edit of the summary and the letter, a regeneration that consults the
   model again, a reopen, a close
4. an export package for every approved item, with the identifier rules checked per member
5. the corrected claims and appeals marked submitted by the reviewer
6. follow-up 835s that pay, partially pay, deny again or reverse each submitted fix; outcomes
7. the dashboard page and the JSON metrics
8. the audit log: every step recorded, rows append-only, chain intact, visible on /audit
9. the PHI guard: zero batch identifiers in any model payload or log record of the whole run

Every name, member id, address and date of birth in the batch is synthetic. Nothing here submits a
claim: approvals produce zip packages and the reviewer marks them submitted by hand.
"""

from __future__ import annotations

import csv
import io
import json
import re
import time
import zipfile
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import func, select

from app import audit
from app.audit import AuditImmutableError
from app.drafting.pdf import weasyprint_available
from app.enums import ACTIONABLE_CATEGORIES, ActionType, OutcomeResult, WorkItemStatus
from app.models import AuditLog, ImportBatch, ModelCall, Outcome, Payer, ReviewAction, WorkItem
from app.outcomes.matching import CLAIM_STATUS_FOR_RESULT, PAID_THRESHOLD
from app.review.filters import label as label_text
from app.review.filters import money, pct
from app.schemas import CLAIM_EXPORT_COLUMNS
from synthetic.followup import (
    PLAN_DENIED_AGAIN,
    PLAN_PAID,
    PLAN_PARTIAL,
    PLAN_VALUES,
    generate_followup_files,
)
from tests import helpers

# The committed fixture (synthetic/fixtures/labeled_set_v1/manifest.json).
EXPECTED_CLAIMS = 200
EXPECTED_LINES = 444
EXPECTED_PATIENTS = 140
EXPECTED_PAYERS = 4
EXPECTED_NOTES = 174
EXPECTED_DOCUMENTS = 68
EXPECTED_PLANTED = 14
EXPECTED_DENIALS = 150
EXPECTED_REMIT_FILES = 4

SAMPLE_PER_GROUP = 2  # items per (category, action type) group: 34 on this fixture
MIN_SAMPLE = 25
MIN_PAYLOADS = 10
MIN_LOG_RECORDS = 100
REVIEW_PAUSE_SECONDS = 1.05  # every decision lands at least one second after the first view
FOLLOWUP_LAG_DAYS = 30
DOCUMENT_LINE_MIN_CHARS = 60  # payer-letter lines this long are body text, never a shared header
NOTE_FALLBACK = "Separate problem-focused evaluation documented before the destruction procedure."
INJECTION_BANNER = "Possible planted instructions"
HX = helpers.HX_HEADERS

# Roles a sampled item plays in the review phase and the status each one ends in.
APPROVE, REJECT, REJECT_REOPEN_APPROVE, CLOSE, EDIT_APPROVE, REGENERATE_APPROVE = (
    "approve",
    "reject",
    "reject_reopen_approve",
    "close",
    "edit_approve",
    "regenerate_approve",
)
FINAL_STATUS: dict[str, str] = {
    APPROVE: "approved",
    REJECT: "rejected",
    REJECT_REOPEN_APPROVE: "approved",
    CLOSE: "closed",
    EDIT_APPROVE: "approved",
    REGENERATE_APPROVE: "approved",
}
ROLE_ACTIONS: dict[str, set[str]] = {
    APPROVE: {"view", "approve"},
    REJECT: {"view", "reject"},
    REJECT_REOPEN_APPROVE: {"view", "reject", "reopen", "approve"},
    CLOSE: {"view", "close"},
    EDIT_APPROVE: {"view", "edit", "approve"},
    REGENERATE_APPROVE: {"view", "regenerate", "approve"},
}
REJECT_REASON = "payer policy excludes this service; not worth an appeal"
REOPEN_REASON = "second look requested by the biller"
CLOSE_REASON = "timely filing window has passed; written off"
EDIT_REASON = "clearer summary for the payer"
EDIT_SUFFIX = " Resubmit with the enclosed documentation."
LETTER_SENTENCE = "Supporting documentation is enclosed with this appeal."
MODEL_CALL_ACTOR = ("agent", "llm:stub")  # app.llm.guard: "llm:" + the client's name
REVIEW_EVENTS = (
    "work_item_viewed",
    "work_item_approved",
    "work_item_rejected",
    "work_item_reopened",
    "work_item_closed",
    "proposal_edited",
    "proposal_regenerate_requested",
    "note_attached",
    "package_exported",
    "work_item_submitted",
)
FIRST_RUN_EVENTS = ("work_item_created", "classified", "proposal_created")
PIPELINE_EVENTS = (*FIRST_RUN_EVENTS, "proposal_regenerated")

JOB_RE = re.compile(r"is <strong>(\w+)</strong>")
BATCH_RE = re.compile(r"Import batch (\d+): <strong>(\w+)</strong>")
PIPELINE_RE = re.compile(r"Pipeline: <strong>(\w+)</strong>")
CREATED_RE = re.compile(r"(\d+) work item\(s\) created")
BATCH_ROW_RE = re.compile(
    r"<tr>\s*<td>(\d+)</td>\s*<td>(\w+)</td>\s*<td>([^<]*)</td>\s*<td><span class=\"status status-([\w-]+)\">"
)
AUDIT_ROW_RE = re.compile(r"<tr>\s*<td>(\d+)</td>\s*<td>\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC</td>")
LINE_PATH_RE = re.compile(r"^lines\[(\d+)\]\.(\w+)$")  # corrected-claim change paths (app.drafting)
MEMBER_ID_SHAPE = re.compile(r"\b[A-Z]{1,4}\d{6,}\b")  # what app.logging_config masks as [ID]
_WS = re.compile(r"\s+")


# --------------------------------------------------------------------------------------
# Shared state
# --------------------------------------------------------------------------------------


@dataclass
class Flow:
    """What the phases hand each other: the run, the CSRF token and what has been done so far."""

    run: helpers.HttpRun
    token: str
    done: set[str] = field(default_factory=set)
    uploads: dict[str, dict[str, Any]] = field(default_factory=dict)  # upload name -> job as shown
    viewed: set[int] = field(default_factory=set)
    planted: set[int] = field(default_factory=set)  # items whose payer letter carries planted text
    planted_actions: set[str] = field(default_factory=set)  # the action types those items propose
    sample: list[int] = field(default_factory=list)
    roles: dict[int, str] = field(default_factory=dict)
    edited: int | None = None
    regenerated: int | None = None
    approved: list[int] = field(default_factory=list)
    rejected: list[int] = field(default_factory=list)
    counts: Counter[str] = field(default_factory=Counter)  # review events performed, by audit action
    exported: dict[int, list[str]] = field(default_factory=dict)
    submitted: list[int] = field(default_factory=list)
    plan: dict[int, str] = field(default_factory=dict)
    followup_files: list[str] = field(default_factory=list)
    re_denials: list[int] = field(default_factory=list)

    @property
    def client(self) -> Any:
        return self.run.client

    @property
    def session(self) -> Any:
        return self.run.session


@pytest.fixture(scope="module")
def flow(tmp_path_factory) -> Iterator[Flow]:
    """One database, one logged-in reviewer, one run of the whole loop for this module."""
    with helpers.http_run(tmp_path_factory.mktemp("e2e")) as run:
        yield Flow(run=run, token=helpers.csrf_token(run.client))


def _require(flow: Flow, *phases: str) -> None:
    missing = [phase for phase in phases if phase not in flow.done]
    assert not missing, f"earlier phase(s) did not complete: {missing}; run this module as a whole, in order"


def _fresh(flow: Flow) -> None:
    """The app commits in sessions of its own: drop everything this session cached."""
    flow.session.expire_all()


def _item(flow: Flow, item_id: int) -> WorkItem:
    _fresh(flow)
    work_item = flow.session.get(WorkItem, item_id)
    assert work_item is not None, item_id
    return work_item


def _count(session: Any, model: Any) -> int:
    return int(session.execute(select(func.count()).select_from(model)).scalar_one())


def _action_counts(session: Any) -> Counter[str]:
    return Counter(session.execute(select(AuditLog.action)).scalars())


def _work_items(session: Any) -> list[WorkItem]:
    return list(session.execute(select(WorkItem).order_by(WorkItem.id)).scalars())


def _needs_note(work_item: WorkItem) -> bool:
    proposal = work_item.current_proposal
    return bool(proposal is not None and (proposal.requires_note_excerpt or proposal.coding_change))


def _note_text(work_item: WorkItem) -> str:
    """The reviewer quotes the claim's own clinical note (the fixture supplies one for coding changes)."""
    notes = sorted(work_item.claim.notes, key=lambda note: note.id or 0)
    return notes[0].text if notes else NOTE_FALLBACK


def _normalize(text: str) -> str:
    return _WS.sub(" ", text).strip().lower()


def _ok(response: Any, *context: Any) -> None:
    assert 200 <= response.status_code < 400, (response.status_code, *context)


def _upload(flow: Flow, kind: str, name: str, data: bytes) -> dict[str, Any]:
    """POST one file to /imports/<kind> and read the job result back from the rendered page."""
    response = flow.client.post(
        f"/imports/{kind}",
        data={"_csrf": flow.token},
        files={"file": (name, data, "application/octet-stream")},
    )
    assert response.status_code == 200, (kind, name, response.status_code)
    html = response.text
    job = JOB_RE.search(html)
    batch = BATCH_RE.search(html)
    pipeline = PIPELINE_RE.search(html)
    created = CREATED_RE.search(html)
    assert job and batch and pipeline and created, (kind, name)
    assert job.group(1) == "finished", (kind, name, job.group(1))
    assert name in html
    return {
        "batch_id": int(batch.group(1)),
        "batch_status": batch.group(2),
        "pipeline": pipeline.group(1),
        "work_items_created": int(created.group(1)),
    }


# --------------------------------------------------------------------------------------
# 1. Imports through the UI (M3 done-when, over HTTP)
# --------------------------------------------------------------------------------------


def test_1_imports_through_the_ui(flow: Flow) -> None:
    client, session, batch_dir = flow.client, flow.session, flow.run.batch_dir
    manifest = helpers.load_manifest(batch_dir)
    remits = [path.name for path in helpers.remit_files(batch_dir)]
    assert len(remits) == EXPECTED_REMIT_FILES
    uploads = [
        ("claims", helpers.CLAIMS_FILE),
        ("notes", helpers.NOTES_FILE),
        ("documents", helpers.DOCUMENTS_FILE),
        *(("835", name) for name in remits),
    ]
    for kind, name in uploads:
        flow.uploads[name] = _upload(flow, kind, name, (batch_dir / name).read_bytes())

    _fresh(flow)
    batches = {name: session.get(ImportBatch, job["batch_id"]) for name, job in flow.uploads.items()}
    assert all(batch is not None and batch.status == "done" for batch in batches.values())
    assert all(batch.created_by == f"user:{flow.run.reviewer.id}" for batch in batches.values())
    assert all(job["batch_status"] == "done" and job["pipeline"] == "ok" for job in flow.uploads.values())

    claims = batches[helpers.CLAIMS_FILE].counts
    assert claims["claims_created"] == EXPECTED_CLAIMS == manifest["counts"]["claims"]
    assert claims["lines"] == EXPECTED_LINES and claims["patients_created"] == EXPECTED_PATIENTS
    assert claims["payers_created"] == EXPECTED_PAYERS and claims["warnings"] == 0
    assert batches[helpers.NOTES_FILE].counts["notes"] == EXPECTED_NOTES
    documents = batches[helpers.DOCUMENTS_FILE].counts
    assert documents["documents"] == EXPECTED_DOCUMENTS and documents["flagged"] == EXPECTED_PLANTED
    # Nothing to classify before the remittances arrive; then every denial becomes a work item.
    assert all(flow.uploads[name]["work_items_created"] == 0 for _, name in uploads[:3])
    remit_counts = {name: batches[name].counts for name in remits}
    assert all(counts["unmatched"] == 0 and counts["outcomes"] == 0 for counts in remit_counts.values())
    assert sum(counts["claims"] for counts in remit_counts.values()) == EXPECTED_CLAIMS
    assert sum(counts["matched"] for counts in remit_counts.values()) == EXPECTED_CLAIMS
    assert sum(counts["denials"] for counts in remit_counts.values()) == EXPECTED_DENIALS
    assert sum(flow.uploads[name]["work_items_created"] for name in remits) == EXPECTED_DENIALS

    # The imports page: four 835 batches done, nothing unmatched, nothing left to process.
    page = client.get("/imports")
    assert page.status_code == 200
    rows = BATCH_ROW_RE.findall(page.text)
    remit_rows = [(file_name, status) for _, kind, file_name, status in rows if kind == "835"]
    assert sorted(remit_rows) == [(name, "done") for name in sorted(remits)]
    assert len([row for row in rows if row[1] in ("claims", "notes", "payer_documents")]) == 3
    assert "Every remittance claim is matched." in page.text
    assert "0 unmatched remittance claim(s)." in page.text
    assert "0 denied, matched remittance claim(s) without a work item." in page.text

    # The queue: 150 proposed items, one per labeled denial, each with a cited proposal.
    queue = client.get("/queue")
    assert queue.status_code == 200
    assert len(helpers.queue_item_ids(queue.text)) == EXPECTED_DENIALS
    assert f"<b>{EXPECTED_DENIALS}</b> Proposed" in queue.text
    by_ccn = helpers.work_items_by_ccn(session)
    assert set(by_ccn) == set(flow.run.label_by_ccn) and len(by_ccn) == EXPECTED_DENIALS
    for ccn, work_item in by_ccn.items():
        assert work_item.status == str(WorkItemStatus.PROPOSED), ccn
        proposal = work_item.current_proposal
        assert proposal is not None and proposal.version == 1 and proposal.citations, ccn
        assert work_item.category == flow.run.label_by_ccn[ccn]["category"], ccn
    assert audit.verify_chain(session) is None
    flow.done.add("imports")


# --------------------------------------------------------------------------------------
# 2. Queue ordering, filters and the injection banner
# --------------------------------------------------------------------------------------


def _queue_order_key(work_item: WorkItem) -> tuple[Any, ...]:
    """SPEC 10: deadline asc (NULLs last), dollars desc, confidence desc; the id breaks ties."""
    return (
        work_item.deadline is None,
        work_item.deadline or date.max,
        -Decimal(work_item.dollar_value or 0),
        -Decimal(str(work_item.category_confidence or 0)),
        work_item.id,
    )


def test_2_queue_order_filters_and_injection_banner(flow: Flow) -> None:
    _require(flow, "imports")
    client, session = flow.client, flow.session
    _fresh(flow)
    items = _work_items(session)
    by_id = {work_item.id: work_item for work_item in items}
    ccn_of = {work_item.id: ccn for ccn, work_item in helpers.work_items_by_ccn(session).items()}
    labels = flow.run.label_by_ccn
    expected = [work_item.id for work_item in sorted(items, key=_queue_order_key)]

    page = client.get("/queue")
    assert page.status_code == 200
    ids = helpers.queue_item_ids(page.text)
    assert ids == expected and len(ids) == EXPECTED_DENIALS
    deadlines = [by_id[item_id].deadline for item_id in ids]
    assert None not in deadlines and deadlines == sorted(deadlines)  # every fixture item has a deadline
    shared_deadlines = 0
    for first, second in zip(ids, ids[1:], strict=False):
        if by_id[first].deadline == by_id[second].deadline:
            shared_deadlines += 1
            assert Decimal(by_id[first].dollar_value) >= Decimal(by_id[second].dollar_value), (first, second)
    assert shared_deadlines > 0, "the dollar tiebreak within a deadline was never exercised"

    # Filters: by category, by flag and by payer, each in queue order.
    for category in sorted({work_item.category for work_item in items}):
        filtered = client.get(f"/queue?category={category}")
        assert filtered.status_code == 200
        assert helpers.queue_item_ids(filtered.text) == [
            item_id for item_id in expected if by_id[item_id].category == category
        ], category
    planted = {item_id for item_id in by_id if labels[ccn_of[item_id]]["injection_planted"]}
    assert len(planted) == EXPECTED_PLANTED
    assert set(helpers.queue_item_ids(client.get("/queue?flagged=injection_flagged").text)) == planted
    flow.planted = planted
    flow.planted_actions = {str(by_id[item_id].current_proposal.action_type) for item_id in planted}
    # An appeal drafted for a claim with a planted letter is the package where planted text would
    # surface in prose; the fixture plants at least one.
    assert str(ActionType.APPEAL) in flow.planted_actions, flow.planted_actions
    coding = {work_item.id for work_item in items if "coding_change" in (work_item.flags or [])}
    assert coding and set(helpers.queue_item_ids(client.get("/queue?flagged=coding_change").text)) == coding
    flagged = {work_item.id for work_item in items if work_item.flags}
    assert set(helpers.queue_item_ids(client.get("/queue?flagged=1").text)) == flagged
    assert set(helpers.queue_item_ids(client.get("/queue?flagged=0").text)) == set(by_id) - flagged
    payers = list(session.execute(select(Payer).order_by(Payer.id)).scalars())
    assert len(payers) == EXPECTED_PAYERS
    for payer in payers:
        filtered = helpers.queue_item_ids(client.get(f"/queue?payer_id={payer.id}").text)
        assert filtered == [item_id for item_id in expected if by_id[item_id].claim.payer_id == payer.id]
        assert len(filtered) == sum(1 for label in labels.values() if label["payer_id"] == payer.payer_id)
    partial = client.get("/queue", headers=HX)
    assert partial.status_code == 200 and 'id="queue-table"' in partial.text and "<html" not in partial.text

    # Every item with a planted letter shows the banner; a benign letter shows none.
    for item_id in sorted(planted):
        page = client.get(f"/items/{item_id}")
        assert page.status_code == 200, item_id
        assert INJECTION_BANNER in page.text and 'class="document flagged"' in page.text, item_id
        flow.viewed.add(item_id)
    benign = sorted(
        item_id
        for item_id in by_id
        if labels[ccn_of[item_id]]["has_letter"] and not labels[ccn_of[item_id]]["injection_planted"]
    )
    assert benign
    page = client.get(f"/items/{benign[0]}")
    assert page.status_code == 200
    assert INJECTION_BANNER not in page.text and 'class="document"' in page.text
    flow.viewed.add(benign[0])
    flow.done.add("queue")


# --------------------------------------------------------------------------------------
# 3. The review sample, over HTTP only
# --------------------------------------------------------------------------------------


def _assign_roles(sample: list[int], by_id: dict[int, WorkItem], planted: set[int]) -> dict[int, str]:
    """Deterministic roles: the first items of each action type take the special cases.

    Items whose payer letter carries planted instructions are left to the approve role, so every
    one of them ends up exported: its package is where a planted instruction would surface. The
    regeneration goes to a plain corrected claim (no note excerpt needed) with a payer letter
    attached, so the pipeline consults the model again (SPEC 7) under the ``regenerate`` purpose.
    """
    by_action: dict[str, list[int]] = {}
    for item_id in sample:
        by_action.setdefault(str(by_id[item_id].current_proposal.action_type), []).append(item_id)

    def clean(action: ActionType) -> list[int]:
        return [item_id for item_id in by_action[str(action)] if item_id not in planted]

    roles = dict.fromkeys(sample, APPROVE)
    roles[clean(ActionType.NO_ACTION)[0]] = REJECT
    roles[clean(ActionType.CLINIC_QUESTION)[0]] = REJECT
    roles[clean(ActionType.APPEAL)[0]] = REJECT_REOPEN_APPROVE
    roles[clean(ActionType.NO_ACTION)[1]] = CLOSE
    roles[clean(ActionType.APPEAL)[1]] = EDIT_APPROVE
    with_letter = [
        item_id
        for item_id in clean(ActionType.CORRECTED_CLAIM)
        if not _needs_note(by_id[item_id]) and by_id[item_id].claim.documents
    ]
    assert with_letter, "no plain corrected claim with a payer letter in the sample"
    roles[with_letter[0]] = REGENERATE_APPROVE
    return roles


def _post(
    flow: Flow, item_id: int, action: str, *, data: dict[str, str] | None = None, hx: bool = False
) -> Any:
    """One action form on the item page: HTMX style (header, partial back) or plain form (token, 303)."""
    if hx:
        response = flow.client.post(f"/items/{item_id}/{action}", data=data or {}, headers=HX)
        _ok(response, item_id, action)
        assert 'id="item-panel"' in response.text and "<html" not in response.text, (item_id, action)
    else:
        response = flow.client.post(f"/items/{item_id}/{action}", data={"_csrf": flow.token, **(data or {})})
        _ok(response, item_id, action)
        assert response.status_code == 303 and response.headers["location"] == f"/items/{item_id}"
    return response


def _approve(flow: Flow, item_id: int, position: int) -> None:
    """Approve; a coding change gets its note excerpt attached first or inline, alternating by position."""
    work_item = _item(flow, item_id)
    if _needs_note(work_item):
        note = _note_text(work_item)
        flow.counts["note_attached"] += 1
        if position % 2 == 0:
            _post(flow, item_id, "attach-note", data={"text": note})
            _post(flow, item_id, "approve", hx=True)
        else:
            _post(flow, item_id, "approve", data={"note_excerpt": note})
    else:
        _post(flow, item_id, "approve", hx=position % 2 == 1)
    flow.counts["work_item_approved"] += 1
    flow.approved.append(item_id)
    fresh = _item(flow, item_id)
    assert fresh.status == "approved" and fresh.decided_at is not None, item_id


def _reject(flow: Flow, item_id: int) -> None:
    _post(flow, item_id, "reject", data={"reason": REJECT_REASON})
    flow.counts["work_item_rejected"] += 1
    fresh = _item(flow, item_id)
    assert fresh.status == "rejected" and fresh.decided_at is not None, item_id


def _reopen(flow: Flow, item_id: int) -> None:
    _post(flow, item_id, "reopen", data={"reason": REOPEN_REASON})
    flow.counts["work_item_reopened"] += 1
    fresh = _item(flow, item_id)
    assert fresh.status == "proposed" and fresh.decided_at is None, item_id


def _close(flow: Flow, item_id: int) -> None:
    _post(flow, item_id, "close", data={"reason": CLOSE_REASON})
    flow.counts["work_item_closed"] += 1
    fresh = _item(flow, item_id)
    assert fresh.status == "closed" and fresh.closed_at is not None, item_id


def _edit(flow: Flow, item_id: int) -> None:
    """Edit the summary and the appeal letter: a version the reviewer wrote, everything else kept."""
    before = _item(flow, item_id).current_proposal
    assert before.appeal_letter, item_id
    summary = before.summary.rstrip() + EDIT_SUFFIX
    letter = before.appeal_letter.rstrip("\n") + "\n\n" + LETTER_SENTENCE + "\n"
    _post(flow, item_id, "edit", data={"reason": EDIT_REASON, "summary": summary, "appeal_letter": letter})
    flow.counts["proposal_edited"] += 1
    fresh = _item(flow, item_id)
    current = fresh.current_proposal
    assert current.version == before.version + 1 and current.id != before.id, item_id
    assert current.summary == summary and current.appeal_letter == letter, item_id
    assert current.generated_by == "reviewer" and current.rationale == before.rationale, item_id
    assert current.citations == before.citations and current.action_type == before.action_type, item_id
    assert [p.version for p in fresh.proposals] == [1, 2] and fresh.status == "in_review", item_id
    assert sum(1 for p in fresh.proposals if p.is_current) == 1
    flow.edited = item_id


def _regenerate(flow: Flow, item_id: int) -> None:
    """Ask for a fresh proposal; the payer letter on the claim makes the pipeline consult the model."""
    work_item = _item(flow, item_id)
    before = work_item.current_proposal
    assert work_item.claim.documents, item_id
    payloads_before, calls_before = len(flow.run.payloads), _count(flow.session, ModelCall)
    _post(flow, item_id, "regenerate")
    flow.counts["proposal_regenerated"] += 1
    fresh = _item(flow, item_id)
    current = fresh.current_proposal
    assert current.version == before.version + 1 and current.is_current and current.citations, item_id
    assert current.action_type == before.action_type and fresh.status == "in_review", item_id
    assert current.generated_by == "playbook" and current.corrected_claim, item_id
    assert sum(1 for p in fresh.proposals if p.is_current) == 1
    # Exactly one guarded model call, recorded against this item under the regeneration purpose.
    assert len(flow.run.payloads) == payloads_before + 1, item_id
    calls = list(flow.session.execute(select(ModelCall).order_by(ModelCall.id)).scalars())
    assert len(calls) == calls_before + 1, item_id
    latest = calls[-1]
    assert (latest.purpose, latest.work_item_id, latest.prompt_name, latest.status) == (
        "regenerate",
        item_id,
        "classify_denial",
        "ok",
    )
    flow.regenerated = item_id


def test_3_review_sample_over_http(flow: Flow) -> None:
    _require(flow, "imports", "queue")
    client, session, reviewer = flow.client, flow.session, flow.run.reviewer
    _fresh(flow)
    items = _work_items(session)
    by_id = {work_item.id: work_item for work_item in items}
    groups: dict[tuple[str, str], list[int]] = {}
    for work_item in items:
        key = (str(work_item.category), str(work_item.current_proposal.action_type))
        groups.setdefault(key, []).append(work_item.id)
    sample_ids = {item_id for ids in groups.values() for item_id in ids[:SAMPLE_PER_GROUP]}
    # Every package shape is also built for a claim whose payer letter carries planted instructions:
    # the first such item of each action type joins the sample.
    for action in sorted(flow.planted_actions):
        sample_ids.add(min(i for i in flow.planted if str(by_id[i].current_proposal.action_type) == action))
    sample = sorted(sample_ids)
    assert len(sample) >= MIN_SAMPLE
    present = {str(work_item.category) for work_item in items}
    assert {str(by_id[i].category) for i in sample} == present == {str(c) for c in ACTIONABLE_CATEGORIES}
    assert {str(by_id[i].current_proposal.action_type) for i in sample} == {str(a) for a in ActionType}
    planted_in_sample = {i for i in sample if i in flow.planted}
    assert {str(by_id[i].current_proposal.action_type) for i in planted_in_sample} == flow.planted_actions
    roles = _assign_roles(sample, by_id, flow.planted)
    assert Counter(roles.values()) == {
        APPROVE: len(sample) - 6,
        REJECT: 2,
        REJECT_REOPEN_APPROVE: 1,
        CLOSE: 1,
        EDIT_APPROVE: 1,
        REGENERATE_APPROVE: 1,
    }
    assert all(roles[i] == APPROVE for i in planted_in_sample)  # each gets an export package
    flow.sample, flow.roles = sample, roles

    # 3a. Read every item first: the first view stamps first_viewed_at and moves it to in_review.
    for item_id in sample:
        assert client.get(f"/items/{item_id}").status_code == 200, item_id
        flow.viewed.add(item_id)
    _fresh(flow)
    for item_id in sample:
        work_item = session.get(WorkItem, item_id)
        assert work_item.first_viewed_at is not None and work_item.status == "in_review", item_id
    time.sleep(REVIEW_PAUSE_SECONDS)

    # 3b. A coding change cannot be approved without a quoted note excerpt.
    coding_ids = [item_id for item_id in sample if _needs_note(by_id[item_id])]
    assert coding_ids, "the sample carries no coding change"
    coding_id = coding_ids[0]
    refused = client.post(f"/items/{coding_id}/approve", data={"_csrf": flow.token})
    assert refused.status_code == 400 and "note excerpt is required" in refused.text
    assert _item(flow, coding_id).status == "in_review"

    # 3c. Decide every sampled item through its page.
    for position, item_id in enumerate(sample):
        role = roles[item_id]
        if role == APPROVE:
            _approve(flow, item_id, position)
        elif role == REJECT:
            _reject(flow, item_id)
            flow.rejected.append(item_id)
        elif role == REJECT_REOPEN_APPROVE:
            _reject(flow, item_id)
            _reopen(flow, item_id)
            _approve(flow, item_id, position)
        elif role == CLOSE:
            _close(flow, item_id)
        elif role == EDIT_APPROVE:
            _edit(flow, item_id)
            _approve(flow, item_id, position)
        else:
            _regenerate(flow, item_id)
            _approve(flow, item_id, position)

    # 3d. The database reflects every decision: status, timestamps, review seconds, action rows.
    _fresh(flow)
    for item_id in sample:
        work_item = session.get(WorkItem, item_id)
        role = roles[item_id]
        assert work_item.status == FINAL_STATUS[role], (item_id, role)
        actions = sorted(work_item.actions, key=lambda a: a.id or 0)
        names = [str(a.action) for a in actions]
        assert names[0] == "view" and names.count("view") == 1, (item_id, names)
        assert all(a.user_id == reviewer.id for a in actions), item_id
        expected_names = set(ROLE_ACTIONS[role])
        if _needs_note(work_item):
            expected_names.add("attach_note")
        assert set(names) == expected_names, (item_id, role, names)
        decisions = [a for a in actions if a.action in ("approve", "reject")]
        if role == CLOSE:
            assert not decisions and work_item.closed_at is not None and work_item.decided_at is None
        else:
            assert decisions and work_item.decided_at is not None, item_id
            assert all(a.review_seconds is not None and a.review_seconds >= 1 for a in decisions), item_id
            # SPEC 10: review_seconds = decided_at - first_viewed_at (whole seconds).
            elapsed = int((work_item.decided_at - work_item.first_viewed_at).total_seconds())
            assert decisions[-1].review_seconds == elapsed, (item_id, decisions[-1].review_seconds, elapsed)
        if role == REJECT:
            assert decisions[-1].reason == REJECT_REASON
        if role == EDIT_APPROVE:
            (edit_action,) = [a for a in actions if a.action == "edit"]
            assert edit_action.reason == EDIT_REASON and edit_action.proposal_id is not None, item_id
            assert edit_action.edited_payload["fields"] == ["summary", "appeal_letter"], item_id
    assert Counter(session.get(WorkItem, i).status for i in sample) == {
        "approved": len(sample) - 3,
        "rejected": 2,
        "closed": 1,
    }
    assert sorted(flow.approved) == sorted(i for i in sample if FINAL_STATUS[roles[i]] == "approved")
    assert flow.counts["work_item_approved"] == len(flow.approved)
    assert flow.counts["note_attached"] >= 1
    assert flow.edited is not None and flow.regenerated is not None

    # The queue reflects the decisions: the default view hides what was rejected or closed.
    rejected, closed = set(flow.rejected), {i for i in sample if roles[i] == CLOSE}
    assert set(helpers.queue_item_ids(client.get("/queue").text)) == set(by_id) - rejected - closed
    assert set(helpers.queue_item_ids(client.get("/queue?status=approved").text)) == set(flow.approved)
    assert set(helpers.queue_item_ids(client.get("/queue?status=rejected").text)) == rejected
    assert set(helpers.queue_item_ids(client.get("/queue?status=closed").text)) == closed
    assert set(helpers.queue_item_ids(client.get("/queue?status=all").text)) == set(by_id)
    assert audit.verify_chain(session) is None
    flow.done.add("review")


# --------------------------------------------------------------------------------------
# 4. Export packages
# --------------------------------------------------------------------------------------


def _expected_members(action_type: str) -> set[str]:
    members = {"summary.json", "audit_excerpt.json"}
    if action_type == str(ActionType.CORRECTED_CLAIM):
        members |= {"corrected_claim.json", "corrected_claim.csv"}
    elif action_type == str(ActionType.APPEAL):
        members |= {"appeal_letter.txt", "appeal_letter.html"}
        members.add("appeal_letter.pdf" if weasyprint_available() else "README.txt")
    elif action_type == str(ActionType.CLINIC_QUESTION):
        members.add("clinic_question.txt")
    return members


def _assert_identifier_rules(
    members: dict[str, bytes], identifiers: list[tuple[str, str]], item_id: int
) -> None:
    """What each member may carry (plan: minimum necessary outside the artifacts a human submits).

    The corrected claim CSV carries the patient block because the clinic resubmits it, and the
    appeal letter names the patient; nothing else may, except quoted note excerpts (which the plan
    says a proposal cites) inside summary.json and the practice-management account number the
    corrected claim JSON needs to identify the claim being replaced.
    """
    summary = json.loads(members["summary.json"])
    for citation in summary.get("citations", []):
        if isinstance(citation, dict) and citation.get("kind") == "note_excerpt":
            citation["text"] = "<quoted note excerpt>"
    found = helpers.scan_text_for_identifiers(json.dumps(summary, ensure_ascii=False), identifiers)
    assert not found, (item_id, "summary.json", found)
    for name in ("audit_excerpt.json", "clinic_question.txt"):
        if name in members:
            found = helpers.scan_text_for_identifiers(members[name].decode("utf-8"), identifiers)
            assert not found, (item_id, name, found)
    if "corrected_claim.json" in members:
        kinds = {
            kind
            for kind, _ in helpers.scan_text_for_identifiers(
                members["corrected_claim.json"].decode("utf-8"), identifiers
            )
        }
        assert kinds <= {helpers.KIND_ACCOUNT}, (item_id, "corrected_claim.json", kinds)


def _assert_no_document_text(
    members: dict[str, bytes], documents: list[Any], phrases: list[str], item_id: int
) -> None:
    """Payer letters are untrusted data: no line of body text and no planted phrase travels in the zip."""
    texts = {
        name: _normalize(data.decode("utf-8", errors="replace"))
        for name, data in members.items()
        if not name.endswith(".pdf")
    }
    for document in documents:
        for raw_line in (document.text or "").splitlines():
            line = _normalize(raw_line)
            if len(line) < DOCUMENT_LINE_MIN_CHARS:
                continue
            for name, text in texts.items():
                assert line not in text, (item_id, name, raw_line[:50])
    for name, text in texts.items():
        assert not helpers.find_planted_phrases(text, phrases), (item_id, name)


def _assert_csv_carries_changes(rows: list[dict[str, str]], corrected: dict[str, Any], item_id: int) -> None:
    """Every change the proposal made is in the CSV the clinic resubmits (line rows or every row)."""
    by_line = {row["line_number"]: row for row in rows}
    for change in corrected["changes"]:
        after = change["after"]
        if isinstance(after, list):
            expected = "|".join(str(value) for value in after)
        else:
            expected = "" if after is None else str(after)
        match = LINE_PATH_RE.match(change["path"])
        field_name = match.group(2) if match else change["path"]
        targets = [by_line[match.group(1)]] if match else rows
        assert field_name in CLAIM_EXPORT_COLUMNS, (item_id, change["path"])
        for row in targets:
            assert row[field_name] == expected, (item_id, change["path"], row[field_name], expected)


def test_4_export_packages(flow: Flow) -> None:
    _require(flow, "review")
    client, session, settings = flow.client, flow.session, flow.run.settings
    identifiers = helpers.identifiers_for_batch(flow.run.batch_dir)
    phrases = helpers.planted_phrases()
    assert client.get(f"/items/{flow.rejected[0]}/export").status_code == 400  # only approved items export
    with_letter = 0
    planted_exported: set[str] = set()

    for item_id in sorted(flow.approved):
        work_item = _item(flow, item_id)
        proposal = work_item.current_proposal
        claim = work_item.claim
        action_type = str(proposal.action_type)
        response = client.get(f"/items/{item_id}/export")
        assert response.status_code == 200, item_id
        assert response.headers["content-type"].startswith("application/zip")
        assert response.headers["content-disposition"].startswith("attachment;")
        archive = zipfile.ZipFile(io.BytesIO(response.content))
        members = {name: archive.read(name) for name in archive.namelist()}
        assert set(members) == _expected_members(action_type), (item_id, action_type, sorted(members))

        summary = json.loads(members["summary.json"])
        assert summary["work_item_id"] == item_id and summary["action_type"] == action_type
        assert summary["claim_control_number"] == claim.claim_control_number
        assert summary["proposal_version"] == proposal.version and summary["proposal_id"] == proposal.id
        assert summary["status_at_export"] == "approved" and summary["submitted_by_software"] is False
        assert summary["reviewer_email"] == flow.run.reviewer.email and summary["citations"]
        assert summary["coding_change"] == bool(proposal.coding_change)
        if _needs_note(work_item):
            assert summary["note_excerpt_attached"] is True, item_id
        excerpt = json.loads(members["audit_excerpt.json"])
        assert excerpt and all(row["claim_id"] == claim.id for row in excerpt)
        assert {row["action"] for row in excerpt} >= {
            "work_item_created",
            "classified",
            "proposal_created",
            "work_item_viewed",
            "work_item_approved",
        }
        if action_type == str(ActionType.CORRECTED_CLAIM):
            csv_text = members["corrected_claim.csv"].decode("utf-8")
            assert csv_text.splitlines()[0] == ",".join(CLAIM_EXPORT_COLUMNS)
            reader = csv.DictReader(io.StringIO(csv_text))
            assert reader.fieldnames == list(CLAIM_EXPORT_COLUMNS)
            rows = list(reader)
            assert len(rows) == len(claim.lines) and all(
                len(row) == len(CLAIM_EXPORT_COLUMNS) for row in rows
            )
            first = rows[0]
            assert first["claim_control_number"] == f"{claim.claim_control_number}-R1"
            assert first["original_claim_control_number"] == claim.claim_control_number
            assert first["claim_frequency_code"] == "7"
            assert first["patient_account_number"] == claim.patient.account_number
            assert first["patient_last_name"] == claim.patient.last_name
            corrected = json.loads(members["corrected_claim.json"])
            assert corrected["after"]["claim_control_number"] == f"{claim.claim_control_number}-R1"
            assert corrected["changes"]
            _assert_csv_carries_changes(rows, corrected, item_id)
        elif action_type == str(ActionType.APPEAL):
            letter = members["appeal_letter.txt"].decode("utf-8")
            assert letter == proposal.appeal_letter
            html = members["appeal_letter.html"].decode("utf-8")
            assert "<pre>" in html and claim.claim_control_number in html
            if item_id == flow.edited:  # the reviewer's letter, not the agent's draft, is what ships
                assert letter.rstrip().endswith(LETTER_SENTENCE) and EDIT_SUFFIX.strip() in summary["summary"]
        elif action_type == str(ActionType.CLINIC_QUESTION):
            assert members["clinic_question.txt"].decode("utf-8") == proposal.clinic_question
        if item_id in (flow.edited, flow.regenerated):
            assert summary["proposal_version"] == 2, item_id
        _assert_identifier_rules(members, identifiers, item_id)
        _assert_no_document_text(members, list(claim.documents), phrases, item_id)
        if claim.documents:
            with_letter += 1
        if item_id in flow.planted:
            planted_exported.add(action_type)

        fresh = _item(flow, item_id)
        assert fresh.status == "exported", item_id
        exports = [a for a in fresh.actions if a.action == "export"]
        assert len(exports) == 1 and sorted(exports[0].edited_payload["members"]) == sorted(members)
        flow.exported[item_id] = sorted(members)

    flow.counts["package_exported"] = len(flow.exported)
    assert set(flow.exported) == set(flow.approved)
    exported_kinds = {str(_item(flow, i).current_proposal.action_type) for i in flow.exported}
    assert exported_kinds == {str(a) for a in ActionType}  # a package of every shape was built
    # The document checks above were not vacuous: packages were built for claims with payer letters,
    # among them one of every action type whose letter carried planted instructions.
    assert with_letter >= 1 and planted_exported == flow.planted_actions, (with_letter, planted_exported)
    written = list(settings.export_path.glob("work_item_*.zip"))
    assert len(written) == len(flow.exported)
    assert audit.verify_chain(session) is None
    flow.done.add("export")


# --------------------------------------------------------------------------------------
# 5. Mark submitted
# --------------------------------------------------------------------------------------


def test_5_mark_submitted(flow: Flow) -> None:
    _require(flow, "export")
    client, session = flow.client, flow.session
    today = flow.run.today
    submittable = (str(ActionType.CORRECTED_CLAIM), str(ActionType.APPEAL))
    for item_id in sorted(flow.exported):
        work_item = _item(flow, item_id)
        action_type = str(work_item.current_proposal.action_type)
        if action_type not in submittable:
            continue
        ccn = work_item.claim.claim_control_number
        hx = len(flow.submitted) % 2 == 1
        _post(flow, item_id, "mark-submitted", data={"submitted_on": today.isoformat()}, hx=hx)
        fresh = _item(flow, item_id)
        expected_ccn = f"{ccn}-R1" if action_type == str(ActionType.CORRECTED_CLAIM) else ccn
        assert fresh.status == "submitted" and fresh.submitted_claim_control_number == expected_ccn, item_id
        assert fresh.submitted_at is not None and fresh.submitted_at.date() == today, item_id
        expected_claim_status = "corrected" if action_type == str(ActionType.CORRECTED_CLAIM) else "appealed"
        assert fresh.claim.status == expected_claim_status, item_id
        marks = [a for a in fresh.actions if a.action == "mark_submitted"]
        assert len(marks) == 1 and marks[0].edited_payload["submitted_claim_control_number"] == expected_ccn
        flow.submitted.append(item_id)
    flow.counts["work_item_submitted"] = len(flow.submitted)

    kinds = Counter(str(_item(flow, i).current_proposal.action_type) for i in flow.submitted)
    assert set(kinds) == set(submittable) and all(n >= 2 for n in kinds.values()), kinds
    # Once is enough: a submitted item cannot be marked twice; questions and no-action items stay exported.
    assert (
        client.post(f"/items/{flow.submitted[0]}/mark-submitted", data={"_csrf": flow.token}).status_code
        == 400
    )
    for item_id in sorted(set(flow.exported) - set(flow.submitted)):
        assert _item(flow, item_id).status == "exported", item_id
    assert set(helpers.queue_item_ids(client.get("/queue?status=submitted").text)) == set(flow.submitted)
    assert audit.verify_chain(session) is None
    flow.done.add("submitted")


# --------------------------------------------------------------------------------------
# 6. Follow-up remittances close the loop
# --------------------------------------------------------------------------------------


def test_6_followup_remittances_record_outcomes(flow: Flow) -> None:
    _require(flow, "submitted")
    session = flow.session
    _fresh(flow)
    submitted = sorted(flow.submitted)
    plan = {item_id: PLAN_VALUES[index % len(PLAN_VALUES)] for index, item_id in enumerate(submitted)}
    assert set(plan.values()) == set(PLAN_VALUES)
    work_items = list(
        session.execute(select(WorkItem).where(WorkItem.id.in_(submitted)).order_by(WorkItem.id)).scalars()
    )
    assert [work_item.id for work_item in work_items] == submitted
    dollar_values = {work_item.id: Decimal(work_item.dollar_value) for work_item in work_items}
    payer_of = {work_item.id: work_item.claim.payer.payer_id for work_item in work_items}
    planned_per_payer = Counter(payer_of.values())
    denied_again_claims = {wi.claim_id for wi in work_items if plan[wi.id] == PLAN_DENIED_AGAIN}
    payment_date = flow.run.today + timedelta(days=FOLLOWUP_LAG_DAYS)
    files = generate_followup_files(session, work_items, plan, payment_date)
    assert set(files) == set(planned_per_payer)
    flow.followup_files = sorted(files)
    items_before = _count(session, WorkItem)
    assert _count(session, Outcome) == 0

    for payer_id in sorted(files):
        name = f"followup_{payer_id}.835"
        job = _upload(flow, "835", name, files[payer_id].encode("utf-8"))
        flow.uploads[name] = job
        _fresh(flow)
        batch = session.get(ImportBatch, job["batch_id"])
        assert batch.status == "done" and job["batch_status"] == "done" and job["pipeline"] == "ok", name
        assert batch.counts["claims"] == batch.counts["matched"] == planned_per_payer[payer_id], name
        assert batch.counts["unmatched"] == 0 and batch.counts["outcomes"] == planned_per_payer[payer_id], (
            name
        )

    # One outcome per submitted item, exactly the planned result, the item closed, the claim updated.
    _fresh(flow)
    outcomes = list(session.execute(select(Outcome).order_by(Outcome.id)).scalars())
    assert sorted(outcome.work_item_id for outcome in outcomes) == submitted
    reasons = {
        PLAN_PAID: "paid_at_least_95pct",
        PLAN_PARTIAL: "paid_below_95pct",
        PLAN_DENIED_AGAIN: "denial",
    }
    for outcome in outcomes:
        item_id = outcome.work_item_id
        planned = plan[item_id]
        work_item = session.get(WorkItem, item_id)
        assert str(outcome.result) == planned, (item_id, outcome.result, planned)
        assert outcome.payment_date == payment_date and outcome.details.get("match") == "claim_id"
        assert outcome.details.get("reason") == reasons.get(planned, "reversal"), item_id
        assert work_item.status == "closed" and work_item.closed_at is not None, item_id
        assert work_item.claim.status == str(CLAIM_STATUS_FOR_RESULT[OutcomeResult(planned)]), item_id
        paid, value = Decimal(outcome.paid_amount), dollar_values[item_id]
        if planned == PLAN_PAID:
            assert paid >= PAID_THRESHOLD * value, item_id
        elif planned == PLAN_PARTIAL:
            assert Decimal("0") < paid < PAID_THRESHOLD * value, item_id
        elif planned == PLAN_DENIED_AGAIN:
            assert paid == 0 and outcome.remittance_claim.is_denial, item_id
        else:
            assert paid < 0 and outcome.remittance_claim.claim_status_code == "22", item_id
        assert outcome.remittance_claim.remittance.file_name == f"followup_{payer_of[item_id]}.835"

    # The queue and the item pages show the closed loop: every submitted fix is closed now, and an
    # item page of each outcome kind lists the result the follow-up remittance brought.
    closed_by_reviewer = {item_id for item_id, role in flow.roles.items() if role == CLOSE}
    closed_page = flow.client.get("/queue?status=closed")
    assert set(helpers.queue_item_ids(closed_page.text)) == set(submitted) | closed_by_reviewer
    for kind in PLAN_VALUES:
        item_id = min(i for i in submitted if plan[i] == kind)
        outcome = next(o for o in outcomes if o.work_item_id == item_id)
        page = flow.client.get(f"/items/{item_id}")
        assert page.status_code == 200 and "<h3>Outcomes</h3>" in page.text, item_id
        shown = f"{label_text(outcome.result)} · paid {money(outcome.paid_amount)}"
        assert shown in page.text and "No actions are available" in page.text, (item_id, kind)

    # A corrected claim denied again is a new denial: it re-enters the queue as a new work item
    # (SPEC 11: every denied, matched remittance claim without a work item is processed).
    new_items = [wi for wi in _work_items(session) if wi.id > items_before]
    assert (
        len(new_items)
        == len(denied_again_claims)
        == sum(flow.uploads[f"followup_{payer_id}.835"]["work_items_created"] for payer_id in files)
    )
    assert {wi.claim_id for wi in new_items} == denied_again_claims
    assert all(wi.status == "proposed" and wi.remittance_claim.is_denial for wi in new_items)
    assert all(wi.remittance_claim.remittance.file_name.startswith("followup_") for wi in new_items)
    assert {wi.id for wi in new_items} <= set(
        helpers.queue_item_ids(flow.client.get("/queue?status=proposed").text)
    )
    flow.re_denials = [wi.id for wi in new_items]
    flow.plan = plan

    # The same follow-up file again is a duplicate batch: nothing is written but its audit trail.
    first = sorted(files)[0]
    before = (_count(session, Outcome), _count(session, WorkItem), _count(session, AuditLog))
    job = _upload(flow, "835", f"followup_{first}.835", files[first].encode("utf-8"))
    flow.uploads[f"followup_{first}.835#again"] = job
    assert job["batch_status"] == "duplicate" and job["work_items_created"] == 0
    _fresh(flow)
    batch = session.get(ImportBatch, job["batch_id"])
    assert batch.status == "duplicate" and batch.counts == {}
    after = (_count(session, Outcome), _count(session, WorkItem), _count(session, AuditLog))
    assert after[:2] == before[:2] and after[2] == before[2] + 3  # requested, completed (duplicate), finished
    assert audit.verify_chain(session) is None
    flow.done.add("followup")


# --------------------------------------------------------------------------------------
# 7. Dashboard and metrics
# --------------------------------------------------------------------------------------


def test_7_dashboard_and_metrics(flow: Flow) -> None:
    _require(flow, "followup")
    client, session = flow.client, flow.session
    _fresh(flow)
    outcomes = list(session.execute(select(Outcome).order_by(Outcome.id)).scalars())
    # Recovered dollars = what the payer paid on the fixes the plan paid or partially paid; the
    # plan, not the recorded result, says which outcomes count, so the metric is checked against
    # the test's own expectation.
    paid_by_plan = [o for o in outcomes if flow.plan[o.work_item_id] in (PLAN_PAID, PLAN_PARTIAL)]
    assert paid_by_plan and all(Decimal(o.paid_amount) > 0 for o in paid_by_plan)
    recovered = sum((Decimal(o.paid_amount) for o in paid_by_plan), Decimal("0"))
    planned = Counter(flow.plan.values())
    submitted = len(flow.submitted)
    overturn = round((planned[PLAN_PAID] + planned[PLAN_PARTIAL]) / submitted, 4)
    assert recovered > 0 and 0 < overturn < 1

    page = client.get("/dashboard")
    assert page.status_code == 200
    assert (
        "Recovered dollars" in page.text and f'<div class="tile-value">{money(recovered)}</div>' in page.text
    )
    assert f'<div class="tile-value">{pct(overturn)}</div>' in page.text
    assert f"({planned[PLAN_PAID] + planned[PLAN_PARTIAL]} of {submitted} submitted fixes)" in page.text
    assert "audit chain intact" in page.text

    data = client.get("/api/metrics").json()
    total = EXPECTED_DENIALS + len(flow.re_denials)
    assert data["total_work_items"] == total == _count(session, WorkItem)
    assert data["submitted_fixes"] == submitted
    assert data["paid_fixes"] == planned[PLAN_PAID] and data["partial_fixes"] == planned[PLAN_PARTIAL]
    assert data["denied_again"] == planned[PLAN_DENIED_AGAIN]
    assert data["overturn_rate"] == overturn
    assert Decimal(data["recovered_dollars"]) == recovered
    review_seconds = [
        int(s)
        for s in session.execute(
            select(ReviewAction.review_seconds).where(ReviewAction.review_seconds.is_not(None))
        ).scalars()
    ]
    assert review_seconds and min(review_seconds) >= 1
    assert data["avg_review_minutes"] > 0
    assert data["avg_review_minutes"] == round(sum(review_seconds) / len(review_seconds) / 60.0, 2)
    assert data["reviewed_items"] == len(flow.approved) + len(flow.rejected)
    items = _work_items(session)
    by_status = Counter(str(wi.status) for wi in items)
    assert {k: v for k, v in data["items_by_status"].items() if v} == dict(by_status)
    assert sum(data["items_by_status"].values()) == total
    assert by_status["closed"] == submitted + flow.counts["work_item_closed"]
    assert by_status["rejected"] == len(flow.rejected)
    assert by_status["exported"] == len(flow.exported) - submitted
    # Dollars at stake: the open items (not closed, not rejected), also what the deadline buckets hold.
    open_items = [wi for wi in items if str(wi.status) not in ("closed", "rejected")]
    at_stake = sum((Decimal(wi.dollar_value) for wi in open_items), Decimal("0"))
    assert len(open_items) == total - by_status["closed"] - by_status["rejected"] > 0
    assert Decimal(data["dollars_at_stake"]) == at_stake > 0
    assert sum(bucket["items"] for bucket in data["deadline_buckets"]) == len(open_items)
    assert sum((Decimal(bucket["dollars"]) for bucket in data["deadline_buckets"]), Decimal("0")) == at_stake

    categories_present = {str(wi.category) for wi in _work_items(session)}
    payer_names = {payer["name"] for payer in helpers.load_manifest(flow.run.batch_dir)["payers"]}
    for key, expected_keys in (("by_category", categories_present), ("by_payer", payer_names)):
        rows = data[key]
        assert {row["key"] for row in rows} == expected_keys, key
        assert sum(row["items"] for row in rows) == total, key
        assert sum(row["submitted"] for row in rows) == submitted, key
        assert sum(row["paid"] for row in rows) == planned[PLAN_PAID], key
        assert sum(row["partial"] for row in rows) == planned[PLAN_PARTIAL], key
        assert sum(row["denied_again"] for row in rows) == planned[PLAN_DENIED_AGAIN], key
        assert sum((Decimal(row["recovered_dollars"]) for row in rows), Decimal("0")) == recovered, key
        for row in rows:
            expected_rate = (row["paid"] + row["partial"]) / row["submitted"] if row["submitted"] else None
            assert row["overturn_rate"] == expected_rate, (key, row["key"])
    assert data["audit_chain_ok"] is True
    assert data["model_calls"] == _count(session, ModelCall) == len(flow.run.payloads)
    flow.done.add("dashboard")


# --------------------------------------------------------------------------------------
# 8. Every step is in the audit log
# --------------------------------------------------------------------------------------


def test_8_every_step_is_in_the_audit_log(flow: Flow) -> None:
    _require(flow, "dashboard")
    client, session, reviewer = flow.client, flow.session, flow.run.reviewer
    _fresh(flow)
    counts = _action_counts(session)
    uploads = len(flow.uploads)
    followups = [name for name in flow.uploads if name.startswith("followup_")]
    assert len(followups) == len(flow.followup_files) + 1  # one per payer, plus the duplicate upload
    assert uploads == 3 + EXPECTED_REMIT_FILES + len(followups)
    assert counts["import_requested"] == counts["import_job_finished"] == uploads
    assert counts["import_completed"] == _count(session, ImportBatch) == uploads
    assert counts["import_failed"] == 0
    pipeline_runs = EXPECTED_DENIALS + len(flow.re_denials)
    assert counts["work_item_created"] == counts["classified"] == counts["proposal_created"] == pipeline_runs
    assert pipeline_runs == _count(session, WorkItem)
    assert counts["work_item_viewed"] == len(flow.viewed) >= len(flow.sample)
    for action in (
        "work_item_approved",
        "work_item_rejected",
        "work_item_reopened",
        "work_item_closed",
        "proposal_edited",
        "proposal_regenerated",
        "note_attached",
        "package_exported",
        "work_item_submitted",
    ):
        assert counts[action] == flow.counts[action] > 0, (action, counts[action], flow.counts[action])
    assert counts["proposal_regenerate_requested"] == flow.counts["proposal_regenerated"]
    assert counts["outcome_recorded"] == _count(session, Outcome) == len(flow.submitted)
    assert counts["login_success"] == 1 and counts["user_created"] == 1
    assert counts["model_call"] == _count(session, ModelCall) == len(flow.run.payloads) > 0
    assert counts["model_call_blocked"] == 0 and counts["model_call_failed"] == 0

    rows = list(session.execute(select(AuditLog).order_by(AuditLog.id)).scalars())
    assert len(rows) == sum(counts.values())
    assert all(row.actor_type in {"user", "agent", "system"} and row.actor_id for row in rows)
    assert all(row.hash and len(row.hash) == 64 for row in rows)
    assert {row.actor_id for row in rows if row.action in REVIEW_EVENTS} == {f"user:{reviewer.id}"}
    assert {(row.actor_type, row.actor_id) for row in rows if row.action in FIRST_RUN_EVENTS} == {
        ("agent", "pipeline")
    }
    # A regeneration the reviewer asked for is the agent's work on the reviewer's behalf.
    assert {(row.actor_type, row.actor_id) for row in rows if row.action == "proposal_regenerated"} == {
        ("agent", f"user:{reviewer.id}")
    }
    assert {(row.actor_type, row.actor_id) for row in rows if row.action == "outcome_recorded"} == {
        ("system", "outcomes")
    }
    assert {(row.actor_type, row.actor_id) for row in rows if row.action == "model_call"} == {
        MODEL_CALL_ACTOR
    }
    assert {row.actor_type for row in rows if row.action.startswith("import_")} == {"user", "system"}
    assert all(row.claim_id is not None for row in rows if row.action in PIPELINE_EVENTS + REVIEW_EVENTS)

    # Append-only: the chain verifies, and neither an update nor a delete gets through the ORM.
    assert audit.verify_chain(session) is None
    first_id, first_action = rows[0].id, rows[0].action
    rows[0].action = "tampered"
    with pytest.raises(AuditImmutableError):
        session.flush()
    session.rollback()
    session.expire_all()
    session.delete(session.get(AuditLog, first_id))
    with pytest.raises(AuditImmutableError):
        session.flush()
    session.rollback()
    session.expire_all()
    assert session.get(AuditLog, first_id).action == first_action
    assert _count(session, AuditLog) == len(rows)
    assert audit.verify_chain(session) is None

    # The audit page: chain intact, newest events first, filters by action.
    page = client.get("/audit")
    assert page.status_code == 200
    assert "Hash chain intact" in page.text and f"{len(rows)} event(s)" in page.text
    newest = [row.id for row in rows[::-1][:100]]
    assert [int(x) for x in AUDIT_ROW_RE.findall(page.text)] == newest
    assert "<code>outcome_recorded</code>" in page.text and "<code>import_completed</code>" in page.text
    filtered = client.get("/audit?action=work_item_submitted")
    assert filtered.status_code == 200
    assert len(AUDIT_ROW_RE.findall(filtered.text)) == min(counts["work_item_submitted"], 100)
    assert "<code>outcome_recorded</code>" not in filtered.text
    flow.done.add("audit")


# --------------------------------------------------------------------------------------
# 9. The PHI guard across the whole loop
# --------------------------------------------------------------------------------------


def test_9_no_identifier_reaches_the_model_or_the_logs(flow: Flow) -> None:
    _require(flow, "audit")
    session = flow.session
    _fresh(flow)
    payloads = list(flow.run.payloads)
    assert len(payloads) >= MIN_PAYLOADS
    assert {prompt.name for prompt in payloads} == {"classify_denial", "draft_appeal"}
    assert _count(session, ModelCall) == len(payloads)
    calls = list(session.execute(select(ModelCall).order_by(ModelCall.id)).scalars())
    assert {call.purpose for call in calls} == {"denial_pipeline", "regenerate"}
    assert [call.work_item_id for call in calls if call.purpose == "regenerate"] == [flow.regenerated]
    assert all(call.status == "ok" and call.error is None and call.work_item_id is not None for call in calls)
    assert {call.work_item_id for call in calls} <= {wi.id for wi in _work_items(session)}

    identifiers = helpers.identifiers_for_batch(flow.run.batch_dir)
    letterheads = helpers.payer_letterheads(helpers.load_manifest(flow.run.batch_dir)["profile"])
    patient_identifiers = [(kind, value) for kind, value in identifiers if value.lower() not in letterheads]
    assert len(identifiers) - len(patient_identifiers) <= len(letterheads)
    findings = [
        (index, prompt.name, part, kind, preview)
        for index, prompt in enumerate(payloads)
        for part, text in (("system", prompt.system), ("user", prompt.user))
        for kind, preview in helpers.scan_text_for_identifiers(text, patient_identifiers)
    ]
    assert not findings, f"{len(findings)} identifier(s) in model payloads, e.g. {findings[:10]}"
    # Payer letters reached the model as wrapped data in the user turn (SPEC 8).
    assert any(p.name == "classify_denial" and "<untrusted_document" in p.user for p in payloads)

    records = flow.run.logs.records
    assert len(records) >= MIN_LOG_RECORDS, len(records)
    assert {record.name.split(".")[0] for record in records} >= {"app"}
    messages = flow.run.logs.messages
    joined = "\n".join(messages)
    log_findings = helpers.scan_text_for_identifiers(joined, identifiers)
    if log_findings:
        where = [
            (record.name, kind, preview)
            for record, message in zip(records, messages, strict=True)
            for kind, preview in helpers.scan_text_for_identifiers(message, identifiers)
        ]
        raise AssertionError(
            f"{len(log_findings)} identifier(s) in log output: {where[:10] or log_findings[:10]}"
        )
    member_ids = [value for kind, value in identifiers if kind == helpers.KIND_MEMBER_ID]
    assert member_ids and not any(value in joined for value in member_ids)
    assert not MEMBER_ID_SHAPE.search(joined), MEMBER_ID_SHAPE.findall(joined)[:5]  # masked by shape too
    # The log carried every phase: imports, pipeline, review decisions, exports, outcomes.
    for marker in ("import batch", "pipeline processed", "approved", "rejected", "exported", "outcome"):
        assert any(marker in message for message in messages), marker
    flow.done.add("phi")
