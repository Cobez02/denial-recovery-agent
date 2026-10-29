"""Pipeline: denied remittance claim -> DenialContext -> classify -> propose -> draft -> WorkItem + Proposal.

Every step is recorded in the audit log. The model is only ever reached through app.llm.guard,
with a registry of this claim's identifiers (the clinic's record and the payer's own spelling of
the patient from the 835) so names, member ids, addresses and dates are tokenized first. Payer
documents and note excerpts are untrusted: both are scanned for planted instructions (at import and
again here) and a finding flags the work item. Nothing here submits anything; the output is a
proposal for a human to review.

``process_pending`` handles each denied remittance claim inside its own savepoint: a claim that
fails (a model transport error, a playbook bug) is rolled back to the savepoint and recorded as a
``pipeline_failed`` audit event with the exception class name, and the run continues with the next
claim, so one bad denial never discards the batch's finished work items or re-spends their model
calls. The failed claims stay pending for the next run.
"""

from __future__ import annotations

import logging
import traceback
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.classify import classify, enrich_reasons
from app.config import get_settings
from app.drafting.appeal import draft_appeal
from app.enums import ActionType, ActorType, ClassificationSource, WorkItemStatus
from app.ingest.denials import denial_reasons_for
from app.llm.base import LLMClient
from app.llm.factory import get_client
from app.llm.guard import make_guard_call
from app.models import Claim, Proposal, RemittanceClaim, WorkItem
from app.playbooks import priority_score, propose
from app.playbooks.base import FLAG_CODING_CHANGE
from app.redact.injection import detect_injection
from app.redact.registry import IdentifierRegistry
from app.schemas import (
    ClaimContext,
    ClaimLineContext,
    ClassificationResult,
    DenialContext,
    DocumentContext,
    InjectionFinding,
    NoteContext,
    PatientIdentifiers,
    PayerContext,
    ProposalDraft,
    RemittanceContext,
)

log = logging.getLogger(__name__)

PIPELINE_ACTOR = "pipeline"
FLAG_INJECTION = "injection_flagged"


@dataclass(frozen=True)
class PipelineFailure:
    """One remittance claim ``process_pending`` could not turn into a work item (class name only)."""

    remittance_claim_id: int
    claim_id: int | None
    error: str


class PipelineRun(list[WorkItem]):
    """The work items ``process_pending`` created, plus the claims it had to skip (``failed``)."""

    def __init__(self, created: list[WorkItem] | None = None, failed: list[PipelineFailure] | None = None):
        super().__init__(created or [])
        self.failed: list[PipelineFailure] = list(failed or [])


def _utcnow() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------------------
# Context building
# --------------------------------------------------------------------------------------


