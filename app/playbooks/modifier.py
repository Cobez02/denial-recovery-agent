"""Modifier playbook: corrected claim with the modifier fix derived from `modifier_rules.yaml`.

Situations (see the YAML for the code lists and keywords):

(a) an E/M code on the same date as a procedure without modifier 25 -> add 25 to the E/M line;
(b) a modifier CARC/RARC on a line carrying an invalid or repeated modifier -> remove it;
(e) modifier 59 billed together with an X{EPSU} modifier on a denied line -> drop the redundant 59;
(f) `kind: restricted_modifier`: a modifier that belongs only on certain codes (AT on 98940-98942)
    billed on another line -> remove it from that line;
(g) `kind: required_modifier`: a modifier a line of this kind must carry (AT on spinal CMT, GP on
    physical medicine codes) is missing from a line the denial names -> add it, never on top of the
    situation's `absent_modifiers` (an ABN modifier on a CMT line, another discipline modifier),
    never for a payer whose claim filing indicator is not among the situation's
    `filing_indicators`, and never when a note sentence matches the situation's `blocked_by`
    keyword set (maintenance documentation for AT): such a sentence never counts as support, and
    the draft is `no_action` (the denial stands) or, when other sentences document active care, a
    `clinic_question` for the coder;
(c) two procedures that bundle on the same day -> add 59 (XS when the notes describe a different
    anatomic region) to the column-2 line;
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
    FLAG_CONFLICTING_DOCUMENTATION,
    FLAG_MAINTENANCE_DOCUMENTED,
    FLAG_UNSUPPORTED_CODING_CHANGE,
    Playbook,
    code_is_numeric_in,
    dedupe_citations,
    keyword_pattern,
    marked_sentences,
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
    notices: list[str] = field(default_factory=list)

    @property
    def supported(self) -> bool:
        return (not self.needs_note_support) or bool(self.support)


@dataclass
class BlockedFix:
    """A required modifier the claim lacks that the notes rule out (maintenance care for AT)."""

    situation_id: str
    line_number: int
    modifier: str
    keyword_set: str
    blockers: list[tuple[NoteContext, str]]
    support: list[tuple[NoteContext, str]] = field(default_factory=list)


def keyword_set(name: str) -> dict[str, Any]:
    """A named keyword set of modifier_rules.yaml (`markers`, `negations`, `code_ranges`, ...)."""
    value = load_modifier_rules().get(name) or {}
    return value if isinstance(value, dict) else {}


def keyword_set_sentences(ctx: DenialContext, name: str) -> list[tuple[NoteContext, str]]:
    """The usable note sentences that carry one of the set's markers and none of its negations."""
    spec = keyword_set(name)
    return marked_sentences(ctx, spec.get("markers", []), spec.get("negations", []))


def maintenance_sentences(ctx: DenialContext) -> list[tuple[NoteContext, str]]:
    """Note sentences that document maintenance (supportive) care (`maintenance_documentation`)."""
    return keyword_set_sentences(ctx, "maintenance_documentation")


def filing_indicator(ctx: DenialContext) -> str | None:
    """The claim filing indicator (CLP06: MB, 16, MC, 12, ...) when the remittance context carries
    it. RemittanceContext gains the field with the integrator's foundation change; until then this
    is None and the payer-specific situations ask the reviewer to confirm the payer instead."""
    value = getattr(ctx.remittance, "filing_indicator", None)
    return str(value).strip().upper() if value else None


def _payer_allows(situation: dict[str, Any], ctx: DenialContext) -> tuple[bool, str | None]:
    """(applies, notice): False when the payer's filing indicator is known and not listed; the
    situation's `payer_notice` when the indicator is unknown."""
    indicators = {str(x).strip().upper() for x in situation.get("filing_indicators", [])}
    if not indicators:
        return True, None
    value = filing_indicator(ctx)
    if value is None:
        return True, situation.get("payer_notice")
    return value in indicators, None


