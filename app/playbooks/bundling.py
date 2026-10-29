"""Bundling playbook (CARC 97 / 234 / 236 / 231 / 59, RARC M15 / N19 / M144 / N390).

Appeal with modifier 59 (XS when the notes describe a separate site or structure) on the bundled
column-2 line when a note sentence supports a distinct service; otherwise `no_action` flagged
`contractual_bundling`.
"""

from __future__ import annotations

from app.drafting.corrected_claim import build_corrected_claim
from app.enums import ActionType, DenialCategory
from app.playbooks.base import FLAG_CONTRACTUAL_BUNDLING, Playbook, dedupe_citations
from app.playbooks.modifier import load_modifier_rules
from app.schemas import (
    ClaimLineContext,
    ClassificationResult,
    CorrectedClaimChange,
    DenialContext,
    ProposalDraft,
)


class BundlingPlaybook(Playbook):
    category = DenialCategory.BUNDLING

    def target_line(self, ctx: DenialContext) -> ClaimLineContext | None:
        lines = self.reason_lines(ctx)
        if lines:
            return self.line_by_number(ctx, lines[0])
        rules = load_modifier_rules()
        pairs = rules["situations"].get("bundled_pair", {}).get("pairs", [])
        codes = {line.procedure_code for line in ctx.claim.lines}
        for column1, column2 in pairs:
            if str(column1) in codes and str(column2) in codes:
                return next(line for line in ctx.claim.lines if line.procedure_code == str(column2))
        return ctx.claim.lines[-1] if ctx.claim.lines else None

    def propose(self, ctx: DenialContext, classification: ClassificationResult) -> ProposalDraft:
        rules = load_modifier_rules()
        pair_rule = rules["situations"].get("bundled_pair", {})
        support = self.find_supporting_notes(ctx, pair_rule.get("support_keywords", []))
        site_support = self.find_supporting_notes(ctx, pair_rule.get("alternate_keywords", []))
        if not support and not site_support:
            return self.build(
                ctx,
                action_type=ActionType.NO_ACTION,
                summary="Bundling denial stands; nothing in the notes supports a distinct service",
                rationale=(
                    f"Denied for {self.reasons_summary(ctx)}. No note sentence describes a separate site, lesion or "
                    "procedure, so the bundling edit is treated as contractual. Attach a note excerpt and regenerate "
                    "if the services were distinct."
                ),
                citations=self.cite_reasons(ctx),
                confidence=0.7,
                flags=[FLAG_CONTRACTUAL_BUNDLING],
            )
        all_support = support + [s for s in site_support if s not in support]
        modifier = (
            pair_rule.get("alternate_modifier", "XS") if site_support else pair_rule.get("modifier", "59")
        )
        distinct = {str(m) for m in rules.get("distinct_service_modifiers", [])}
        line = self.target_line(ctx)
        citations = [*self.cite_reasons(ctx), *self.note_citations(ctx, all_support)]
        corrected = None
        coding_change = False
        summary = "Appeal the bundling denial: the notes document a distinct service"
        if line is not None:
            citations.append(
                self.cite_claim_field(ctx, f"lines[{line.line_number}].procedure_code", line.procedure_code)
            )
            citations.append(
                self.cite_claim_field(ctx, f"lines[{line.line_number}].modifiers", line.modifiers)
            )
            if not (distinct & {str(m).upper() for m in line.modifiers}):
                change = CorrectedClaimChange(
                    path=f"lines[{line.line_number}].modifiers",
                    before=list(line.modifiers),
                    after=[*line.modifiers, modifier],
                    reason=f"Append modifier {modifier} to line {line.line_number} ({line.procedure_code}): distinct procedural service documented in the notes",
                    coding_change=True,
                    citation_refs=[c.ref for c in citations],
                )
                corrected = build_corrected_claim(ctx, [change])
                coding_change = True
                summary += f" (modifier {modifier} on line {line.line_number})"
        rationale = (
            f"Denied for {self.reasons_summary(ctx)}. {len(all_support)} note sentence(s) describe a separate or "
            f"distinct service{' at a different site' if site_support else ''} and are quoted in the appeal."
        )
        if coding_change:
            rationale += f" Modifier {modifier} is a coding change; the quoted note excerpt must be confirmed before approval."
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


__all__ = ["BundlingPlaybook"]
