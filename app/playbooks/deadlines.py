"""Filing and appeal deadlines for a denied claim (docs/SPEC.md section 9).

Every proposal carries a deadline so the review queue can sort by it. Both windows count from the
denial itself, not from the date of service:

- corrected-claim deadline = remittance.payment_date + payer.corrected_claim_days, falling back to
  remittance.claim_received_date, then today. The payer accepts a replacement of a timely filed
  claim within that window however long after the service the denial arrived, so a denial
  adjudicated months after the visit is not "overdue" on the day it is received.
- appeal deadline = remittance.payment_date + payer.appeal_days, with the same fall-backs.
- clinic_question and no_action carry the corrected-claim deadline so the reviewer can see how much
  of the resubmission window is left.

The payer's timely-filing window, counted from the first date of service (`first_service_date`),
is not a deadline: it is the test the timely_filing playbook applies to the original submission
date to decide between an appeal with proof of timely filing and `timely_filing_expired`.
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


def denial_date(ctx: DenialContext) -> date:
    """The day the denial is counted from: the remittance payment date, else the date the payer
    received the claim, else today."""
    return ctx.remittance.payment_date or ctx.remittance.claim_received_date or ctx.today


def corrected_claim_deadline(ctx: DenialContext) -> date:
    """Last day a replacement claim can be filed: payer.corrected_claim_days after the denial."""
    return denial_date(ctx) + timedelta(days=ctx.payer.corrected_claim_days)


def appeal_deadline(ctx: DenialContext) -> date:
    """Last day a formal appeal can be filed: payer.appeal_days after the denial."""
    return denial_date(ctx) + timedelta(days=ctx.payer.appeal_days)


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


__all__ = [
    "appeal_deadline",
    "corrected_claim_deadline",
    "days_until",
    "deadline_for",
    "denial_date",
    "first_service_date",
]
