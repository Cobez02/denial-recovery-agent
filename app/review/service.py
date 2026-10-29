"""Review queue operations (milestone M7).

Everything a reviewer can do to a work item lives here: list the queue in SPEC order, bundle the
facts for the item page, record views, approve / edit / reject / mark submitted / reopen / close,
attach a quoted note excerpt and ask the pipeline for a new proposal. Every operation writes one
``actions`` row and one audit event with ``actor_type=user`` and ``actor_id=user:<id>``, validates
the status transition (``ValueError("invalid_transition")``) and never commits: the route does.

Rules enforced here, not in the templates: a coding change (or a proposal that asks for one) needs
a quoted note excerpt before approval; edits and rejections need a reason; note text that looks
like planted instructions is refused.
"""

from __future__ import annotations

import copy
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session, selectinload

from app import audit
from app.auth.service import actor_for, as_utc
from app.classify.codes import carc_text, rarc_text
from app.config import get_settings
from app.drafting.corrected_claim import (
    DATE_FIELDS,
    LIST_FIELDS,
    MONEY_FIELDS,
    format_money,
    parse_path,
    render_diff,
    resolve_path,
    set_path,
)
from app.enums import ActionType, ActorType, ClaimStatus, ReviewActionType, WorkItemStatus
from app.models import (
    Claim,
    ModelCall,
    NoteExcerpt,
    Outcome,
    Patient,
    Payer,
    PayerDocument,
    Proposal,
    Remittance,
    RemittanceClaim,
    ReviewAction,
    WorkItem,
)
from app.redact.injection import detect_injection, is_suspicious
from app.schemas import CorrectedClaim, CorrectedClaimChange

log = logging.getLogger(__name__)

STATUS_ALL = "all"
DEFAULT_LIMIT = 200
SORTS: tuple[str, ...] = ("default", "dollars", "priority", "newest", "deadline")
NOTE_SOURCE_REVIEWER = "reviewer"
EDITABLE_TEXT_FIELDS: tuple[str, ...] = ("summary", "rationale", "appeal_letter", "clinic_question")
# Editing one of these on a corrected claim is a coding change: it then needs a note excerpt.
CODING_FIELDS: frozenset[str] = frozenset(
    {"procedure_code", "modifiers", "diagnosis_codes", "diagnosis_pointers"}
)

_S = WorkItemStatus
_OPEN_FOR_DECISION: frozenset[str] = frozenset({str(_S.PROPOSED), str(_S.IN_REVIEW)})
_ALL_STATUSES: frozenset[str] = frozenset(str(s) for s in WorkItemStatus)
ALLOWED_STATUSES: dict[str, frozenset[str]] = {
    "approve": _OPEN_FOR_DECISION,
    "reject": _OPEN_FOR_DECISION,
    "edit": _OPEN_FOR_DECISION,
    "regenerate": _OPEN_FOR_DECISION,
    "export": frozenset({str(_S.APPROVED), str(_S.EXPORTED), str(_S.SUBMITTED)}),
    "mark_submitted": frozenset({str(_S.APPROVED), str(_S.EXPORTED)}),
    "reopen": frozenset({str(_S.REJECTED), str(_S.APPROVED)}),
    "close": _ALL_STATUSES - {str(_S.CLOSED)},
    "attach_note": _ALL_STATUSES - {str(_S.CLOSED)},
}
_LIST_SPLIT = re.compile(r"[|,]")


def utcnow() -> datetime:
    return datetime.now(UTC)


def today_utc() -> date:
    return utcnow().date()


# --------------------------------------------------------------------------------------
# Queue
# --------------------------------------------------------------------------------------


def _deadline_nulls_last() -> Any:
    """Portable NULLS LAST: a case expression both SQLite and Postgres order the same way."""
    return case((WorkItem.deadline.is_(None), 1), else_=0)


def _order_for(sort: str) -> list[Any]:
    base = [
        _deadline_nulls_last(),
        WorkItem.deadline.asc(),
        WorkItem.dollar_value.desc(),
        WorkItem.category_confidence.desc(),
        WorkItem.id.asc(),
    ]
    if sort == "dollars":
        return [WorkItem.dollar_value.desc(), *base]
    if sort == "priority":
        return [WorkItem.priority_score.desc(), *base]
    if sort == "newest":
        return [WorkItem.id.desc()]
    return base