def _same_day(a: ClaimLineContext, b: ClaimLineContext) -> bool:
    return a.service_date == b.service_date


def _notes_matching(
    playbook: Playbook, ctx: DenialContext, keywords: list[str]
) -> list[tuple[NoteContext, str]]:
    return playbook.find_supporting_notes(ctx, keywords)


def _situation_covers(situation: dict[str, Any], code: str) -> bool:
    """True when the situation's `codes` / `code_ranges` name the procedure code."""
    codes = {str(c).strip().upper() for c in situation.get("codes", [])}
    if code.strip().upper() in codes:
        return True
    return code_is_numeric_in(code, situation.get("code_ranges", []))


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
        return self.analyze(ctx)[0]

    def analyze(self, ctx: DenialContext) -> tuple[list[ModifierFix], list[BlockedFix]]:
        """(fixes, blocked): the structural fixes, one per line, with the note sentences that support
        each, and the required modifiers the notes rule out (`blocked_by`)."""
        rules = load_modifier_rules()
        situations = rules["situations"]
        working: dict[int, list[str]] = {line.line_number: list(line.modifiers) for line in ctx.claim.lines}
        fixes: dict[int, ModifierFix] = {}
        blocked: list[BlockedFix] = []

        def record(
            situation_id: str,
            line: ClaimLineContext,
            after: list[str],
            support: list,
            reasons: list,
            notices: list[str] | None = None,
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
                    notices=list(notices or []),
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
                existing.notices.extend(n for n in notices or [] if n not in existing.notices)
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

        # (f) a modifier restricted to certain codes, billed on another line
        for situation_id, situation in situations.items():
            if situation.get("kind") != "restricted_modifier":
                continue
            restricted = str(situation["modifier"]).upper()
            for line in ctx.claim.lines:
                current = [str(mod).strip().upper() for mod in working[line.line_number]]
                if restricted in current and not _situation_covers(situation, line.procedure_code):
                    related = [r for r in ctx.reasons if r.line_number in (None, line.line_number)]
                    record(situation_id, line, [m for m in current if m != restricted], [], related)

        # (g) a modifier a line of this kind must carry, missing from a line the denial names; never for
        #     a payer that does not use it, never when the notes rule it out (`blocked_by`)
        denied_numbers = set(self.reason_lines(ctx))
        for situation_id, situation in situations.items():
            if situation.get("kind") != "required_modifier":
                continue
            applies, notice = _payer_allows(situation, ctx)
            if not applies:
                continue
            required = str(situation["modifier"]).upper()
            blocking = {str(m).upper() for m in situation.get("absent_modifiers", [])}
            set_name = str(situation.get("blocked_by") or "")
            blockers = keyword_set_sentences(ctx, set_name) if set_name else []
            # a sentence that rules the modifier out is never its support
            support = [
                s
                for s in _notes_matching(self, ctx, list(situation.get("support_keywords", [])))
                if s not in blockers
            ]
            for line in ctx.claim.lines:
                if situation.get("requires_denied_line", True) and line.line_number not in denied_numbers:
                    continue
                if not _situation_covers(situation, line.procedure_code):
                    continue
                current = [str(mod).strip().upper() for mod in working[line.line_number]]
                if required in current or (blocking & set(current)):
                    continue
                if blockers:
                    blocked.append(
                        BlockedFix(situation_id, line.line_number, required, set_name, blockers, support)
                    )
                    continue
                related = [r for r in ctx.reasons if r.line_number in (None, line.line_number)]
                record(
                    situation_id,
                    line,
                    [*working[line.line_number], required],
                    support,
                    related,
                    [notice] if notice else None,
                )

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
                return on_denied, blocked
        return ordered, blocked

    # ---- proposal ---------------------------------------------------------------------

    def propose(self, ctx: DenialContext, classification: ClassificationResult) -> ProposalDraft:
        fixes, blocked = self.analyze(ctx)
        if fixes:
            return self.corrected_claim_from_fixes(ctx, fixes, blocked=blocked)
        if blocked:
            return self.blocked_proposal(ctx, blocked)
        return self.modifier_question(ctx)

    def blocked_proposal(self, ctx: DenialContext, blocked: list[BlockedFix]) -> ProposalDraft:
        """The notes rule the required modifier out (maintenance care documented for AT): the denial
        stands (`no_action`), or the coder decides when other sentences document active care."""
        blockers = [s for fix in blocked for s in fix.blockers]
        support = [s for fix in blocked for s in fix.support if s not in blockers]
        lines = sorted({fix.line_number for fix in blocked})
        modifiers = ", ".join(dict.fromkeys(fix.modifier for fix in blocked))
        citations: list[Citation] = list(self.cite_reasons(ctx))
        for number in lines:
            line = self.line_by_number(ctx, number)
            if line is not None:
                citations.append(
                    self.cite_claim_field(ctx, f"lines[{number}].procedure_code", line.procedure_code)
                )
                citations.append(self.cite_claim_field(ctx, f"lines[{number}].modifiers", line.modifiers))
        citations.extend(self.note_citations(ctx, blockers))
        quoted = blockers[0][1]
        set_rationale = " ".join(
            " ".join(str(keyword_set(name).get("rationale") or "").split())
            for name in dict.fromkeys(fix.keyword_set for fix in blocked)
        ).strip()
        if support:
            citations.extend(self.note_citations(ctx, support))
            question = (
                f"Claim {ctx.claim.claim_control_number} (DOS {self.dos_label(ctx)}) was denied for "
                f'{self.reasons_summary(ctx)}. The notes document maintenance care ("{quoted}") and also '
                f'active treatment ("{support[0][1]}"). Modifier {modifiers} may be added only for active, '
                "corrective treatment and never to maintenance care: please have the treating chiropractor or "
                "a coder state which applies to this visit before anything is resubmitted."
            )
            return self.build(
                ctx,
                action_type=ActionType.CLINIC_QUESTION,
                summary=f"Coder decision: the notes document both maintenance and active care (modifier {modifiers})",
                rationale=f"Denied for {self.reasons_summary(ctx)}. {set_rationale} The notes also carry "
                "active-treatment wording, so the coder decides.",
                citations=dedupe_citations(citations),
                clinic_question=question,
                confidence=0.45,
                flags=[FLAG_MAINTENANCE_DOCUMENTED, FLAG_CONFLICTING_DOCUMENTATION],
            )
        return self.build(
            ctx,
            action_type=ActionType.NO_ACTION,
            summary=f"Denial stands: the notes document maintenance care, so modifier {modifiers} is not added",
            rationale=f"Denied for {self.reasons_summary(ctx)}. {set_rationale}",
            citations=dedupe_citations(citations),
            confidence=0.75,
            flags=[FLAG_MAINTENANCE_DOCUMENTED],
        )

    def corrected_claim_from_fixes(
        self,
        ctx: DenialContext,
        fixes: list[ModifierFix],
        *,
        extra_citations: list[Citation] | None = None,
        blocked: list[BlockedFix] | None = None,
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
        for notice in dict.fromkeys(n for fix in fixes for n in fix.notices):
            rationale += f" {notice}"
        for held in blocked or []:
            rationale += (
                f" Modifier {held.modifier} is not added to line {held.line_number}: the notes document "
                f'maintenance care ("{held.blockers[0][1]}").'
            )
            citations.extend(self.note_citations(ctx, held.blockers))
            if FLAG_MAINTENANCE_DOCUMENTED not in flags:
                flags.append(FLAG_MAINTENANCE_DOCUMENTED)
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


__all__ = [
    "BlockedFix",
    "ModifierFix",
    "ModifierPlaybook",
    "RULES_PATH",
    "filing_indicator",
    "keyword_set",
    "keyword_set_sentences",
    "load_modifier_rules",
    "maintenance_sentences",
    "notes_mention_any",
]
