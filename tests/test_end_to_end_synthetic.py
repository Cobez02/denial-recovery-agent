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
   (a reversal leaves the fix submitted for the re-adjudication; a partial payment and a claim
   denied again are new denials that re-enter the queue)
6b. the payer takes two of those payments back in later 835s (the closed fixes reopen, their claims
   go back to corrected or appealed) and then pays one of them again (closed again)
7. the dashboard page and the JSON metrics, take-backs included
8. the audit log: every step recorded, rows append-only, chain intact, visible on /audit
9. the PHI guard: zero batch identifiers in any model payload or log record of the whole run

Every name, member id, address and date of birth in the batch is synthetic. Nothing here submits a
claim: approvals produce zip packages and the reviewer marks them submitted by hand.
"""

from __future__ import annotations

import csv
import html
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
    PLAN_REVERSED,
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
NEXT_PAGE_RE = re.compile(r'rel="next" href="([^"]+)"')  # the queue pager (100 rows per page)
SHOWING_RE = re.compile(r"Showing (\d+)-(\d+) of (\d+) item\(s\)")
MAX_QUEUE_PAGES = 20
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
    re_denials: list[int] = field(default_factory=list)  # work items the follow-up denials created
    reversed_items: list[int] = field(default_factory=list)  # fixes reversed, still submitted
    taken_back: list[int] = field(default_factory=list)  # closed fixes whose payment a later 835 took back
    takeback_files: list[str] = field(default_factory=list)
    repay_files: list[str] = field(default_factory=list)
    repaid: int | None = None  # the taken-back fix a later 835 paid again (closed again)
    taken_back_pending: int | None = None  # the taken-back fix still awaiting its re-adjudication
    taken_back_dollars: Decimal = Decimal("0")  # what the payer took back from that fix

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


def _queue_ids(flow: Flow, path: str = "/queue") -> list[int]:
    """Work item ids in queue order across every page of a (filtered) queue listing.

    The queue shows 100 rows per page with exact totals and a `next` link; the loop follows the
    links so an assertion about "every item" never silently stops at the first page.
    """
    ids: list[int] = []
    url: str | None = path
    for _ in range(MAX_QUEUE_PAGES):
        if url is None:
            break
        page = flow.client.get(url)
        assert page.status_code == 200, (url, page.status_code)
        ids.extend(helpers.queue_item_ids(page.text))
        match = NEXT_PAGE_RE.search(page.text)
        url = html.unescape(match.group(1)) if match else None
    assert url is None, "queue pager did not end"
    shown = SHOWING_RE.search(page.text)
    if shown:
        assert int(shown.group(3)) == len(ids), (path, shown.group(0), len(ids))
    return ids


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
    assert len(_queue_ids(flow)) == EXPECTED_DENIALS
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

    ids = _queue_ids(flow)
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
        assert _queue_ids(flow, f"/queue?category={category}") == [
            item_id for item_id in expected if by_id[item_id].category == category
        ], category
    planted = {item_id for item_id in by_id if labels[ccn_of[item_id]]["injection_planted"]}
    assert len(planted) == EXPECTED_PLANTED
    assert set(_queue_ids(flow, "/queue?flagged=injection_flagged")) == planted
    flow.planted = planted
    flow.planted_actions = {str(by_id[item_id].current_proposal.action_type) for item_id in planted}
    # An appeal drafted for a claim with a planted letter is the package where planted text would
    # surface in prose; the fixture plants at least one.
    assert str(ActionType.APPEAL) in flow.planted_actions, flow.planted_actions
    coding = {work_item.id for work_item in items if "coding_change" in (work_item.flags or [])}
    assert coding and set(_queue_ids(flow, "/queue?flagged=coding_change")) == coding
    flagged = {work_item.id for work_item in items if work_item.flags}
    assert set(_queue_ids(flow, "/queue?flagged=1")) == flagged
    assert set(_queue_ids(flow, "/queue?flagged=0")) == set(by_id) - flagged
    payers = list(session.execute(select(Payer).order_by(Payer.id)).scalars())
    assert len(payers) == EXPECTED_PAYERS
    for payer in payers:
        filtered = _queue_ids(flow, f"/queue?payer_id={payer.id}")
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
    """Reopen, then read the item again as a reviewer taking a second look would: the second review
    is timed from that view (record_view stores one view action per reviewer, whatever it stamps)."""
    _post(flow, item_id, "reopen", data={"reason": REOPEN_REASON})
    flow.counts["work_item_reopened"] += 1
    fresh = _item(flow, item_id)
    assert fresh.status == "proposed" and fresh.decided_at is None, item_id
    assert flow.client.get(f"/items/{item_id}").status_code == 200, item_id
    assert _item(flow, item_id).first_viewed_at is not None, item_id
    time.sleep(REVIEW_PAUSE_SECONDS)


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
    assert set(_queue_ids(flow)) == set(by_id) - rejected - closed
    assert set(_queue_ids(flow, "/queue?status=approved")) == set(flow.approved)
    assert set(_queue_ids(flow, "/queue?status=rejected")) == rejected
    assert set(_queue_ids(flow, "/queue?status=closed")) == closed
    assert set(_queue_ids(flow, "/queue?status=all")) == set(by_id)
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
    for excerpt in summary.get("note_excerpts", []):  # the excerpts the reviewer attached, verbatim
        if isinstance(excerpt, dict):
            excerpt["text"] = "<quoted note excerpt>"
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
    assert set(_queue_ids(flow, "/queue?status=submitted")) == set(flow.submitted)
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
    # A claim denied again and a claim paid in part (a denial remains on the unpaid part) are both
    # denials the pipeline turns into new work items; a reversal and a full payment are not.
    redenied_claims = {wi.claim_id for wi in work_items if plan[wi.id] in (PLAN_DENIED_AGAIN, PLAN_PARTIAL)}
    reversed_items = sorted(wi.id for wi in work_items if plan[wi.id] == PLAN_REVERSED)
    assert reversed_items and redenied_claims
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
        assert batch.counts["denials"] == sum(
            1 for wi in work_items if payer_of[wi.id] == payer_id and wi.claim_id in redenied_claims
        ), name

    # One outcome per submitted item, exactly the planned result; a payment, a partial payment and
    # a second denial close the item and update the claim, a reversal leaves the fix submitted.
    _fresh(flow)
    outcomes = list(session.execute(select(Outcome).order_by(Outcome.id)).scalars())
    assert sorted(outcome.work_item_id for outcome in outcomes) == submitted
    reasons = {
        PLAN_PAID: "no_denial_remaining",
        PLAN_PARTIAL: "denial_remains",
        PLAN_DENIED_AGAIN: "denial",
        PLAN_REVERSED: "reversal",
    }
    for outcome in outcomes:
        item_id = outcome.work_item_id
        planned = plan[item_id]
        work_item = session.get(WorkItem, item_id)
        rc = outcome.remittance_claim
        assert str(outcome.result) == planned, (item_id, outcome.result, planned)
        assert outcome.payment_date == payment_date and outcome.details.get("match") == "claim_id"
        assert outcome.details.get("reason") == reasons[planned], (item_id, outcome.details)
        if planned == PLAN_REVERSED:
            # The fix is still in flight: the claim keeps the status mark-submitted gave it.
            assert work_item.status == "submitted" and work_item.closed_at is None, item_id
            corrected = str(work_item.current_proposal.action_type) == str(ActionType.CORRECTED_CLAIM)
            assert work_item.claim.status == ("corrected" if corrected else "appealed"), item_id
        else:
            assert work_item.status == "closed" and work_item.closed_at is not None, item_id
            assert work_item.claim.status == str(CLAIM_STATUS_FOR_RESULT[OutcomeResult(planned)]), item_id
        paid, value = Decimal(outcome.paid_amount), dollar_values[item_id]
        if planned == PLAN_PAID:
            # Paid at the contracted rate: CO-45 only, no denial adjustment, whatever the 95% bar says.
            assert paid > 0 and not rc.is_denial and outcome.details["remaining_denials"] == [], item_id
            assert {(a.group_code, a.carc) for a in rc.adjustments} <= {("CO", "45")}, item_id
        elif planned == PLAN_PARTIAL:
            assert Decimal("0") < paid < PAID_THRESHOLD * value and rc.is_denial, item_id
            assert outcome.details["remaining_denials"], item_id
        elif planned == PLAN_DENIED_AGAIN:
            assert paid == 0 and rc.is_denial, item_id
        else:
            assert paid < 0 and rc.claim_status_code == "22", item_id
        assert rc.remittance.file_name == f"followup_{payer_of[item_id]}.835"
    full_charge_wins = [
        o
        for o in outcomes
        if plan[o.work_item_id] == PLAN_PAID
        and Decimal(o.paid_amount) < PAID_THRESHOLD * dollar_values[o.work_item_id]
    ]
    assert full_charge_wins, "no overturned full-charge denial was paid below 95% of its dollar value"

    # The queue and the item pages show the closed loop: every submitted fix but the reversed ones
    # is closed now, the reversed ones wait in the submitted list, and an item page of each outcome
    # kind lists the result the follow-up remittance brought.
    closed_by_reviewer = {item_id for item_id, role in flow.roles.items() if role == CLOSE}
    assert set(_queue_ids(flow, "/queue?status=closed")) == (set(submitted) - set(reversed_items)) | (
        closed_by_reviewer
    )
    assert set(_queue_ids(flow, "/queue?status=submitted")) == set(reversed_items)
    for kind in PLAN_VALUES:
        item_id = min(i for i in submitted if plan[i] == kind)
        outcome = next(o for o in outcomes if o.work_item_id == item_id)
        page = flow.client.get(f"/items/{item_id}")
        assert page.status_code == 200 and "<h3>Outcomes</h3>" in page.text, item_id
        shown = f"{label_text(outcome.result)} · paid {money(outcome.paid_amount)}"
        assert shown in page.text, (item_id, kind)
        if kind == PLAN_REVERSED:  # still submitted: the package can be re-downloaded, the item closed
            assert "No actions are available" not in page.text and "re-download" in page.text, item_id
        else:
            assert "No actions are available" in page.text, (item_id, kind)

    # A corrected claim denied again, or paid in part, is a new denial: it re-enters the queue as a
    # new work item (SPEC 11: every denied, matched remittance claim without a work item is processed).
    new_items = [wi for wi in _work_items(session) if wi.id > items_before]
    assert (
        len(new_items)
        == len(redenied_claims)
        == sum(flow.uploads[f"followup_{payer_id}.835"]["work_items_created"] for payer_id in files)
    )
    assert {wi.claim_id for wi in new_items} == redenied_claims
    assert all(wi.status == "proposed" and wi.remittance_claim.is_denial for wi in new_items)
    assert all(wi.remittance_claim.remittance.file_name.startswith("followup_") for wi in new_items)
    assert {wi.id for wi in new_items} <= set(_queue_ids(flow, "/queue?status=proposed"))
    flow.re_denials = [wi.id for wi in new_items]
    flow.reversed_items = reversed_items
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
# 6b. Payments taken back after the close; one of them paid again
# --------------------------------------------------------------------------------------

TAKEBACK_LAG_DAYS = 45  # the take-back 835 is dated after the first follow-up (FOLLOWUP_LAG_DAYS)
REPAY_LAG_DAYS = 60


def _outcome_rows(session: Any, item_id: int) -> list[Outcome]:
    stmt = select(Outcome).where(Outcome.work_item_id == item_id).order_by(Outcome.id)
    return list(session.execute(stmt).scalars())


def _events(session: Any, action: str, item_id: int) -> list[AuditLog]:
    stmt = (
        select(AuditLog)
        .where(AuditLog.action == action, AuditLog.entity_type == "work_item", AuditLog.entity_id == item_id)
        .order_by(AuditLog.id)
    )
    return list(session.execute(stmt).scalars())


def _pick_take_backs(flow: Flow) -> list[int]:
    """Two fixes the first follow-up paid, from two payers when the plan allows."""
    paid = [item_id for item_id in sorted(flow.submitted) if flow.plan[item_id] == PLAN_PAID]
    assert len(paid) >= 2, paid
    payer_of = {item_id: _item(flow, item_id).claim.payer_id for item_id in paid}
    first = paid[0]
    second = next((item_id for item_id in paid[1:] if payer_of[item_id] != payer_of[first]), paid[1])
    return [first, second]


def _upload_followup(flow: Flow, name: str, text: str, *, expected_claims: int) -> ImportBatch:
    """Upload one later 835 that creates no work item (reversals and payments are not denials)."""
    job = _upload(flow, "835", name, text.encode("utf-8"))
    flow.uploads[name] = job
    _fresh(flow)
    batch = flow.session.get(ImportBatch, job["batch_id"])
    assert batch.status == "done" and job["batch_status"] == "done" and job["pipeline"] == "ok", name
    assert job["work_items_created"] == 0 and batch.counts["denials"] == 0, name
    assert batch.counts["claims"] == batch.counts["matched"] == batch.counts["outcomes"] == expected_claims, (
        name
    )
    assert batch.counts["unmatched"] == 0, name
    return batch


def test_6b_payments_taken_back_and_repaid(flow: Flow) -> None:
    _require(flow, "followup")
    client, session = flow.client, flow.session
    chosen = _pick_take_backs(flow)
    _fresh(flow)
    work_items = [session.get(WorkItem, item_id) for item_id in chosen]
    payer_of = {wi.id: wi.claim.payer.payer_id for wi in work_items}
    before = {
        wi.id: (wi.submitted_at, wi.submitted_claim_control_number, str(wi.current_proposal.action_type))
        for wi in work_items
    }
    first_payment = {wi.id: Decimal(_outcome_rows(session, wi.id)[0].paid_amount) for wi in work_items}
    assert all(wi.status == "closed" and wi.claim.status == "paid" for wi in work_items)
    assert all(amount > 0 for amount in first_payment.values())
    outcomes_before, items_before = _count(session, Outcome), _count(session, WorkItem)
    takeback_date = flow.run.today + timedelta(days=TAKEBACK_LAG_DAYS)
    assert takeback_date > _outcome_rows(session, chosen[0])[0].payment_date
    files = generate_followup_files(session, work_items, dict.fromkeys(chosen, PLAN_REVERSED), takeback_date)
    assert set(files) == set(payer_of.values())
    flow.takeback_files = sorted(files)
    for payer_id in sorted(files):
        _upload_followup(
            flow,
            f"followup_takeback_{payer_id}.835",
            files[payer_id],
            expected_claims=sum(1 for item_id in chosen if payer_of[item_id] == payer_id),
        )
    # A reversal is not a denial: nothing re-enters the queue; one reversed outcome per fix.
    assert _count(session, WorkItem) == items_before
    assert _count(session, Outcome) == outcomes_before + len(chosen)

    for item_id in chosen:
        work_item = session.get(WorkItem, item_id)
        rows = _outcome_rows(session, item_id)
        assert [str(o.result) for o in rows] == [PLAN_PAID, PLAN_REVERSED], item_id
        reversal = rows[-1]
        assert reversal.payment_date == takeback_date, item_id
        assert Decimal(reversal.paid_amount) == -first_payment[item_id], item_id
        assert reversal.details["match"] == "claim_id" and reversal.details["reason"] == "reversal", item_id
        assert reversal.remittance_claim.claim_status_code == "22", item_id
        assert reversal.remittance_claim.remittance.file_name == f"followup_takeback_{payer_of[item_id]}.835"
        # Back to submitted: closed_at cleared, the submission facts untouched, the claim as the
        # submission left it.
        assert work_item.status == "submitted" and work_item.closed_at is None, item_id
        submitted_at, ccn, action_type = before[item_id]
        assert work_item.submitted_at == submitted_at, item_id
        assert work_item.submitted_claim_control_number == ccn, item_id
        claim_status = "corrected" if action_type == str(ActionType.CORRECTED_CLAIM) else "appealed"
        assert work_item.claim.status == claim_status, item_id
        # The audit trail: the outcome event says where the item came from, the reopen event why.
        recorded = _events(session, "outcome_recorded", item_id)
        assert len(recorded) == 2 and (recorded[-1].actor_type, recorded[-1].actor_id) == (
            "system",
            "outcomes",
        )
        assert recorded[-1].details["result"] == "reversed", item_id
        assert recorded[-1].details["work_item_status"] == "submitted", item_id
        assert recorded[-1].details["previous_work_item_status"] == "closed", item_id
        [reopened] = _events(session, "work_item_reopened", item_id)
        assert (reopened.actor_type, reopened.actor_id, reopened.claim_id) == (
            "system",
            "outcomes",
            work_item.claim_id,
        )
        assert reopened.id > recorded[-1].id
        assert reopened.details == {
            "previous_status": "closed",
            "reason": "payer_reversal",
            "remittance_claim_id": reversal.remittance_claim_id,
            "remittance_id": reversal.remittance_claim.remittance_id,
            "outcome_id": reversal.id,
            "claim_status": claim_status,
        }
        # The item page lists both outcomes and offers the actions a submitted item has again.
        page = client.get(f"/items/{item_id}")
        assert page.status_code == 200 and "<h3>Outcomes</h3>" in page.text, item_id
        for outcome in rows:
            assert f"{label_text(outcome.result)} · paid {money(outcome.paid_amount)}" in page.text, item_id
        assert "No actions are available" not in page.text and "re-download" in page.text, item_id
    # The queue lists them as submitted again, and no longer as closed.
    assert set(_queue_ids(flow, "/queue?status=submitted")) == set(flow.reversed_items) | set(chosen)
    assert not set(chosen) & set(_queue_ids(flow, "/queue?status=closed"))

    # One of them is paid again by a later 835: closed again, with the new payment as its latest
    # outcome; the other waits in the submitted list with the reversal as its latest outcome.
    repaid, pending = chosen
    repay_date = flow.run.today + timedelta(days=REPAY_LAG_DAYS)
    files = generate_followup_files(session, [session.get(WorkItem, repaid)], {repaid: PLAN_PAID}, repay_date)
    assert list(files) == [payer_of[repaid]]
    flow.repay_files = sorted(files)
    _upload_followup(
        flow, f"followup_repay_{payer_of[repaid]}.835", files[payer_of[repaid]], expected_claims=1
    )
    work_item = session.get(WorkItem, repaid)
    rows = _outcome_rows(session, repaid)
    assert [str(o.result) for o in rows] == [PLAN_PAID, PLAN_REVERSED, PLAN_PAID]
    assert rows[-1].payment_date == repay_date and Decimal(rows[-1].paid_amount) == first_payment[repaid]
    assert work_item.status == "closed" and work_item.closed_at is not None
    assert work_item.claim.status == "paid"
    assert len(_events(session, "work_item_reopened", repaid)) == 1  # the reopen is not repeated
    assert len(_events(session, "outcome_recorded", repaid)) == 3
    page = client.get(f"/items/{repaid}")
    assert page.status_code == 200 and "No actions are available" in page.text
    other = session.get(WorkItem, pending)
    assert other.status == "submitted" and other.closed_at is None
    assert [str(o.result) for o in _outcome_rows(session, pending)] == [PLAN_PAID, PLAN_REVERSED]
    assert set(_queue_ids(flow, "/queue?status=submitted")) == set(flow.reversed_items) | {pending}
    assert repaid in set(_queue_ids(flow, "/queue?status=closed"))
    assert _count(session, Outcome) == outcomes_before + len(chosen) + 1
    assert _count(session, WorkItem) == items_before
    assert audit.verify_chain(session) is None

    flow.taken_back = list(chosen)
    flow.repaid, flow.taken_back_pending = repaid, pending
    flow.taken_back_dollars = first_payment[pending]
    flow.done.add("takeback")


# --------------------------------------------------------------------------------------
# 7. Dashboard and metrics
# --------------------------------------------------------------------------------------


def test_7_dashboard_and_metrics(flow: Flow) -> None:
    _require(flow, "followup", "takeback")
    client, session = flow.client, flow.session
    _fresh(flow)
    outcomes = list(session.execute(select(Outcome).order_by(Outcome.id)).scalars())
    # Recovered dollars = what the payer paid on the fixes the plan paid or partially paid, each by
    # its latest outcome: the first follow-up's payment, except for the fix whose payment was taken
    # back and not paid again (nothing) and the fix paid again (its new payment). The plan and
    # phase 6b, not the recorded results, say which outcomes count, so the metric is checked
    # against the test's own expectation.
    first_outcome: dict[int, Outcome] = {}
    for outcome in outcomes:
        first_outcome.setdefault(outcome.work_item_id, outcome)
    paid_by_plan = [
        first_outcome[item_id]
        for item_id in sorted(flow.submitted)
        if flow.plan[item_id] in (PLAN_PAID, PLAN_PARTIAL) and item_id not in flow.taken_back
    ]
    assert paid_by_plan and all(Decimal(o.paid_amount) > 0 for o in paid_by_plan)
    repaid_outcome = [o for o in outcomes if o.work_item_id == flow.repaid][-1]
    assert str(repaid_outcome.result) == PLAN_PAID and Decimal(repaid_outcome.paid_amount) > 0
    recovered = sum((Decimal(o.paid_amount) for o in paid_by_plan), Decimal(repaid_outcome.paid_amount))
    planned = Counter(flow.plan.values())
    submitted = len(flow.submitted)
    paid_fixes = planned[PLAN_PAID] - 1  # the fix still taken back counts neither paid nor partial
    reversed_fixes = planned[PLAN_REVERSED] + 1  # the plan's reversals plus the fix still taken back
    taken_back = flow.taken_back_dollars
    overturn = round((paid_fixes + planned[PLAN_PARTIAL]) / submitted, 4)
    assert recovered > 0 and 0 < overturn < 1 and taken_back > 0

    page = client.get("/dashboard")
    assert page.status_code == 200
    assert (
        "Recovered dollars" in page.text and f'<div class="tile-value">{money(recovered)}</div>' in page.text
    )
    assert f'<div class="tile-value">{money(taken_back)}</div>' in page.text
    fixes_word = "fix" if reversed_fixes == 1 else "fixes"
    assert f"Taken back ({reversed_fixes} {fixes_word} awaiting re-adjudication)" in page.text
    assert f'<div class="tile-value">{pct(overturn)}</div>' in page.text
    assert f"({planned[PLAN_PARTIAL] + paid_fixes} of {submitted} submitted fixes)" in page.text
    # The dashboard never re-hashes the whole audit log; it points at the audit page (phase 8
    # verifies the chain there) and never claims a chain it did not check.
    assert 'href="/audit"' in page.text and "BROKEN" not in page.text
    assert "audit chain intact" in page.text or "verify on the audit page" in page.text

    data = client.get("/api/metrics").json()
    total = EXPECTED_DENIALS + len(flow.re_denials)
    assert data["total_work_items"] == total == _count(session, WorkItem)
    assert data["submitted_fixes"] == submitted
    assert data["paid_fixes"] == paid_fixes and data["partial_fixes"] == planned[PLAN_PARTIAL]
    assert data["denied_again"] == planned[PLAN_DENIED_AGAIN]
    assert data["reversed_fixes"] == reversed_fixes
    assert Decimal(data["taken_back_dollars"]) == taken_back
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
    # Reversed fixes and the fix still taken back are submitted (pending the re-adjudication);
    # everything else that was submitted is closed by its outcome, the re-paid fix among them.
    assert by_status["submitted"] == len(flow.reversed_items) + 1 == reversed_fixes > 0
    assert by_status["closed"] == submitted - by_status["submitted"] + flow.counts["work_item_closed"]
    assert by_status["rejected"] == len(flow.rejected)
    assert by_status["exported"] == len(flow.exported) - submitted
    # Dollars at stake: the open items (not closed, not rejected), also what the deadline buckets
    # hold; the taken-back fix is open again, the re-paid one is not.
    open_items = [wi for wi in items if str(wi.status) not in ("closed", "rejected")]
    open_ids = {wi.id for wi in open_items}
    assert flow.taken_back_pending in open_ids and flow.repaid not in open_ids
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
        assert sum(row["paid"] for row in rows) == paid_fixes, key
        assert sum(row["partial"] for row in rows) == planned[PLAN_PARTIAL], key
        assert sum(row["denied_again"] for row in rows) == planned[PLAN_DENIED_AGAIN], key
        assert sum((Decimal(row["recovered_dollars"]) for row in rows), Decimal("0")) == recovered, key
        for row in rows:
            expected_rate = (row["paid"] + row["partial"]) / row["submitted"] if row["submitted"] else None
            assert row["overturn_rate"] == expected_rate, (key, row["key"])
    assert data["audit_chain_ok"] in (True, None)  # None: the JSON leaves verification to /audit too
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
    # One per payer, plus the duplicate upload, plus the take-back files and the re-payment.
    assert len(followups) == len(flow.followup_files) + 1 + len(flow.takeback_files) + len(flow.repay_files)
    assert len(flow.takeback_files) >= 1 and len(flow.repay_files) == 1
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
        "work_item_closed",
        "proposal_edited",
        "proposal_regenerated",
        "note_attached",
        "package_exported",
        "work_item_submitted",
    ):
        assert counts[action] == flow.counts[action] > 0, (action, counts[action], flow.counts[action])
    # Reopens: the reviewer's second look and the system's take-backs, told apart by actor type.
    reopened_by = Counter(
        session.execute(select(AuditLog.actor_type).where(AuditLog.action == "work_item_reopened")).scalars()
    )
    assert reopened_by == {"user": flow.counts["work_item_reopened"], "system": len(flow.taken_back)}
    assert counts["work_item_reopened"] == flow.counts["work_item_reopened"] + len(flow.taken_back) > 1
    assert counts["proposal_regenerate_requested"] == flow.counts["proposal_regenerated"]
    # One outcome per submitted fix, one per take-back, one for the re-payment.
    assert (
        counts["outcome_recorded"]
        == _count(session, Outcome)
        == len(flow.submitted) + len(flow.taken_back) + 1
    )
    assert counts["login_success"] == 1 and counts["user_created"] == 1
    assert counts["model_call"] == _count(session, ModelCall) == len(flow.run.payloads) > 0
    assert counts["model_call_blocked"] == 0 and counts["model_call_failed"] == 0

    rows = list(session.execute(select(AuditLog).order_by(AuditLog.id)).scalars())
    assert len(rows) == sum(counts.values())
    assert all(row.actor_type in {"user", "agent", "system"} and row.actor_id for row in rows)
    assert all(row.hash and len(row.hash) == 64 for row in rows)
    # Review events are the reviewer's, except the reopens the outcomes matcher wrote for take-backs.
    assert {row.actor_id for row in rows if row.action in REVIEW_EVENTS and row.actor_type == "user"} == {
        f"user:{reviewer.id}"
    }
    system_reviews = [row for row in rows if row.action in REVIEW_EVENTS and row.actor_type != "user"]
    assert {(row.actor_type, row.actor_id, row.action) for row in system_reviews} == {
        ("system", "outcomes", "work_item_reopened")
    }
    assert len(system_reviews) == len(flow.taken_back)
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