def _flag_matches(work_item: WorkItem, flagged: bool | str) -> bool:
    flags = [str(f) for f in (work_item.flags or [])]
    if flagged is True:
        return bool(flags)
    if flagged is False:
        return not flags
    return str(flagged) in flags


def queue(
    session: Session,
    *,
    status: str | None = None,
    category: str | None = None,
    payer_id: int | None = None,
    flagged: bool | str | None = None,
    sort: str = "default",
    limit: int = DEFAULT_LIMIT,
) -> list[WorkItem]:
    """Work items in review order: deadline asc (NULLs last), dollars desc, confidence desc.

    Without a status filter the closed and rejected items are left out; ``status="all"`` lists
    every status. ``flagged`` is True (any flag), False (no flags) or one flag name.
    """
    stmt = select(WorkItem).options(
        selectinload(WorkItem.claim).selectinload(Claim.payer),
        selectinload(WorkItem.claim).selectinload(Claim.patient),
        selectinload(WorkItem.proposals),
    )
    if status and status != STATUS_ALL:
        stmt = stmt.where(WorkItem.status == str(status))
    elif not status:
        stmt = stmt.where(WorkItem.status.not_in([str(_S.CLOSED), str(_S.REJECTED)]))
    if category:
        stmt = stmt.where(WorkItem.category == str(category))
    if payer_id is not None:
        stmt = stmt.join(Claim, Claim.id == WorkItem.claim_id).where(Claim.payer_id == int(payer_id))
    stmt = stmt.order_by(*_order_for(sort if sort in SORTS else "default"))
    items = list(session.execute(stmt).scalars().unique())
    if flagged is not None:
        items = [wi for wi in items if _flag_matches(wi, flagged)]
    return items[: max(int(limit), 0)] if limit is not None else items


def status_counts(session: Session) -> dict[str, int]:
    counts = {str(s): 0 for s in WorkItemStatus}
    stmt = select(WorkItem.status, func.count(WorkItem.id)).group_by(WorkItem.status)
    for status, n in session.execute(stmt):
        counts[str(status)] = int(n)
    return counts


def deadline_badge(work_item: WorkItem, today: date | None = None) -> str | None:
    """'overdue', '<=14d', '<=30d' (thresholds from DEADLINE_WARN_DAYS) or None."""
    deadline = work_item.deadline
    if deadline is None:
        return None
    today = today or today_utc()
    days = (deadline - today).days
    if days < 0:
        return "overdue"
    for threshold in sorted(set(get_settings().warn_days)):
        if days <= threshold:
            return f"<={threshold}d"
    return None


def days_left(work_item: WorkItem, today: date | None = None) -> int | None:
    if work_item.deadline is None:
        return None
    return (work_item.deadline - (today or today_utc())).days


# --------------------------------------------------------------------------------------
# Item detail
# --------------------------------------------------------------------------------------


@dataclass
class DocumentView:
    id: int
    document_type: str
    received_date: date | None
    text: str
    flagged: bool
    findings: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ItemDetail:
    """Everything the item page and the JSON API show for one work item."""

    work_item: WorkItem
    claim: Claim
    patient: Patient | None
    payer: Payer | None
    remittance: Remittance | None
    remittance_claim: RemittanceClaim
    adjustments: list[dict[str, Any]]
    remark_codes: list[dict[str, Any]]
    reasons: list[dict[str, Any]]
    documents: list[DocumentView]
    notes: list[NoteExcerpt]
    proposal: Proposal | None
    proposals: list[Proposal]
    citations: list[dict[str, Any]]
    corrected_claim: CorrectedClaim | None
    diff_rows: list[dict[str, Any]]
    actions: list[ReviewAction]
    outcomes: list[Outcome]
    model_calls: int
    badge: str | None
    days_left: int | None
    needs_note_excerpt: bool
    note_excerpt_attached: bool
    today: date

    @property
    def status(self) -> str:
        return str(self.work_item.status)

    @property
    def can(self) -> dict[str, bool]:
        """Which actions the current status allows (drives the forms on the page)."""
        status = self.status
        return {name: status in allowed for name, allowed in ALLOWED_STATUSES.items()}