def build_context(
    session: Session, rc: RemittanceClaim, *, today: date | None = None
) -> DenialContext | None:
    """Assemble everything the classifier, playbooks and drafter need for one denied claim."""
    claim: Claim | None = rc.claim
    if claim is None:
        return None
    today = today or date.today()
    patient = claim.patient
    payer = claim.payer
    remittance = rc.remittance

    paid_by_line: dict[int, Decimal] = {}
    for rl in rc.lines:
        if rl.claim_line_id is not None:
            paid_by_line[rl.claim_line_id] = Decimal(rl.paid_amount or 0)

    lines = [
        ClaimLineContext(
            line_number=cl.line_number,
            service_date=cl.service_date,
            procedure_code=cl.procedure_code,
            modifiers=list(cl.modifiers or []),
            units=Decimal(cl.units or 1),
            charge_amount=Decimal(cl.charge_amount or 0),
            diagnosis_pointers=[int(p) for p in (cl.diagnosis_pointers or [])],
            ndc=cl.ndc,
            line_item_control_number=cl.line_item_control_number,
            paid_amount=paid_by_line.get(cl.id),
        )
        for cl in claim.lines
    ]

    identifiers = PatientIdentifiers(
        first_name=patient.first_name,
        last_name=patient.last_name,
        dob=patient.dob,
        member_id=patient.member_id,
        address_line1=patient.address_line1,
        city=patient.city,
        state=patient.state,
        zip=patient.zip,
        account_number=patient.account_number,
    )

    claim_ctx = ClaimContext(
        claim_id=claim.id,
        claim_control_number=claim.claim_control_number,
        patient_token=patient.token,
        patient_identifiers=identifiers,
        payer_name=payer.name,
        payer_id=payer.payer_id,
        billing_provider_npi=claim.billing_provider_npi,
        rendering_provider_npi=claim.rendering_provider_npi,
        rendering_provider_name=claim.rendering_provider_name,
        referring_provider_npi=claim.referring_provider_npi,
        place_of_service=claim.place_of_service,
        diagnosis_codes=list(claim.diagnosis_codes or []),
        prior_auth_number=claim.prior_auth_number,
        service_date_from=claim.service_date_from,
        service_date_to=claim.service_date_to,
        total_charge=Decimal(claim.total_charge or 0),
        original_submission_date=claim.original_submission_date,
        frequency_code=claim.frequency_code or "1",
        lines=lines,
    )

    payer_ctx = PayerContext(
        name=payer.name,
        payer_id=payer.payer_id,
        timely_filing_days=payer.timely_filing_days,
        corrected_claim_days=payer.corrected_claim_days,
        appeal_days=payer.appeal_days,
    )

    remit_ctx = RemittanceContext(
        remittance_claim_id=rc.id,
        payment_date=remittance.payment_date if remittance else None,
        claim_received_date=rc.claim_received_date,
        claim_status_code=rc.claim_status_code,
        charge_amount=Decimal(rc.charge_amount or 0),
        paid_amount=Decimal(rc.paid_amount or 0),
        patient_responsibility=Decimal(rc.patient_responsibility or 0),
        payer_claim_control_number=rc.payer_claim_control_number,
        trace_number=remittance.trace_number if remittance else None,
        denied_amount=Decimal(rc.denied_amount or 0),
        remark_codes=list(rc.remark_codes or []),
        # The payer's spelling of the patient (NM1*QC) may differ from the clinic's record; the
        # registry tokenizes both so a letter written in the payer's spelling is still redacted.
        patient_first_name=rc.patient_first_name,
        patient_last_name=rc.patient_last_name,
        member_id=rc.member_id,
    )

    reasons = enrich_reasons(denial_reasons_for(rc))

    documents: list[DocumentContext] = []
    seen_doc_ids: set[int] = set()
    for doc in list(claim.documents) + _documents_for_remittance_claim(session, rc):
        if doc.id in seen_doc_ids:
            continue
        seen_doc_ids.add(doc.id)
        findings = [InjectionFinding(**f) for f in (doc.injection_findings or [])]
        if not findings and not doc.flagged_injection:
            # Defence in depth: documents imported before the scanner existed are re-scanned here.
            findings = detect_injection(doc.text)
        documents.append(
            DocumentContext(
                document_id=doc.id,
                document_type=doc.document_type,
                received_date=doc.received_date,
                text=doc.text,
                flagged_injection=bool(doc.flagged_injection or findings),
                injection_findings=findings,
            )
        )

    notes: list[NoteContext] = []
    for note in claim.notes:
        note_findings = [InjectionFinding(**f) for f in (note.injection_findings or [])]
        if not note_findings and not note.flagged_injection:
            # Defence in depth: notes imported before the scanner existed are re-scanned here.
            note_findings = detect_injection(note.text)
        notes.append(
            NoteContext(
                note_id=note.id,
                note_date=note.note_date,
                author=note.author,
                text=note.text,
                flagged_injection=bool(note.flagged_injection or note_findings),
                injection_findings=note_findings,
            )
        )

    return DenialContext(
        claim=claim_ctx,
        payer=payer_ctx,
        remittance=remit_ctx,
        reasons=reasons,
        documents=documents,
        notes=notes,
        today=today,
    )


