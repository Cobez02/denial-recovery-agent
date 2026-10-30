"""Contractual and patient-responsibility adjustments (`no_action`) and the `other` category
(`clinic_question` listing the raw reasons, or a specific question when `OTHER_QUESTIONS` knows the
reason: the benefit maximum asks for the payer's visit accumulator to be checked against the
clinic's visit log, quoting the note that counts the visits)."""

from __future__ import annotations

from dataclasses import dataclass

from app.enums import ActionType, DenialCategory
from app.playbooks.base import Playbook, dedupe_citations, rarc_text, reason_label, reason_text
from app.schemas import ClassificationResult, DenialContext, ProposalDraft


@dataclass(frozen=True)
class OtherQuestion:
    """A specific clinic question for a reason the `other` category carries (data, not a rule)."""

    carcs: frozenset[str]
    summary: str
    question: str
    note_keywords: tuple[str, ...]
    flag: str


OTHER_QUESTIONS: tuple[OtherQuestion, ...] = (
    OtherQuestion(
        carcs=frozenset({"119"}),
        summary="Verify the plan's visit accumulator against the clinic's visit log",
        question=(
            "The plan says the benefit maximum for the period was reached. Please compare the plan's visit "
            "accumulator for this member with the clinic's own visit log for the benefit year (visits from other "
            "disciplines are sometimes counted against a combined benefit). If the plan's count is wrong, send the "
            "dated visit log for an appeal; if it is right, confirm whether the patient signed a benefits "
            "acknowledgement before the visit, because only then may the balance be billed to the patient."
        ),
        note_keywords=(
            "visit log",
            "visits per calendar year",
            "accumulator",
            "benefit allows",
            "visit count",
        ),
        flag="benefit_maximum",
    ),
)


def other_question_for(ctx: DenialContext) -> OtherQuestion | None:
    for spec in OTHER_QUESTIONS:
        if any(reason.carc in spec.carcs for reason in ctx.reasons):
            return spec
    return None


class ContractualPlaybook(Playbook):
    category = DenialCategory.CONTRACTUAL

    def propose(self, ctx: DenialContext, classification: ClassificationResult) -> ProposalDraft:
        return self.build(
            ctx,
            action_type=ActionType.NO_ACTION,
            summary="Contractual adjustment; no action",
            rationale=(
                f"Adjusted for {self.reasons_summary(ctx)}. Contractual adjustments reflect the payer's fee schedule or "
                "agreement and are not appealable denials."
            ),
            citations=self.cite_reasons(ctx),
            confidence=0.9,
            flags=["contractual"],
        )


class PatientResponsibilityPlaybook(Playbook):
    category = DenialCategory.PATIENT_RESPONSIBILITY

    def propose(self, ctx: DenialContext, classification: ClassificationResult) -> ProposalDraft:
        return self.build(
            ctx,
            action_type=ActionType.NO_ACTION,
            summary="Patient responsibility (deductible, copay, coinsurance or plan exclusion); no payer action",
            rationale=(
                f"Adjusted for {self.reasons_summary(ctx)}. The amount is the patient's responsibility under the plan; "
                "bill the patient per clinic policy. Nothing to resubmit or appeal."
            ),
            citations=[*self.cite_reasons(ctx), self.cite_remittance_field(ctx, "patient_responsibility")],
            confidence=0.85,
            flags=["patient_responsibility"],
        )


class OtherPlaybook(Playbook):
    category = DenialCategory.OTHER

    def propose(self, ctx: DenialContext, classification: ClassificationResult) -> ProposalDraft:
        listed = []
        for reason in ctx.reasons:
            piece = f'{reason_label(reason)}: "{reason_text(reason)}"'
            if reason.rarcs:
                piece += " with RARC " + "; ".join(f'{rc}: "{rarc_text(reason, rc)}"' for rc in reason.rarcs)
            if reason.amount:
                piece += f" (${reason.amount:.2f})"
            listed.append(piece)
        raw = "; ".join(listed) if listed else "no reason codes on the remittance"
        spec = other_question_for(ctx)
        if spec is not None:
            support = self.find_supporting_notes(ctx, spec.note_keywords, limit=2)
            question = (
                f"Claim {ctx.claim.claim_control_number} (DOS {self.dos_label(ctx)}) was denied for {raw}. "
                + spec.question
            )
            if support:
                question += " The note on file states: " + " ".join(
                    f'"{sentence}"' for _, sentence in support
                )
            return self.build(
                ctx,
                action_type=ActionType.CLINIC_QUESTION,
                summary=spec.summary,
                rationale=(
                    f"Classified as other ({classification.rationale or 'no rule matched'}). Raw reasons: {raw}. "
                    "The reason names a benefit limit the clinic's own records can confirm or refute"
                    + (", and the note that counts the visits is quoted." if support else ".")
                ),
                citations=dedupe_citations([*self.cite_reasons(ctx), *self.note_citations(ctx, support)]),
                clinic_question=question,
                confidence=0.6,
                flags=[spec.flag],
            )
        question = (
            f"Claim {ctx.claim.claim_control_number} (DOS {self.dos_label(ctx)}) was denied for reasons the rules do not "
            f"map to a fix: {raw}. Please review the remittance and payer portal and tell us how this denial should be "
            "worked (corrected claim, appeal, or write-off) so the rule set can be extended."
        )
        return self.build(
            ctx,
            action_type=ActionType.CLINIC_QUESTION,
            summary="Unmapped denial reason; ask the clinic how to work it",
            rationale=(
                f"Classified as other ({classification.rationale or 'no rule matched'}; confidence "
                f"{classification.confidence:.2f}). Raw reasons: {raw}."
            ),
            citations=self.cite_reasons(ctx),
            clinic_question=question,
            confidence=min(0.4, max(0.1, float(classification.confidence))),
            flags=["unmapped_reason"],
        )


__all__ = [
    "OTHER_QUESTIONS",
    "ContractualPlaybook",
    "OtherPlaybook",
    "OtherQuestion",
    "PatientResponsibilityPlaybook",
    "other_question_for",
]