def _adjustment_views(rc: RemittanceClaim) -> list[dict[str, Any]]:
    rows = []
    for adj in sorted(rc.adjustments, key=lambda a: a.id or 0):
        line = adj.remittance_line
        rows.append(
            {
                "id": adj.id,
                "level": adj.level,
                "line_number": line.line_number if line is not None else None,
                "procedure_code": line.procedure_code if line is not None else None,
                "group_code": adj.group_code,
                "carc": adj.carc,
                "carc_text": carc_text(adj.carc),
                "amount": adj.amount,
                "quantity": adj.quantity,
                "is_denial": bool(adj.is_denial),
            }
        )
    return rows


def _remark_views(rc: RemittanceClaim) -> list[dict[str, Any]]:
    rows = [{"code": str(c), "text": rarc_text(str(c)), "line_number": None} for c in (rc.remark_codes or [])]
    for line in rc.lines:
        rows.extend(
            {"code": str(c), "text": rarc_text(str(c)), "line_number": line.line_number}
            for c in (line.remark_codes or [])
        )
    return rows


def _reason_views(work_item: WorkItem) -> list[dict[str, Any]]:
    rows = []
    for raw in work_item.all_reasons or []:
        if not isinstance(raw, dict):
            continue
        carc = str(raw.get("carc") or "")
        texts = raw.get("rarc_texts") or {}
        rarcs = [
            {"code": str(c), "text": texts.get(str(c)) or rarc_text(str(c))} for c in raw.get("rarcs") or []
        ]
        rows.append({**raw, "carc_text": raw.get("carc_text") or carc_text(carc), "rarc_views": rarcs})
    return rows


def _document_views(session: Session, claim: Claim, rc: RemittanceClaim) -> list[DocumentView]:
    docs: list[PayerDocument] = list(claim.documents)
    stmt = select(PayerDocument).where(PayerDocument.remittance_claim_id == rc.id).order_by(PayerDocument.id)
    seen = {d.id for d in docs}
    docs.extend(d for d in session.execute(stmt).scalars() if d.id not in seen)
    views: list[DocumentView] = []
    for doc in sorted(docs, key=lambda d: d.id or 0):
        findings = [dict(f) for f in (doc.injection_findings or []) if isinstance(f, dict)]
        if not findings and not doc.flagged_injection:
            # Defence in depth: documents stored before the scanner existed are scanned here.
            findings = [f.model_dump() for f in detect_injection(doc.text)]
        views.append(
            DocumentView(
                id=doc.id,
                document_type=doc.document_type,
                received_date=doc.received_date,
                text=doc.text or "",
                flagged=bool(doc.flagged_injection or findings),
                findings=findings,
            )
        )
    return views


def load_corrected_claim(proposal: Proposal | None) -> CorrectedClaim | None:
    if proposal is None or not proposal.corrected_claim:
        return None
    try:
        return CorrectedClaim.model_validate(proposal.corrected_claim)
    except Exception:  # a hand-edited or legacy JSON blob: show the proposal without the diff
        log.warning("proposal %s carries an unreadable corrected_claim", proposal.id)
        return None


def has_note_excerpt(work_item: WorkItem) -> bool:
    return any(str(a.action) == str(ReviewActionType.ATTACH_NOTE) for a in work_item.actions)


def needs_note_excerpt(proposal: Proposal | None) -> bool:
    return bool(proposal is not None and (proposal.requires_note_excerpt or proposal.coding_change))


def get_work_item(session: Session, work_item_id: int) -> WorkItem | None:
    stmt = (
        select(WorkItem)
        .where(WorkItem.id == int(work_item_id))
        .options(
            selectinload(WorkItem.claim).selectinload(Claim.lines),
            selectinload(WorkItem.claim).selectinload(Claim.patient),
            selectinload(WorkItem.claim).selectinload(Claim.payer),
            selectinload(WorkItem.claim).selectinload(Claim.notes),
            selectinload(WorkItem.claim).selectinload(Claim.documents),
            selectinload(WorkItem.remittance_claim).selectinload(RemittanceClaim.remittance),
            selectinload(WorkItem.remittance_claim).selectinload(RemittanceClaim.lines),
            selectinload(WorkItem.remittance_claim).selectinload(RemittanceClaim.adjustments),
            selectinload(WorkItem.proposals),
            selectinload(WorkItem.actions).selectinload(ReviewAction.user),
            selectinload(WorkItem.outcomes),
        )
    )
    return session.execute(stmt).scalars().first()


