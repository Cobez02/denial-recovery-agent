"""Modifier playbook: corrected claim with the modifier fix derived from `modifier_rules.yaml`.

Situations (see the YAML for the code lists and keywords):

(a) an E/M code on the same date as a procedure without modifier 25 -> add 25 to the E/M line;
(b) CARC 4 / RARC M78 or N519 on a line carrying an invalid or repeated modifier -> remove it;
(e) modifier 59 billed together with an X{EPSU} modifier on a denied line -> drop the redundant 59;
(c) two procedures that bundle on the same day -> add 59 (XS when the notes describe a different site)
    to the column-2 line;
(d) the notes describe a bilateral service billed on one line -> add 50.

Every modifier edit is a coding change: `coding_change=True`, `requires_note_excerpt=True`, flag
`coding_change`. When the notes do not support the edit the corrected claim is still built, but the
draft is flagged `unsupported_coding_change`, capped at confidence 0.4, and its rationale tells the
reviewer to confirm the documentation before approval.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.drafting.corrected_claim import build_corrected_claim
from app.enums import ActionType, DenialCategory
from app.playbooks.base import (
    FLAG_UNSUPPORTED_CODING_CHANGE,
    Playbook,
    code_is_numeric_in,
    dedupe_citations,
    keyword_pattern,
    split_sentences,
    usable_notes,
)
from app.schemas import (
    Citation,
    ClaimLineContext,
    ClassificationResult,
    CorrectedClaimChange,
    DenialContext,
    DenialReason,
    NoteContext,
    ProposalDraft,
)

RULES_PATH = Path(__file__).resolve().parent / "modifier_rules.yaml"

REVIEWER_CONFIRM = (
    "The reviewer must confirm the clinical documentation supports this change before approval."
)


@lru_cache(maxsize=1)
def load_modifier_rules(path: str | None = None) -> dict[str, Any]:
    """The parsed YAML rules (cached)."""
    with open(path or RULES_PATH, encoding="utf-8") as fh:
        rules = yaml.safe_load(fh) or {}
    rules["valid_modifiers"] = [str(m) for m in rules.get("valid_modifiers", [])]
    rules["situations"] = {s["id"]: s for s in rules.get("situations", [])}
    return rules


@dataclass
class ModifierFix:
    """One proposed modifier edit on one claim line."""

    situation_id: str
    line_number: int
    before: list[str]
    after: list[str]
    description: str
    needs_note_support: bool
    support: list[tuple[NoteContext, str]] = field(default_factory=list)
    reasons: list[DenialReason] = field(default_factory=list)

    @property
    def supported(self) -> bool:
        return (not self.needs_note_support) or bool(self.support)


def _same_day(a: ClaimLineContext, b: ClaimLineContext) -> bool:
    return a.service_date == b.service_date


def _notes_matching(
    playbook: Playbook, ctx: DenialContext, keywords: list[str]
) -> list[tuple[NoteContext, str]]:
    return playbook.find_supporting_notes(ctx, keywords)


class ModifierPlaybook(Playbook):
    category = DenialCategory.MODIFIER

    # ---- situation detection ---------------------------------------------------------

    def modifier_reasons(self, ctx: DenialContext) -> list[DenialReason]:
        rules = load_modifier_rules()
        carcs = {str(c) for c in rules.get("modifier_carcs", [])}
        rarcs = {str(r) for r in rules.get("modifier_rarcs", [])}
        return [r for r in ctx.reasons if r.carc in carcs or any(rc in rarcs for rc in r.rarcs)]

    def find_fixes(self, ctx: DenialContext) -> list[ModifierFix]:
        """All structural fixes, one per line, with the note sentences that support each."""
        rules = load_modifier_rules()
        situations = rules["situations"]
        working: dict[int, list[str]] = {line.line_number: list(line.modifiers) for line in ctx.claim.lines}
        fixes: dict[int, ModifierFix] = {}

        def record(
            situation_id: str, line: ClaimLineContext, after: list[str], support: list, reasons: list
        ) -> None:
            situation = situations.get(situation_id, {})
            existing = fixes.get(line.line_number)
            if existing is None:
                fixes[line.line_number] = ModifierFix(
                    situation_id=situation_id,
                    line_number=line.line_number,
                    before=list(line.modifiers),
                    after=after,
                    description=situation.get("description", situation_id),
                    needs_note_support=bool(situation.get("needs_note_support", True)),
                    support=list(support),
                    reasons=list(reasons),
                )
            else:
                existing.after = after
                existing.description += "; " + situation.get("description", situation_id)
                existing.situation_id += "+" + situation_id
                existing.needs_note_support = existing.needs_note_support or bool(
                    situation.get("needs_note_support", True)
                )
                existing.support.extend(s for s in support if s not in existing.support)
                existing.reasons.extend(r for r in reasons if r not in existing.reasons)
            working[line.line_number] = after

        # (b) invalid or duplicate modifiers on lines the denial points at
        valid = set(rules["valid_modifiers"])
        mod_reasons = self.modifier_reasons(ctx)
        target_numbers = {r.line_number for r in mod_reasons if r.line_number is not None}
        targets = [
            line for line in ctx.claim.lines if not target_numbers or line.line_number in target_numbers
        ]
        if "invalid_or_duplicate_modifier" in situations and mod_reasons:
            for line in targets:
                current = [str(mod).strip().upper() for mod in working[line.line_number]]
                cleaned: list[str] = []
                for m in current:
                    if m in valid and m not in cleaned:
                        cleaned.append(m)
                if cleaned != current:  # a difference in case or spacing alone is not a coding change
                    related = [r for r in mod_reasons if r.line_number in (None, line.line_number)]
                    record("invalid_or_duplicate_modifier", line, cleaned, [], related)

        # (e) 59 together with a specific X{EPSU} modifier on a denied line: keep the specific one
        redundant_rule = situations.get("redundant_distinct_service_pair")
        if redundant_rule and mod_reasons:
            specific = {str(m) for m in rules.get("distinct_service_modifiers", [])} - {"59"}
            for line in targets:
                current = [str(mod).strip().upper() for mod in working[line.line_number]]
                if "59" in current and any(m in specific for m in current):
                    related = [r for r in mod_reasons if r.line_number in (None, line.line_number)]
                    after = [m for m in current if m != "59"]
                    record("redundant_distinct_service_pair", line, after, [], related)

        # (a) E/M on the same date as a procedure without 25
        em_rule = situations.get("em_same_day_procedure")
        if em_rule:
            em_ranges = rules.get("em_code_ranges", [])
            proc_ranges = rules.get("procedure_code_ranges", [])
            support = _notes_matching(self, ctx, list(em_rule.get("support_keywords", [])))
            for line in ctx.claim.lines:
                if not code_is_numeric_in(line.procedure_code, em_ranges):
                    continue
                if em_rule["modifier"] in working[line.line_number]:
                    continue
                if any(
                    other is not line
                    and code_is_numeric_in(other.procedure_code, proc_ranges)
                    and _same_day(line, other)
                    for other in ctx.claim.lines
                ):
                    related = [r for r in ctx.reasons if r.line_number in (None, line.line_number)]
                    record(
                        "em_same_day_procedure",
                        line,
                        [*working[line.line_number], em_rule["modifier"]],
                        support,
                        related,
                    )

        # (c) bundled pairs on the same day
        pair_rule = situations.get("bundled_pair")
        if pair_rule:
            distinct = {str(m) for m in rules.get("distinct_service_modifiers", [])}
            support = _notes_matching(self, ctx, list(pair_rule.get("support_keywords", [])))
            site_support = _notes_matching(self, ctx, list(pair_rule.get("alternate_keywords", [])))
            modifier = pair_rule.get("alternate_modifier", "XS") if site_support else pair_rule["modifier"]
            for column1, column2 in pair_rule.get("pairs", []):
                c1_lines = [line for line in ctx.claim.lines if line.procedure_code == str(column1)]
                c2_lines = [line for line in ctx.claim.lines if line.procedure_code == str(column2)]
                for secondary in c2_lines:
                    if any(_same_day(secondary, primary) for primary in c1_lines) and not (
                        distinct & {str(m).upper() for m in working[secondary.line_number]}
                    ):
                        related = [r for r in ctx.reasons if r.line_number in (None, secondary.line_number)]
                        record(
                            "bundled_pair",
                            secondary,
                            [*working[secondary.line_number], modifier],
                            support + site_support,
                            related,
                        )

        # (d) bilateral service documented, billed on one line
        bilateral_rule = situations.get("bilateral")
        if bilateral_rule:
            support = _notes_matching(self, ctx, list(bilateral_rule.get("support_keywords", [])))
            if support:
                laterality = {str(m) for m in rules.get("laterality_modifiers", [])}
                target: ClaimLineContext | None = None
                reason_lines = self.reason_lines(ctx)
                if len(reason_lines) == 1:
                    target = self.line_by_number(ctx, reason_lines[0])
                elif len(ctx.claim.lines) == 1:
                    target = ctx.claim.lines[0]
                if target is not None and not (
                    laterality & {str(m).upper() for m in working[target.line_number]}
                ):
                    related = [r for r in ctx.reasons if r.line_number in (None, target.line_number)]
                    record(
                        "bilateral",
                        target,
                        [*working[target.line_number], bilateral_rule["modifier"]],
                        support,
                        related,
                    )

        ordered = [fixes[n] for n in sorted(fixes)]
        # When the denial names lines, keep the fixes on those lines (fall back to all fixes).
        denied_lines = set(self.reason_lines(ctx))
        if denied_lines:
            on_denied = [f for f in ordered if f.line_number in denied_lines]
            if on_denied:
                return on_denied
        return ordered

    # ---- proposal ---------------------------------------------------------------------

    def propose(self, ctx: DenialContext, classification: ClassificationResult) -> ProposalDraft:
        fixes = self.find_fixes(ctx)
        if not fixes:
            return self.modifier_question(ctx)
        return self.corrected_claim_from_fixes(ctx, fixes)

    def corrected_claim_from_fixes(
        self, ctx: DenialContext, fixes: list[ModifierFix], *, extra_citations: list[Citation] | None = None
    ) -> ProposalDraft:
        citations: list[Citation] = list(self.cite_reasons(ctx)) + list(extra_citations or [])
        changes: list[CorrectedClaimChange] = []
        summaries: list[str] = []
        all_supported = True
        for fix in fixes:
            line = self.line_by_number(ctx, fix.line_number)
            line_cites = [
                self.cite_claim_field(
                    ctx, f"lines[{fix.line_number}].procedure_code", line.procedure_code if line else None
                ),
                self.cite_claim_field(ctx, f"lines[{fix.line_number}].modifiers", fix.before),
            ]
            note_cites = self.note_citations(ctx, fix.support)
            citations.extend(line_cites + note_cites)
            refs = [c.ref for c in self.cite_reasons(ctx, fix.reasons)] + [
                c.ref for c in line_cites + note_cites
            ]
            changes.append(
                CorrectedClaimChange(
                    path=f"lines[{fix.line_number}].modifiers",
                    before=list(fix.before),
                    after=list(fix.after),
                    reason=fix.description,
                    coding_change=True,
                    citation_refs=list(dict.fromkeys(refs)),
                )
            )
            code = line.procedure_code if line else "?"
            summaries.append(
                f"line {fix.line_number} ({code}): {_format_mods(fix.before)} -> {_format_mods(fix.after)}"
            )
            all_supported = all_supported and fix.supported
        corrected = build_corrected_claim(ctx, changes)
        summary = "Corrected claim with modifier change on " + "; ".join(summaries)
        rationale = f"Denied for {self.reasons_summary(ctx)}. " + " ".join(
            f"{fix.description} (line {fix.line_number})." for fix in fixes
        )
        quoted = sum(len(fix.support) for fix in fixes)
        flags: list[str] = []
        if all_supported:
            confidence = 0.8 if quoted else 0.7
            if quoted:
                rationale += f" {quoted} note sentence(s) support the change and are cited."
        else:
            confidence = 0.35
            flags.append(FLAG_UNSUPPORTED_CODING_CHANGE)
            rationale += " No note excerpt on file supports this modifier. " + REVIEWER_CONFIRM
        return self.build(
            ctx,
            action_type=ActionType.CORRECTED_CLAIM,
            summary=summary,
            rationale=rationale,
            citations=dedupe_citations(citations),
            corrected_claim=corrected,
            coding_change=True,
            confidence=confidence,
            flags=flags,
        )

    def modifier_question(self, ctx: DenialContext) -> ProposalDraft:
        lines = self.reason_lines(ctx) or [line.line_number for line in ctx.claim.lines]
        described = []
        citations: list[Citation] = list(self.cite_reasons(ctx))
        for n in lines:
            line = self.line_by_number(ctx, n)
            if line is None:
                continue
            described.append(f"line {n}: {line.procedure_code} {_format_mods(line.modifiers)}")
            citations.append(self.cite_claim_field(ctx, f"lines[{n}].modifiers", line.modifiers))
        question = (
            "The payer reports a modifier problem but no rule in modifier_rules.yaml matches the claim. "
            f"Please have a coder review the modifiers on {'; '.join(described) or 'the denied lines'} "
            f"against the documentation for {self.dos_label(ctx)} and state the correct modifier(s), "
            "quoting the note sentence that supports them."
        )
        return self.build(
            ctx,
            action_type=ActionType.CLINIC_QUESTION,
            summary="Coder review of the modifiers on the denied line(s)",
            rationale=f"Denied for {self.reasons_summary(ctx)}. No modifier situation in the rules applies, so the fix needs a coder.",
            citations=citations,
            clinic_question=question,
            confidence=0.4,
        )


def _format_mods(mods: list[str]) -> str:
    return "[" + ", ".join(str(m) for m in mods) + "]" if mods else "[no modifiers]"


def notes_mention_any(ctx: DenialContext, keywords: list[str]) -> bool:
    patterns = [keyword_pattern(k) for k in keywords if k]
    return any(
        p.search(s) for note in usable_notes(ctx) for s in split_sentences(note.text) for p in patterns
    )


__all__ = ["ModifierFix", "ModifierPlaybook", "load_modifier_rules", "notes_mention_any", "RULES_PATH"]
