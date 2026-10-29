"""Fix proposals per denial category (milestone M6, docs/SPEC.md section 9).

`propose(ctx, classification)` dispatches on the classification's category through the registry and
guarantees every draft has non-empty citations, a deadline with its kind, and the coding-change
invariants. `priority_score(draft, ctx)` turns dollars and the deadline into one number for the queue.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from app.enums import ActionType
from app.playbooks.base import (
    FLAG_CODING_CHANGE,
    FLAG_FLAGGED_NOTES_SKIPPED,
    Playbook,
    citation_resolves,
    has_flagged_notes,
)
from app.playbooks.deadlines import appeal_deadline, corrected_claim_deadline, days_until, deadline_for
from app.playbooks.registry import all_playbooks, get_playbook, register
from app.schemas import ClassificationResult, DenialContext, ProposalDraft

log = logging.getLogger(__name__)

# Weight of the action type in the priority score: questions and no-action items rank below fixes.
ACTION_WEIGHTS: dict[ActionType, float] = {
    ActionType.CORRECTED_CLAIM: 1.0,
    ActionType.APPEAL: 1.0,
    ActionType.CLINIC_QUESTION: 0.8,
    ActionType.NO_ACTION: 0.25,
}

URGENCY_HORIZON_DAYS = 90
URGENCY_SLOPE_DAYS = 30
URGENCY_PAST_DEADLINE = 0.5


def propose(ctx: DenialContext, classification: ClassificationResult) -> ProposalDraft:
    """Run the playbook for the classified category and enforce the proposal invariants."""
    playbook = get_playbook(classification.category)
    draft = playbook.propose(ctx, classification)
    return finalize(draft, ctx, playbook)


def finalize(draft: ProposalDraft, ctx: DenialContext, playbook: Playbook | None = None) -> ProposalDraft:
    """Invariants every proposal must satisfy, applied even to drafts built outside `Playbook.build`."""
    playbook = playbook or Playbook()
    if not draft.citations:
        draft.citations = playbook.cite_reasons(ctx) or [
            playbook.cite_remittance_field(ctx, "claim_status_code")
        ]
    if draft.deadline is None or draft.deadline_kind is None:
        draft.deadline, draft.deadline_kind = deadline_for(draft.action_type, ctx)
    if draft.coding_change:
        draft.requires_note_excerpt = True
        if FLAG_CODING_CHANGE not in draft.flags:
            draft.flags = [FLAG_CODING_CHANGE, *draft.flags]
    if has_flagged_notes(ctx) and FLAG_FLAGGED_NOTES_SKIPPED not in draft.flags:
        draft.flags = [*draft.flags, FLAG_FLAGGED_NOTES_SKIPPED]
    # Counts and ids only: the deadline is logged as days from today, never as a date.
    log.info(
        "playbook %s proposed %s for claim_id=%s (citations=%d, coding_change=%s, days_to_deadline=%s)",
        playbook.category,
        draft.action_type,
        ctx.claim.claim_id,
        len(draft.citations),
        draft.coding_change,
        days_until(draft.deadline, ctx.today),
    )
    return draft


def urgency_weight(days: int | None) -> float:
    """1.0 with 90 or more days left, rising by 1.0 per 30 days to 4.0 on the deadline day;
    0.5 once the deadline has passed; 1.0 without a deadline."""
    if days is None:
        return 1.0
    if days < 0:
        return URGENCY_PAST_DEADLINE
    remaining = min(days, URGENCY_HORIZON_DAYS)
    return 1.0 + (URGENCY_HORIZON_DAYS - remaining) / URGENCY_SLOPE_DAYS


def dollar_value(ctx: DenialContext) -> Decimal:
    """Denied dollars, falling back to the sum of the reasons, then the claim's total charge."""
    amount = Decimal(ctx.remittance.denied_amount or 0)
    if amount <= 0:
        amount = sum((Decimal(r.amount or 0) for r in ctx.reasons), Decimal("0"))
    if amount <= 0:
        amount = Decimal(ctx.claim.total_charge or 0)
    return max(amount, Decimal("0"))


def priority_score(draft: ProposalDraft, ctx: DenialContext) -> float:
    """priority = dollar_value x urgency_weight(days until the deadline) x action_weight.

    Higher means more urgent: a $500 corrected claim due in 10 days scores 500 x 3.67 x 1.0 = 1833,
    the same claim with 90 days left scores 500, and a no-action item scores a quarter of that.
    """
    days = days_until(draft.deadline, ctx.today)
    weight = ACTION_WEIGHTS.get(ActionType(draft.action_type), 1.0)
    return round(float(dollar_value(ctx)) * urgency_weight(days) * weight, 4)


__all__ = [
    "ACTION_WEIGHTS",
    "Playbook",
    "all_playbooks",
    "appeal_deadline",
    "citation_resolves",
    "corrected_claim_deadline",
    "days_until",
    "deadline_for",
    "dollar_value",
    "finalize",
    "get_playbook",
    "priority_score",
    "propose",
    "register",
    "urgency_weight",
]
