"""Contractual and patient-responsibility adjustments (`no_action`) and the `other` category
(`clinic_question` listing the raw reasons)."""

from __future__ import annotations

from app.enums import ActionType, DenialCategory
from app.playbooks.base import Playbook, rarc_text, reason_label, reason_text
from app.schemas import ClassificationResult, DenialContext, ProposalDraft


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


__all__ = ["ContractualPlaybook", "OtherPlaybook", "PatientResponsibilityPlaybook"]