def item_detail(session: Session, work_item_id: int, *, today: date | None = None) -> ItemDetail | None:
    work_item = get_work_item(session, work_item_id)
    if work_item is None:
        return None
    today = today or today_utc()
    claim = work_item.claim
    rc = work_item.remittance_claim
    proposal = work_item.current_proposal
    corrected = load_corrected_claim(proposal)
    model_calls = session.execute(
        select(func.count(ModelCall.id)).where(ModelCall.work_item_id == work_item.id)
    ).scalar_one()
    return ItemDetail(
        work_item=work_item,
        claim=claim,
        patient=claim.patient,
        payer=claim.payer,
        remittance=rc.remittance if rc is not None else None,
        remittance_claim=rc,
        adjustments=_adjustment_views(rc) if rc is not None else [],
        remark_codes=_remark_views(rc) if rc is not None else [],
        reasons=_reason_views(work_item),
        documents=_document_views(session, claim, rc) if rc is not None else [],
        notes=sorted(claim.notes, key=lambda n: (n.note_date or date.min, n.id or 0)),
        proposal=proposal,
        proposals=sorted(work_item.proposals, key=lambda p: p.version),
        citations=[c for c in (proposal.citations or []) if isinstance(c, dict)] if proposal else [],
        corrected_claim=corrected,
        diff_rows=render_diff(corrected) if corrected is not None else [],
        actions=sorted(work_item.actions, key=lambda a: a.id or 0),
        outcomes=sorted(work_item.outcomes, key=lambda o: o.id or 0),
        model_calls=int(model_calls or 0),
        badge=deadline_badge(work_item, today),
        days_left=days_left(work_item, today),
        needs_note_excerpt=needs_note_excerpt(proposal),
        note_excerpt_attached=has_note_excerpt(work_item),
        today=today,
    )


# --------------------------------------------------------------------------------------
# Shared helpers for actions
# --------------------------------------------------------------------------------------


def _require_status(work_item: WorkItem, action: str) -> None:
    if str(work_item.status) not in ALLOWED_STATUSES[action]:
        raise ValueError("invalid_transition")


def _current_proposal_or_raise(work_item: WorkItem) -> Proposal:
    proposal = work_item.current_proposal
    if proposal is None:
        raise ValueError("no_proposal")
    return proposal


def _add_action(
    session: Session,
    work_item: WorkItem,
    user: Any,
    action: ReviewActionType,
    *,
    proposal: Proposal | None = None,
    reason: str | None = None,
    payload: dict[str, Any] | None = None,
    review_seconds: int | None = None,
) -> ReviewAction:
    row = ReviewAction(
        work_item_id=work_item.id,
        proposal_id=proposal.id if proposal is not None else None,
        user_id=getattr(user, "id", None),
        action=str(action),
        reason=reason,
        edited_payload=payload,
        review_seconds=review_seconds,
    )
    session.add(row)
    work_item.actions.append(row)
    session.flush()
    return row


def _audit(
    session: Session,
    work_item: WorkItem,
    user: Any,
    action: str,
    *,
    reason: str | None = None,
    details: dict[str, Any] | None = None,
    entity_type: str = "work_item",
    entity_id: int | None = None,
) -> None:
    audit.record(
        session,
        actor_type=ActorType.USER,
        actor_id=actor_for(user),
        action=action,
        entity_type=entity_type,
        entity_id=work_item.id if entity_id is None else entity_id,
        claim_id=work_item.claim_id,
        reason=reason,
        details={"work_item_id": work_item.id, "status": str(work_item.status), **(details or {})},
    )


def _decide(work_item: WorkItem, now: datetime) -> int | None:
    """Stamp decided_at and return review_seconds (decided_at - first_viewed_at) when a view exists."""
    work_item.decided_at = now
    first = as_utc(work_item.first_viewed_at)
    if first is None:
        return None
    return max(int((now - first).total_seconds()), 0)


