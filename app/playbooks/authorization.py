"""Authorization playbook (CARC 197 / 198 / 15 / 62 / 39; CARC 16 with RARC M62 / N54).

- `corrected_claim` resending the prior authorization number (REF*G1) when the claim record has one;
- otherwise `appeal` for retro-authorization with note citations showing why the service could not wait;
- `clinic_question` when no note excerpt is on file.
"""

from __future__ import annotations

from app.drafting.corrected_claim import build_corrected_claim
from app.enums import ActionType, DenialCategory
from app.playbooks.base import (
    FLAG_NO_NOTES,
    Playbook,
    dedupe_citations,
    latest_note,
    split_sentences,
    usable_notes,
)
from app.playbooks.medical_necessity import MedicalNecessityPlaybook
from app.schemas import ClassificationResult, CorrectedClaimChange, DenialContext, ProposalDraft

FLAG_RETRO_AUTHORIZATION = "retro_authorization"
FLAG_WEAK_NOTE_SUPPORT = "weak_note_support"

RETRO_AUTH_KEYWORDS: tuple[str, ...] = (
    "urgent",
    "urgently",
    "emergent",
    "emergency",
    "same day",
    "same-day",
    "could not wait",
    "cannot wait",
    "immediate",
    "immediately",
    "medically necessary",
    "medical necessity",
    "necessary",
    "required",
    "worsening",
    "pain",
    "painful",
    "bleeding",
    "infection",
    "infected",
    "suspicious",
    "rule out",
    "malignant",
    "malignancy",
    "carcinoma",
    "melanoma",
    "failed",
    "symptomatic",
)


class AuthorizationPlaybook(Playbook):
    category = DenialCategory.AUTHORIZATION

    def propose(self, ctx: DenialContext, classification: ClassificationResult) -> ProposalDraft:
        auth = ctx.claim.prior_auth_number
        if auth:
            citations = [*self.cite_reasons(ctx), self.cite_claim_field(ctx, "prior_auth_number", auth)]
            change = CorrectedClaimChange(
                path="prior_auth_number",
                before=auth,
                after=auth,
                reason="Resend the prior authorization number (REF*G1) on a replacement claim; the payer processed the claim without it",
                coding_change=False,
                citation_refs=[c.ref for c in citations],
            )
            return self.build(
                ctx,
                action_type=ActionType.CORRECTED_CLAIM,
                summary="Corrected claim resending the prior authorization number",
                rationale=(
                    f"Denied for {self.reasons_summary(ctx)}. The claim record holds a prior authorization number, so a "
                    "replacement claim (frequency 7) resends it in REF*G1. No code or modifier changes."
                ),
                citations=citations,
                corrected_claim=build_corrected_claim(ctx, [change]),
                confidence=0.75,
            )

        if not usable_notes(ctx):  # a note flagged for planted instructions counts as absent
            question = (
                f"Claim {ctx.claim.claim_control_number} (DOS {self.dos_label(ctx)}) was denied for missing authorization "
                f"({self.reasons_summary(ctx)}) and the record has no authorization number or note excerpt. Please confirm "
                "whether an authorization was obtained (and its number) or supply the note sentences that explain why the "
                "service could not wait, so a retro-authorization appeal can be prepared."
            )
            return self.build(
                ctx,
                action_type=ActionType.CLINIC_QUESTION,
                summary="Ask the clinic for the authorization number or notes for a retro-authorization request",
                rationale=f"Denied for {self.reasons_summary(ctx)}. No authorization number and no note excerpt on file.",
                citations=[*self.cite_reasons(ctx), self.cite_claim_field(ctx, "prior_auth_number", None)],
                clinic_question=question,
                confidence=0.5,
                flags=[FLAG_NO_NOTES],
            )

        keywords = list(RETRO_AUTH_KEYWORDS) + MedicalNecessityPlaybook().keywords_for(ctx)
        support = self.find_supporting_notes(ctx, keywords)
        flags = [FLAG_RETRO_AUTHORIZATION]
        confidence = 0.65
        if not support:
            latest = latest_note(ctx)
            assert latest is not None  # usable_notes(ctx) is non-empty here
            support = [(latest, sentence) for sentence in split_sentences(latest.text)[:2]]
            flags.append(FLAG_WEAK_NOTE_SUPPORT)
            confidence = 0.45
        citations = [
            *self.cite_reasons(ctx),
            self.cite_claim_field(ctx, "prior_auth_number", None),
            *self.note_citations(ctx, support),
        ]
        rationale = (
            f"Denied for {self.reasons_summary(ctx)}. No authorization number is on the claim record. "
        )
        if FLAG_WEAK_NOTE_SUPPORT in flags:
            rationale += "The notes do not state why the service could not wait; the opening of the latest note is quoted and the reviewer should replace it with the relevant excerpt."
        else:
            rationale += f"{len(support)} note sentence(s) document why the service was necessary and are quoted in the retro-authorization request."
        return self.build(
            ctx,
            action_type=ActionType.APPEAL,
            summary="Appeal for retro-authorization with quoted clinical documentation",
            rationale=rationale,
            citations=dedupe_citations(citations),
            confidence=confidence,
            flags=flags,
        )


__all__ = ["AuthorizationPlaybook", "FLAG_RETRO_AUTHORIZATION", "RETRO_AUTH_KEYWORDS"]