def _documents_for_remittance_claim(session: Session, rc: RemittanceClaim) -> list:
    from app.models import PayerDocument

    stmt = select(PayerDocument).where(PayerDocument.remittance_claim_id == rc.id)
    return list(session.execute(stmt).scalars())


# --------------------------------------------------------------------------------------
# Processing
# --------------------------------------------------------------------------------------


def _existing_work_item(session: Session, rc: RemittanceClaim) -> WorkItem | None:
    stmt = select(WorkItem).where(WorkItem.remittance_claim_id == rc.id).order_by(WorkItem.id)
    return session.execute(stmt).scalars().first()


def _resolve_client(llm_client: LLMClient | None) -> LLMClient | None:
    if llm_client is not None:
        return llm_client
    try:
        return get_client(get_settings())
    except Exception as exc:  # misconfigured real provider: run rules-only, never crash the import
        log.warning("model client unavailable (%s); continuing with rules only", type(exc).__name__)
        return None


def process_remittance_claim(
    session: Session,
    rc: RemittanceClaim,
    *,
    llm_client: LLMClient | None = None,
    today: date | None = None,
    actor_id: str = PIPELINE_ACTOR,
    resolve_client: bool = True,
) -> WorkItem | None:
    """Create a work item with a classified, cited proposal for one denied remittance claim.

    Idempotent: a remittance claim that already has a work item returns it unchanged. With
    `resolve_client=False` the given `llm_client` (possibly None: rules only) is used as is.
    """
    if not rc.is_denial or rc.claim_id is None:
        return None
    existing = _existing_work_item(session, rc)
    if existing is not None:
        return existing

    ctx = build_context(session, rc, today=today)
    if ctx is None:
        return None

    work_item = WorkItem(
        claim_id=rc.claim_id,
        remittance_claim_id=rc.id,
        category="other",
        category_confidence=0,
        classification_source=ClassificationSource.RULES,
        all_reasons=[r.model_dump(mode="json") for r in ctx.reasons],
        status=WorkItemStatus.NEW,
        dollar_value=ctx.remittance.denied_amount,
        flags=[],
    )
    session.add(work_item)
    session.flush()
    audit.record(
        session,
        actor_type=ActorType.AGENT,
        actor_id=actor_id,
        action="work_item_created",
        entity_type="work_item",
        entity_id=work_item.id,
        claim_id=rc.claim_id,
        details={
            "remittance_claim_id": rc.id,
            "denied_amount": str(ctx.remittance.denied_amount),
            "reasons": [f"{r.group_code}-{r.carc}" for r in ctx.reasons],
        },
    )

    client = _resolve_client(llm_client) if resolve_client else llm_client
    registry = IdentifierRegistry.from_context(ctx)
    guard_call = (
        make_guard_call(session, client, registry, work_item_id=work_item.id, purpose="denial_pipeline")
        if client is not None
        else None
    )

    classification = classify(ctx, guard_call)
    work_item.category = str(classification.category)
    work_item.category_confidence = classification.confidence
    work_item.classification_source = str(classification.source)
    work_item.classification_rationale = classification.rationale
    audit.record(
        session,
        actor_type=ActorType.AGENT,
        actor_id=actor_id,
        action="classified",
        entity_type="work_item",
        entity_id=work_item.id,
        claim_id=rc.claim_id,
        details={
            "category": str(classification.category),
            "confidence": round(float(classification.confidence), 3),
            "source": str(classification.source),
            "rule_id": classification.rule_id,
            "injection_detected": bool(classification.injection_detected),
        },
    )

    draft = propose(ctx, classification)
    if draft.action_type == ActionType.APPEAL and not draft.appeal_letter:
        letter, letter_flags = draft_appeal(ctx, classification, draft, guard_call)
        draft.appeal_letter = letter
        draft.flags = list(dict.fromkeys([*draft.flags, *letter_flags]))

    proposal = _persist_proposal(session, work_item, draft, version=1)
    work_item.flags = _work_item_flags(ctx, classification, draft)
    work_item.deadline = draft.deadline
    work_item.deadline_kind = str(draft.deadline_kind) if draft.deadline_kind else None
    work_item.priority_score = priority_score(draft, ctx)
    work_item.status = WorkItemStatus.PROPOSED
    session.flush()

    audit.record(
        session,
        actor_type=ActorType.AGENT,
        actor_id=actor_id,
        action="proposal_created",
        entity_type="proposal",
        entity_id=proposal.id,
        claim_id=rc.claim_id,
        details={
            "work_item_id": work_item.id,
            "action_type": str(draft.action_type),
            "coding_change": bool(draft.coding_change),
            "requires_note_excerpt": bool(draft.requires_note_excerpt),
            "citations": len(draft.citations),
            "flags": work_item.flags,
            "deadline": draft.deadline.isoformat() if draft.deadline else None,
        },
    )
    return work_item