def _clean_reason(reason: str | None) -> str:
    text = (reason or "").strip()
    if not text:
        raise ValueError("reason_required")
    return text[:2000]


def _next_version(work_item: WorkItem, source: Proposal, *, generated_by: str) -> Proposal:
    """A copy of ``source`` as the new current version (the others are demoted)."""
    version = max((p.version for p in work_item.proposals), default=0) + 1
    for p in work_item.proposals:
        p.is_current = False
    clone = Proposal(
        work_item_id=work_item.id,
        version=version,
        is_current=True,
        action_type=source.action_type,
        summary=source.summary,
        rationale=source.rationale,
        citations=copy.deepcopy(source.citations or []),
        corrected_claim=copy.deepcopy(source.corrected_claim) if source.corrected_claim else None,
        appeal_letter=source.appeal_letter,
        clinic_question=source.clinic_question,
        coding_change=bool(source.coding_change),
        requires_note_excerpt=bool(source.requires_note_excerpt),
        confidence=source.confidence,
        prompt_version=source.prompt_version,
        model_id=source.model_id,
        generated_by=generated_by,
    )
    work_item.proposals.append(clone)  # the relationship registers the row with the session
    return clone


# --------------------------------------------------------------------------------------
# Actions
# --------------------------------------------------------------------------------------


def record_view(session: Session, work_item: WorkItem, user: Any) -> ReviewAction | None:
    """First view: stamp first_viewed_at, move proposed -> in_review, store one 'view' per user."""
    now = utcnow()
    if work_item.first_viewed_at is None:
        work_item.first_viewed_at = now
    if str(work_item.status) == str(_S.PROPOSED):
        work_item.status = str(_S.IN_REVIEW)
    user_id = getattr(user, "id", None)
    if any(str(a.action) == str(ReviewActionType.VIEW) and a.user_id == user_id for a in work_item.actions):
        session.flush()
        return None
    action = _add_action(session, work_item, user, ReviewActionType.VIEW, proposal=work_item.current_proposal)
    _audit(session, work_item, user, "work_item_viewed")
    return action


def attach_note(
    session: Session, work_item: WorkItem, user: Any, *, text: str, today: date | None = None
) -> ReviewAction:
    """Attach a quoted note excerpt typed by the reviewer (refused when it looks like instructions)."""
    body = (text or "").strip()
    if not body:
        raise ValueError("text_required")
    if is_suspicious(body):
        raise ValueError("suspicious_text")
    _require_status(work_item, "attach_note")
    note = NoteExcerpt(
        claim_id=work_item.claim_id,
        note_date=today or today_utc(),
        author=str(getattr(user, "email", "") or "reviewer")[:200],
        text=body,
        source=NOTE_SOURCE_REVIEWER,
    )
    session.add(note)
    session.flush()
    action = _add_action(
        session,
        work_item,
        user,
        ReviewActionType.ATTACH_NOTE,
        proposal=work_item.current_proposal,
        payload={"note_id": note.id, "length": len(body)},
    )
    _audit(
        session,
        work_item,
        user,
        "note_attached",
        entity_type="note_excerpt",
        entity_id=note.id,
        details={"length": len(body)},
    )
    return action


