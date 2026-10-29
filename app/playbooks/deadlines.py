"""Filing and appeal deadlines for a denied claim (docs/SPEC.md section 9).

Every proposal carries a deadline so the review queue can sort by it:

- corrected-claim deadline = min(service_date_from + payer.timely_filing_days,
  remittance.payment_date + payer.corrected_claim_days) over the dates that are known. When neither
  date is known the fall-back is remittance.claim_received_date + corrected_claim_days, then
  today + corrected_claim_days.
- appeal deadline = remittance.payment_date + payer.appeal_days, falling back to
  remittance.claim_received_date, then today.
- clinic_question and no_action carry the corrected-claim deadline so the reviewer can see how much
  of the resubmission window is left.
"""

from __future__ import annotations

from datetime import date, timedelta

from app.enums import ActionType, DeadlineKind
from app.schemas import DenialContext


def first_service_date(ctx: DenialContext) -> date | None:
    """The claim's first date of service: `service_date_from`, else the earliest line service date."""
    if ctx.claim.service_date_from is not None:
        return ctx.claim.service_date_from
    dates = [line.service_date for line in ctx.claim.lines if line.service_date is not None]
    return min(dates) if dates else None


def corrected_claim_deadline(ctx: DenialContext) -> date:
    """Last day a replacement claim can be filed, from the dates that are known."""
    candidates: list[date] = []
    dos = first_service_date(ctx)
    if dos is not None:
        candidates.append(dos + timedelta(days=ctx.payer.timely_filing_days))
    if ctx.remittance.payment_date is not None:
        candidates.append(ctx.remittance.payment_date + timedelta(days=ctx.payer.corrected_claim_days))
    if candidates:
        return min(candidates)
    base = ctx.remittance.claim_received_date or ctx.today
    return base + timedelta(days=ctx.payer.corrected_claim_days)


def appeal_deadline(ctx: DenialContext) -> date:
    """Last day a formal appeal can be filed: payer.appeal_days after the remittance."""
    base = ctx.remittance.payment_date or ctx.remittance.claim_received_date or ctx.today
    return base + timedelta(days=ctx.payer.appeal_days)


def deadline_for(action_type: ActionType | str, ctx: DenialContext) -> tuple[date, DeadlineKind]:
    """Deadline and its kind for a proposal of the given action type."""
    if ActionType(action_type) == ActionType.APPEAL:
        return appeal_deadline(ctx), DeadlineKind.APPEAL
    return corrected_claim_deadline(ctx), DeadlineKind.CORRECTED_CLAIM


def days_until(deadline: date | None, today: date) -> int | None:
    """Whole days from `today` to `deadline`; negative when it has passed; None without a deadline."""
    if deadline is None:
        return None
    return (deadline - today).days


__all__ = ["appeal_deadline", "corrected_claim_deadline", "days_until", "deadline_for", "first_service_date"]