def _work_item_flags(
    ctx: DenialContext, classification: ClassificationResult, draft: ProposalDraft
) -> list[str]:
    """Flags describing a work item's current proposal and context.

    ``injection_flagged`` when a payer document or a note excerpt carried planted instructions
    (found by the scanner on import or re-scan, or reported by the model), then the draft's own
    flags, then ``coding_change`` for any code or modifier edit. Recomputed on every (re)generation
    so the flags never describe a superseded proposal.
    """
    flags: list[str] = []
    flagged_notes = any(note.flagged_injection for note in ctx.notes)
    if classification.injection_detected or ctx.has_flagged_documents or flagged_notes:
        flags.append(FLAG_INJECTION)
    flags.extend(draft.flags)
    if draft.coding_change and FLAG_CODING_CHANGE not in flags:
        flags.append(FLAG_CODING_CHANGE)
    return list(dict.fromkeys(flags))


def _persist_proposal(
    session: Session, work_item: WorkItem, draft: ProposalDraft, *, version: int
) -> Proposal:
    for p in work_item.proposals:
        p.is_current = False
    proposal = Proposal(
        version=version,
        is_current=True,
        action_type=str(draft.action_type),
        summary=draft.summary,
        rationale=draft.rationale,
        citations=[c.model_dump(mode="json") for c in draft.citations],
        corrected_claim=draft.corrected_claim.model_dump(mode="json") if draft.corrected_claim else None,
        appeal_letter=draft.appeal_letter,
        clinic_question=draft.clinic_question,
        coding_change=draft.coding_change,
        requires_note_excerpt=draft.requires_note_excerpt,
        confidence=draft.confidence,
        prompt_version=draft.prompt_version,
        model_id=draft.model_id,
        generated_by=draft.generated_by,
    )
    # Append through the relationship (not session.add with a raw work_item_id) so the already
    # loaded `work_item.proposals` collection sees the new version in this session: otherwise
    # `current_proposal` is None until a reload and `regenerate_proposal` would reuse version 1.
    work_item.proposals.append(proposal)
    session.flush()
    return proposal