def approve(
    session: Session,
    work_item: WorkItem,
    user: Any,
    *,
    note_excerpt: str | None = None,
    edited_letter: str | None = None,
) -> ReviewAction:
    """Approve the current proposal. A coding change needs a note excerpt (given now or attached before).

    A note excerpt given here is always attached (it is the reviewer's evidence, never dropped);
    without one, a coding change is refused unless an excerpt was attached earlier.
    """
    _require_status(work_item, "approve")
    proposal = _current_proposal_or_raise(work_item)
    excerpt = (note_excerpt or "").strip()
    if excerpt:
        attach_note(session, work_item, user, text=excerpt)
    elif needs_note_excerpt(proposal) and not has_note_excerpt(work_item):
        raise ValueError("note_excerpt_required")
    if edited_letter is not None:
        letter = str(edited_letter).replace("\r\n", "\n")
        if letter.strip() and letter.strip() != (proposal.appeal_letter or "").replace("\r\n", "\n").strip():
            edit(
                session,
                work_item,
                user,
                reason="letter edited at approval",
                edited_payload={"appeal_letter": letter},
            )
            proposal = _current_proposal_or_raise(work_item)
    now = utcnow()
    seconds = _decide(work_item, now)
    work_item.status = str(_S.APPROVED)
    action = _add_action(
        session, work_item, user, ReviewActionType.APPROVE, proposal=proposal, review_seconds=seconds
    )
    _audit(
        session,
        work_item,
        user,
        "work_item_approved",
        entity_type="work_item",
        details={
            "proposal_id": proposal.id,
            "version": proposal.version,
            "action_type": str(proposal.action_type),
            "coding_change": bool(proposal.coding_change),
            "note_excerpt_attached": has_note_excerpt(work_item),
            "review_seconds": seconds,
        },
    )
    log.info("work item %s approved (proposal %s)", work_item.id, proposal.id)
    return action


def _coerce_list(field_name: str, value: Any) -> Any:
    if isinstance(value, str) and field_name in LIST_FIELDS:
        return [part.strip() for part in _LIST_SPLIT.split(value) if part.strip()]
    return value


def _validate_value(field_name: str, value: Any) -> None:
    """Refuse a value the export could not carry: bad money or units, a bad date, a blank procedure code."""
    if value is None or value == []:
        if field_name == "procedure_code":
            raise ValueError("invalid_value")
        return
    if field_name in MONEY_FIELDS or field_name == "units":
        try:
            amount = Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError("invalid_value") from exc
        if not amount.is_finite() or amount < 0 or (field_name == "units" and amount == 0):
            raise ValueError("invalid_value")
    elif field_name in DATE_FIELDS:
        try:
            date.fromisoformat(str(value))
        except ValueError as exc:
            raise ValueError("invalid_value") from exc


def _apply_corrected_claim_edits(
    current: Proposal, edits: dict[str, Any], reason: str
) -> tuple[dict[str, Any], bool, list[str]]:
    """Apply ``{path: value}`` edits to the corrected claim; returns (json, coding_change, field names).

    The edit form posts every editable field, so a value equal to the current one is not an edit:
    it is skipped and does not count as a change (nor as a coding change).
    """
    corrected = load_corrected_claim(current)
    if corrected is None:
        raise ValueError("no_corrected_claim")
    after = copy.deepcopy(corrected.after)
    changes = list(corrected.changes)
    coding = False
    fields: list[str] = []
    for raw_path, raw_value in edits.items():
        path = str(raw_path).strip()
        try:
            _line, field_name = parse_path(path)
            current_value = resolve_path(after, path)
        except ValueError as exc:
            raise ValueError("invalid_path") from exc
        try:
            set_path(after, path, _coerce_list(field_name, raw_value))
        except ValueError as exc:  # a pointer that is not a number, for example
            raise ValueError("invalid_value") from exc
        new_value = resolve_path(after, path)
        _validate_value(field_name, new_value)
        if new_value == current_value:
            continue
        is_coding = field_name in CODING_FIELDS
        for index, change in enumerate(changes):
            if change.path == path:
                changes[index] = change.model_copy(
                    update={"after": new_value, "coding_change": bool(change.coding_change or is_coding)}
                )
                break
        else:
            changes.append(
                CorrectedClaimChange(
                    path=path,
                    before=resolve_path(corrected.before, path),
                    after=new_value,
                    reason=f"Reviewer edit: {reason}",
                    coding_change=is_coding,
                )
            )
        coding = coding or is_coding
        fields.append(f"corrected_claim.{path}")
    total = Decimal("0")
    for line in after.get("lines", []):
        try:
            total += Decimal(str(line.get("charge_amount") or "0"))
        except InvalidOperation:
            continue
    after["claim_total_charge"] = format_money(total)
    rebuilt = CorrectedClaim(before=corrected.before, after=after, changes=changes)
    return rebuilt.model_dump(mode="json"), coding, fields


