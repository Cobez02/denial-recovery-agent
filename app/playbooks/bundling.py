"""Bundling playbook (CARC 97 / 234 / 236 / 231 / 59, RARC M15 / N19 / M144 / N390).

- An E/M bundled into a same-day procedure with a global period (CMT included) is never given 59 or
  an X modifier (NCCI forbids them on E/M codes): a corrected claim adds modifier 25 when the notes
  document a significant, separately identifiable service and the line lacks it; an appeal quotes
  that documentation when 25 was already billed; otherwise `no_action` (the pre-manipulation
  assessment is part of the procedure).
- Any other bundled line: when a note sentence supports a distinct service, a corrected claim adds
  59 (XS when the notes describe a separate site or region) to the bundled column-2 line, a coding
  change; when the line already carries 59 or an X modifier, an appeal with the quoted note.
- Nothing in the notes supports a distinct service: `no_action` flagged `contractual_bundling`.
"""

from __future__ import annotations

from app.drafting.corrected_claim import build_corrected_claim
from app.enums import ActionType, DenialCategory
from app.playbooks.base import FLAG_CONTRACTUAL_BUNDLING, Playbook, code_is_numeric_in, dedupe_citations
from app.playbooks.modifier import load_modifier_rules
from app.schemas import (
    ClaimLineContext,
    ClassificationResult,
    CorrectedClaimChange,
    DenialContext,
    ProposalDraft,
)

