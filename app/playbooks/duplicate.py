"""Duplicate-claim playbook (CARC 18, RARC N522 / M86).

`no_action` unless a note sentence describes a distinct, second, separate or repeat service; then an
appeal that cites the note and adds modifier 76 (repeat by the same physician), 77 (repeat by another
physician) or 91 (repeat clinical laboratory test) as a coding change.
"""

from __future__ import annotations

from app.drafting.corrected_claim import build_corrected_claim
from app.enums import ActionType, DenialCategory
from app.playbooks.base import Playbook, code_is_numeric_in, dedupe_citations
from app.playbooks.modifier import load_modifier_rules
from app.schemas import (
    ClaimLineContext,
    ClassificationResult,
    CorrectedClaimChange,
    DenialContext,
    ProposalDraft,
)

LAB_CODE_RANGES = ((80000, 89999),)
REPEAT_MODIFIERS = frozenset({"76", "77", "91"})
FLAG_POSSIBLE_TRUE_DUPLICATE = "possible_true_duplicate"


class DuplicatePlaybook(Playbook):
    category = DenialCategory.DUPLICATE

    def target_line(self, ctx: DenialContext) -> ClaimLineContext | None:
        lines = self.reason_lines(ctx)
        if lines:
            return self.line_by_number(ctx, lines[0])
        return ctx.claim.lines[0] if ctx.claim.lines else None

    def repeat_modifier(self, ctx: DenialContext, line: ClaimLineContext) -> str:
        rules = load_modifier_rules()
        if code_is_numeric_in(line.procedure_code, LAB_CODE_RANGES):
            return "91"
        if self.find_supporting_notes(ctx, rules.get("other_physician_keywords", [])):
            return "77"
        return "76"

    def propose(self, ctx: DenialContext, classification: ClassificationResult) -> ProposalDraft:
        rules = load_modifier_rules()
        support = self.find_supporting_notes(ctx, rules.get("duplicate_keywords", []))
        if not support:
            return self.build(
                ctx,
                action_type=ActionType.NO_ACTION,
                summary="Duplicate denial stands; no note describes a distinct or repeat service",
                rationale=(
                    f"Denied for {self.reasons_summary(ctx)}. No note sentence on file mentions a distinct, second, "
                    "separate or repeat service, so the payer's record of the earlier claim stands. Attach a note "
                    "excerpt and regenerate if the service was in fact repeated."
                ),
                citations=self.cite_reasons(ctx),
                confidence=0.75,
                flags=[FLAG_POSSIBLE_TRUE_DUPLICATE],
            )
        line = self.target_line(ctx)
        citations = [*self.cite_reasons(ctx), *self.note_citations(ctx, support)]
        corrected = None
        coding_change = False
        summary = "Appeal the duplicate denial: the notes document a distinct or repeat service"
        if line is not None:
            modifier = self.repeat_modifier(ctx, line)
            citations.append(
                self.cite_claim_field(ctx, f"lines[{line.line_number}].modifiers", line.modifiers)
            )
            if not (REPEAT_MODIFIERS & {str(m).upper() for m in line.modifiers}):
                change = CorrectedClaimChange(
                    path=f"lines[{line.line_number}].modifiers",
                    before=list(line.modifiers),
                    after=[*line.modifiers, modifier],
                    reason=f"Append modifier {modifier} to line {line.line_number} ({line.procedure_code}): the notes document a repeat or distinct service",
                    coding_change=True,
                    citation_refs=[c.ref for c in citations],
                )
                corrected = build_corrected_claim(ctx, [change])
                coding_change = True
                summary += f" (modifier {modifier} on line {line.line_number})"
        rationale = (
            f"Denied for {self.reasons_summary(ctx)}. {len(support)} note sentence(s) describe a distinct or repeat "
            "service and are quoted in the appeal."
        )
        if coding_change:
            rationale += " The repeat modifier is a coding change; the quoted note excerpt must be confirmed before approval."
        return self.build(
            ctx,
            action_type=ActionType.APPEAL,
            summary=summary,
            rationale=rationale,
            citations=dedupe_citations(citations),
            corrected_claim=corrected,
            coding_change=coding_change,
            confidence=0.65,
        )


__all__ = ["DuplicatePlaybook", "FLAG_POSSIBLE_TRUE_DUPLICATE"]