def edit(
    session: Session,
    work_item: WorkItem,
    user: Any,
    *,
    reason: str,
    edited_payload: dict[str, Any],
) -> ReviewAction:
    """Create proposal version n+1 with the edited fields. The status stays proposed / in_review.

    ``edited_payload`` keys: summary, rationale, appeal_letter, clinic_question and
    ``corrected_claim`` ({path: value}, paths as in app.drafting.corrected_claim). Editing a code,
    modifier or diagnosis field turns the proposal into a coding change.
    """
    reason_text = _clean_reason(reason)
    _require_status(work_item, "edit")
    current = _current_proposal_or_raise(work_item)
    payload = dict(edited_payload or {})
    fields: list[str] = []
    text_updates: dict[str, str] = {}
    for name in EDITABLE_TEXT_FIELDS:
        if name not in payload or payload[name] is None:
            continue
        value = str(payload[name]).replace("\r\n", "\n")
        if not value.strip():
            continue  # the form posts every field; a blank one means "leave it as it is"
        if value.strip() != (getattr(current, name) or "").replace("\r\n", "\n").strip():
            text_updates[name] = value
            fields.append(name)
    cc_edits = payload.get("corrected_claim") or {}
    corrected_json: dict[str, Any] | None = None
    coding = False
    if cc_edits:
        corrected_json, coding, cc_fields = _apply_corrected_claim_edits(current, dict(cc_edits), reason_text)
        fields.extend(cc_fields)
        if not cc_fields:
            corrected_json = None  # every corrected-claim field was posted unchanged
    if not fields:
        raise ValueError("nothing_changed")

    proposal = _next_version(work_item, current, generated_by="reviewer")
    for name, value in text_updates.items():
        setattr(proposal, name, value)
    if corrected_json is not None:
        proposal.corrected_claim = corrected_json
    if coding:
        proposal.coding_change = True
        proposal.requires_note_excerpt = True
        flags = [str(f) for f in (work_item.flags or [])]
        if "coding_change" not in flags:
            work_item.flags = [*flags, "coding_change"]
    session.flush()
    action = _add_action(
        session,
        work_item,
        user,
        ReviewActionType.EDIT,
        proposal=proposal,
        reason=reason_text,
        payload={"fields": fields, "edits": payload, "version": proposal.version},
    )
    _audit(
        session,
        work_item,
        user,
        "proposal_edited",
        reason=reason_text,
        entity_type="proposal",
        entity_id=proposal.id,
        details={
            "fields": fields,
            "version": proposal.version,
            "coding_change": bool(proposal.coding_change),
        },
    )
    return action


def reject(session: Session, work_item: WorkItem, user: Any, *, reason: str) -> ReviewAction:
    reason_text = _clean_reason(reason)
    _require_status(work_item, "reject")
    proposal = work_item.current_proposal
    now = utcnow()
    seconds = _decide(work_item, now)
    work_item.status = str(_S.REJECTED)
    action = _add_action(
        session,
        work_item,
        user,
        ReviewActionType.REJECT,
        proposal=proposal,
        reason=reason_text,
        review_seconds=seconds,
    )
    _audit(
        session,
        work_item,
        user,
        "work_item_rejected",
        reason=reason_text,
        details={"proposal_id": proposal.id if proposal else None, "review_seconds": seconds},
    )
    log.info("work item %s rejected", work_item.id)
    return action


def default_submitted_control_number(work_item: WorkItem) -> str:
    """The corrected claim's '-R1' number for a corrected claim, else the original claim's number."""
    proposal = work_item.current_proposal
    if proposal is not None and str(proposal.action_type) == str(ActionType.CORRECTED_CLAIM):
        after = (proposal.corrected_claim or {}).get("after") or {}
        number = after.get("claim_control_number")
        if number:
            return str(number)
    return work_item.claim.claim_control_number