def regenerate_proposal(
    session: Session,
    work_item: WorkItem,
    *,
    llm_client: LLMClient | None = None,
    today: date | None = None,
    actor_id: str = PIPELINE_ACTOR,
) -> Proposal:
    """Re-run classification and drafting for an existing work item, adding a new proposal version.

    The work item's category, flags, deadline and priority follow the new proposal, so a payer
    letter attached after the first proposal is re-scanned and flagged here.
    """
    ctx = build_context(session, work_item.remittance_claim, today=today)
    if ctx is None:
        raise ValueError("work item has no matched claim")
    client = _resolve_client(llm_client)
    registry = IdentifierRegistry.from_context(ctx)
    guard_call = (
        make_guard_call(session, client, registry, work_item_id=work_item.id, purpose="regenerate")
        if client is not None
        else None
    )
    classification = classify(ctx, guard_call)
    draft = propose(ctx, classification)
    if draft.action_type == ActionType.APPEAL and not draft.appeal_letter:
        letter, letter_flags = draft_appeal(ctx, classification, draft, guard_call)
        draft.appeal_letter = letter
        draft.flags = list(dict.fromkeys([*draft.flags, *letter_flags]))
    version = (max((p.version for p in work_item.proposals), default=0)) + 1
    proposal = _persist_proposal(session, work_item, draft, version=version)
    work_item.flags = _work_item_flags(ctx, classification, draft)
    work_item.category = str(classification.category)
    work_item.category_confidence = classification.confidence
    work_item.classification_source = str(classification.source)
    work_item.classification_rationale = classification.rationale
    work_item.deadline = draft.deadline
    work_item.deadline_kind = str(draft.deadline_kind) if draft.deadline_kind else None
    work_item.priority_score = priority_score(draft, ctx)
    session.flush()
    audit.record(
        session,
        actor_type=ActorType.AGENT,
        actor_id=actor_id,
        action="proposal_regenerated",
        entity_type="proposal",
        entity_id=proposal.id,
        claim_id=work_item.claim_id,
        details={
            "work_item_id": work_item.id,
            "version": version,
            "action_type": str(draft.action_type),
            "category": str(classification.category),
            "coding_change": bool(draft.coding_change),
            "flags": work_item.flags,
        },
    )
    return proposal


def _record_failure(
    session: Session, rc: RemittanceClaim, exc: Exception, *, actor_id: str
) -> PipelineFailure:
    """Audit one skipped claim: ids and the exception class name only (a message may quote data)."""
    failure = PipelineFailure(remittance_claim_id=rc.id, claim_id=rc.claim_id, error=type(exc).__name__)
    log.warning(
        "pipeline failed for remittance claim %s (%s); rolled back to its savepoint, continuing",
        rc.id,
        failure.error,
    )
    log.debug(
        "pipeline failure traceback (frames only):\n%s", "".join(traceback.format_tb(exc.__traceback__))
    )
    audit.record(
        session,
        actor_type=ActorType.AGENT,
        actor_id=actor_id,
        action="pipeline_failed",
        entity_type="remittance_claim",
        entity_id=rc.id,
        claim_id=rc.claim_id,
        details={"remittance_claim_id": rc.id, "error": failure.error},
    )
    return failure


def process_pending(
    session: Session,
    *,
    llm_client: LLMClient | None = None,
    today: date | None = None,
    limit: int | None = None,
    actor_id: str = PIPELINE_ACTOR,
) -> PipelineRun:
    """Create work items for every denied, matched remittance claim that has none yet.

    Each claim runs inside ``session.begin_nested()`` (a SAVEPOINT): when it raises, its rows
    (work item, proposal, model-call records) are rolled back to the savepoint, a ``pipeline_failed``
    audit event names the remittance claim and the exception class, and the loop goes on. The
    result lists the created work items and, under ``failed``, the claims to retry on the next run.
    Nothing is committed here; the caller commits.
    """
    stmt = (
        select(RemittanceClaim)
        .where(RemittanceClaim.is_denial.is_(True), RemittanceClaim.claim_id.is_not(None))
        .order_by(RemittanceClaim.id)
    )
    existing_ids = set(session.execute(select(WorkItem.remittance_claim_id)).scalars())
    pending = [rc for rc in session.execute(stmt).scalars().all() if rc.id not in existing_ids]
    run = PipelineRun()
    client = _resolve_client(llm_client)
    for rc in pending:
        try:
            with session.begin_nested():
                wi = process_remittance_claim(
                    session, rc, llm_client=client, today=today, actor_id=actor_id, resolve_client=False
                )
        except Exception as exc:  # one bad denial must not take the batch down with it
            run.failed.append(_record_failure(session, rc, exc, actor_id=actor_id))
            continue
        if wi is not None:
            run.append(wi)
            if limit is not None and len(run) >= limit:
                break
    session.flush()
    log.info("pipeline processed %d remittance claims (%d failed)", len(run), len(run.failed))
    return run


__all__ = [
    "PipelineFailure",
    "PipelineRun",
    "build_context",
    "process_pending",
    "process_remittance_claim",
    "regenerate_proposal",
]
