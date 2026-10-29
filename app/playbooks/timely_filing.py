"""Timely-filing playbook (CARC 29).

Appeal with proof of timely submission when the claim's `original_submission_date` falls within the
payer's timely-filing window measured from the first date of service; otherwise `no_action` with reason
`timely_filing_expired` so the item lands in the missed-money review. Without a recorded submission
date the clinic is asked for the clearinghouse acceptance report.
"""

from __future__ import annotations

from datetime import timedelta

from app.enums import ActionType, DenialCategory
from app.playbooks.base import FLAG_MISSED_MONEY_REVIEW, Playbook
from app.playbooks.deadlines import first_service_date
from app.schemas import ClassificationResult, DenialContext, ProposalDraft

REASON_EXPIRED = "timely_filing_expired"


class TimelyFilingPlaybook(Playbook):
    category = DenialCategory.TIMELY_FILING

    def propose(self, ctx: DenialContext, classification: ClassificationResult) -> ProposalDraft:
        submitted = ctx.claim.original_submission_date
        dos = first_service_date(ctx)
        window = ctx.payer.timely_filing_days
        citations = [
            *self.cite_reasons(ctx),
            self.cite_claim_field(ctx, "original_submission_date", submitted),
            self.cite_claim_field(ctx, "service_date_from", dos),
            self.cite_payer_window(ctx, "timely_filing_days"),
        ]
        if submitted is None or dos is None:
            question = (
                f"Claim {ctx.claim.claim_control_number} was denied for timely filing but the record has no "
                f"{'original submission date' if submitted is None else 'date of service'}. Please supply the "
                "original submission date and the clearinghouse acceptance report (or payer acknowledgement) so "
                "the appeal can prove timely submission."
            )
            return self.build(
                ctx,
                action_type=ActionType.CLINIC_QUESTION,
                summary="Ask the clinic for proof of the original submission date",
                rationale=f"Denied for {self.reasons_summary(ctx)}. Timeliness cannot be checked without the original submission date and date of service.",
                citations=citations,
                clinic_question=question,
                confidence=0.5,
            )
        limit = dos + timedelta(days=window)
        days_after = (submitted - dos).days
        if submitted <= limit:
            return self.build(
                ctx,
                action_type=ActionType.APPEAL,
                summary=f"Appeal the timely-filing denial with proof of submission on {submitted.isoformat()}",
                rationale=(
                    f"Denied for {self.reasons_summary(ctx)}. The claim was first submitted on {submitted.isoformat()}, "
                    f"{days_after} days after the first date of service {dos.isoformat()}, inside the payer's "
                    f"{window}-day timely-filing window (deadline {limit.isoformat()}). Attach the clearinghouse "
                    "acceptance report as proof of timely submission."
                ),
                citations=citations,
                confidence=0.85,
                flags=["proof_of_timely_filing_required"],
            )
        return self.build(
            ctx,
            action_type=ActionType.NO_ACTION,
            summary="Timely-filing window missed; no recoverable action (missed-money review)",
            rationale=(
                f"Denied for {self.reasons_summary(ctx)}. Reason: {REASON_EXPIRED}. The claim was first submitted on "
                f"{submitted.isoformat()}, {days_after} days after the first date of service {dos.isoformat()}, "
                f"after the payer's {window}-day window closed on {limit.isoformat()}. Review for a process fix; "
                "an appeal would need proof of an earlier submission that the record does not show."
            ),
            citations=citations,
            confidence=0.9,
            flags=[REASON_EXPIRED, FLAG_MISSED_MONEY_REVIEW],
        )


__all__ = ["REASON_EXPIRED", "TimelyFilingPlaybook"]