def mark_submitted(
    session: Session,
    work_item: WorkItem,
    user: Any,
    *,
    submitted_on: date | None = None,
    submitted_claim_control_number: str | None = None,
) -> ReviewAction:
    """The human submitted the package to the payer: record when and under which control number."""
    _require_status(work_item, "mark_submitted")
    proposal = work_item.current_proposal
    number = (submitted_claim_control_number or "").strip()[:64] or default_submitted_control_number(
        work_item
    )
    when = (
        datetime(submitted_on.year, submitted_on.month, submitted_on.day, 12, 0, tzinfo=UTC)
        if submitted_on
        else utcnow()
    )
    work_item.status = str(_S.SUBMITTED)
    work_item.submitted_at = when
    work_item.submitted_claim_control_number = number
    action_type = str(proposal.action_type) if proposal else None
    if action_type == str(ActionType.CORRECTED_CLAIM):
        work_item.claim.status = str(ClaimStatus.CORRECTED)
    elif action_type == str(ActionType.APPEAL):
        work_item.claim.status = str(ClaimStatus.APPEALED)
    action = _add_action(
        session,
        work_item,
        user,
        ReviewActionType.MARK_SUBMITTED,
        proposal=proposal,
        payload={"submitted_on": when.date().isoformat(), "submitted_claim_control_number": number},
    )
    _audit(
        session,
        work_item,
        user,
        "work_item_submitted",
        details={
            "action_type": action_type,
            "submitted_on": when.date().isoformat(),
            "claim_status": str(work_item.claim.status),
        },
    )
    return action


def reopen(session: Session, work_item: WorkItem, user: Any, *, reason: str | None = None) -> ReviewAction:
    _require_status(work_item, "reopen")
    previous = str(work_item.status)
    reason_text = (reason or "").strip()[:2000] or None
    work_item.status = str(_S.PROPOSED)
    work_item.decided_at = None
    action = _add_action(
        session,
        work_item,
        user,
        ReviewActionType.REOPEN,
        proposal=work_item.current_proposal,
        reason=reason_text,
        payload={"previous_status": previous},
    )
    _audit(
        session,
        work_item,
        user,
        "work_item_reopened",
        reason=reason_text,
        details={"previous_status": previous},
    )
    return action


def close(session: Session, work_item: WorkItem, user: Any, *, reason: str | None = None) -> ReviewAction:
    _require_status(work_item, "close")
    previous = str(work_item.status)
    reason_text = (reason or "").strip()[:2000] or None
    work_item.status = str(_S.CLOSED)
    work_item.closed_at = utcnow()
    action = _add_action(
        session,
        work_item,
        user,
        ReviewActionType.CLOSE,
        proposal=work_item.current_proposal,
        reason=reason_text,
        payload={"previous_status": previous},
    )
    _audit(
        session,
        work_item,
        user,
        "work_item_closed",
        reason=reason_text,
        details={"previous_status": previous},
    )
    return action


def regenerate(
    session: Session, work_item: WorkItem, user: Any, *, today: date | None = None
) -> ReviewAction:
    """Ask the pipeline for a fresh classification and proposal (a new version, audited by the agent)."""
    _require_status(work_item, "regenerate")
    from app.pipeline import regenerate_proposal  # the pipeline is heavy; import on use

    proposal = regenerate_proposal(session, work_item, today=today, actor_id=actor_for(user))
    # The queue filter and the item chips read the flags; keep the coding flag true to the new version.
    flags = [str(f) for f in (work_item.flags or []) if str(f) != "coding_change"]
    if proposal.coding_change:
        flags.append("coding_change")
    work_item.flags = flags
    action = _add_action(
        session,
        work_item,
        user,
        ReviewActionType.REGENERATE,
        proposal=proposal,
        reason="proposal regenerated",
        payload={"regenerated": True, "version": proposal.version},
    )
    _audit(
        session,
        work_item,
        user,
        "proposal_regenerate_requested",
        entity_type="proposal",
        entity_id=proposal.id,
        details={"version": proposal.version, "action_type": str(proposal.action_type)},
    )
    return action


__all__ = [
    "ALLOWED_STATUSES",
    "CODING_FIELDS",
    "DocumentView",
    "ItemDetail",
    "approve",
    "attach_note",
    "close",
    "days_left",
    "deadline_badge",
    "default_submitted_control_number",
    "edit",
    "get_work_item",
    "has_note_excerpt",
    "item_detail",
    "load_corrected_claim",
    "mark_submitted",
    "needs_note_excerpt",
    "queue",
    "record_view",
    "regenerate",
    "reject",
    "reopen",
    "status_counts",
]