FLAG_EM_BUNDLED = "em_bundled_with_procedure"


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

    @staticmethod
    def is_em_with_procedure(ctx: DenialContext, line: ClaimLineContext) -> bool:
        """The denied line is an E/M billed on the same date as a procedure with a global period."""
        rules = load_modifier_rules()
        if not code_is_numeric_in(line.procedure_code, rules.get("em_code_ranges", [])):
            return False
        procedure_ranges = rules.get("procedure_code_ranges", [])
        return any(
            other is not line
            and code_is_numeric_in(other.procedure_code, procedure_ranges)
            and other.service_date == line.service_date
            for other in ctx.claim.lines
        )

    def propose(self, ctx: DenialContext, classification: ClassificationResult) -> ProposalDraft:
        line = self.target_line(ctx)
        if line is not None and self.is_em_with_procedure(ctx, line):
            return self.em_bundled(ctx, line)
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
                    f"Denied for {self.reasons_summary(ctx)}. No note sentence describes a separate region, site or "
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
        citations = [*self.cite_reasons(ctx), *self.note_citations(ctx, all_support)]
        where = " at a different site or region" if site_support else ""
        if line is not None:
            citations.append(
                self.cite_claim_field(ctx, f"lines[{line.line_number}].procedure_code", line.procedure_code)
            )
            citations.append(
                self.cite_claim_field(ctx, f"lines[{line.line_number}].modifiers", line.modifiers)
            )
        if line is not None and not (distinct & {str(m).upper() for m in line.modifiers}):
            # The claim left the distinct-service modifier off: a corrected claim adds it.
            change = CorrectedClaimChange(
                path=f"lines[{line.line_number}].modifiers",
                before=list(line.modifiers),
                after=[*line.modifiers, modifier],
                reason=f"Append modifier {modifier} to line {line.line_number} ({line.procedure_code}): distinct procedural service documented in the notes",
                coding_change=True,
                citation_refs=[c.ref for c in citations],
            )
            return self.build(
                ctx,
                action_type=ActionType.CORRECTED_CLAIM,
                summary=f"Corrected claim adding modifier {modifier} to line {line.line_number} ({line.procedure_code}): the notes document a distinct service",
                rationale=(
                    f"Denied for {self.reasons_summary(ctx)}. {len(all_support)} note sentence(s) describe a separate "
                    f"or distinct service{where} and are cited; the claim did not report it, so a corrected claim "
                    f"adds modifier {modifier}. A coding change: the quoted note excerpt must be confirmed before "
                    "approval."
                ),
                citations=dedupe_citations(citations),
                corrected_claim=build_corrected_claim(ctx, [change]),
                coding_change=True,
                confidence=0.65,
            )
        # The distinct-service modifier was already billed: appeal with the documentation.
        return self.build(
            ctx,
            action_type=ActionType.APPEAL,
            summary="Appeal the bundling denial: the notes document a distinct service",
            rationale=(
                f"Denied for {self.reasons_summary(ctx)}. The claim already reported the distinct service; "
                f"{len(all_support)} note sentence(s) describe a separate or distinct service{where} and are quoted "
                "in the appeal."
            ),
            citations=dedupe_citations(citations),
            confidence=0.6,
        )

    def em_bundled(self, ctx: DenialContext, line: ClaimLineContext) -> ProposalDraft:
        """An E/M bundled into a same-day procedure: modifier 25 with separate documentation, never 59/X."""
        rules = load_modifier_rules()
        em_rule = rules["situations"].get("em_same_day_procedure", {})
        modifier = str(em_rule.get("modifier", "25"))
        support = self.find_supporting_notes(ctx, em_rule.get("support_keywords", []))
        citations = [
            *self.cite_reasons(ctx),
            self.cite_claim_field(ctx, f"lines[{line.line_number}].procedure_code", line.procedure_code),
            self.cite_claim_field(ctx, f"lines[{line.line_number}].modifiers", line.modifiers),
            *self.note_citations(ctx, support),
        ]
        has_modifier = modifier in {str(m).strip().upper() for m in line.modifiers}
        if not support:
            return self.build(
                ctx,
                action_type=ActionType.NO_ACTION,
                summary=f"Bundling denial stands: nothing documents a separately identifiable E/M on line {line.line_number}",
                rationale=(
                    f"Denied for {self.reasons_summary(ctx)}. The E/M on line {line.line_number} "
                    f"({line.procedure_code}) was billed with a same-day procedure whose global period includes the "
                    "pre-procedure assessment (for CMT, the pre-manipulation assessment), and no note sentence "
                    "documents a significant, separately identifiable service. Modifiers 59 and X{EPSU} are never "
                    "used on an E/M; attach a note excerpt and regenerate if the visit was separately identifiable."
                ),
                citations=citations,
                confidence=0.7,
                flags=[FLAG_CONTRACTUAL_BUNDLING, FLAG_EM_BUNDLED],
            )
        if not has_modifier:
            change = CorrectedClaimChange(
                path=f"lines[{line.line_number}].modifiers",
                before=list(line.modifiers),
                after=[*line.modifiers, modifier],
                reason=f"Append modifier {modifier} to the E/M on line {line.line_number} ({line.procedure_code}): a significant, separately identifiable service is documented",
                coding_change=True,
                citation_refs=[c.ref for c in citations],
            )
            return self.build(
                ctx,
                action_type=ActionType.CORRECTED_CLAIM,
                summary=f"Corrected claim adding modifier {modifier} to the E/M on line {line.line_number}",
                rationale=(
                    f"Denied for {self.reasons_summary(ctx)}. {len(support)} note sentence(s) document a significant, "
                    f"separately identifiable E/M on the date of the procedure; modifier {modifier} reports it "
                    "(never 59 or an X modifier on an E/M). A coding change: the quoted note excerpt must be "
                    "confirmed before approval."
                ),
                citations=dedupe_citations(citations),
                corrected_claim=build_corrected_claim(ctx, [change]),
                coding_change=True,
                confidence=0.65,
                flags=[FLAG_EM_BUNDLED],
            )
        return self.build(
            ctx,
            action_type=ActionType.APPEAL,
            summary=f"Appeal the bundled E/M on line {line.line_number}: the notes document a separately identifiable service",
            rationale=(
                f"Denied for {self.reasons_summary(ctx)}. Modifier {modifier} was billed and {len(support)} note "
                "sentence(s) document a significant, separately identifiable E/M; they are quoted in the appeal."
            ),
            citations=dedupe_citations(citations),
            confidence=0.6,
            flags=[FLAG_EM_BUNDLED],
        )


__all__ = ["BundlingPlaybook", "FLAG_EM_BUNDLED"]
