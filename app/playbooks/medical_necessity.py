"""Medical-necessity playbook (CARC 50 / 56 / 167 / 11 / 150 / 151, RARC N115 / M76 / N661).

- `clinic_question` when no note excerpt is on file at all.
- maintenance care documented for a denied service in `maintenance_documentation.code_ranges`
  (modifier_rules.yaml): `no_action` (the denial stands; an ABN going forward), or a
  `clinic_question` for the coder when other sentences document active care. A sentence that
  documents maintenance care is never quoted as support for an appeal.
- `corrected_claim` (coding change, flagged) when a note mentions an ICD-10 code that is absent from
  the claim: the documented diagnosis is added to `diagnosis_codes`; a code of the family that must
  lead the denied line (`primary_diagnosis_rules`: the M99.0x subluxation level for spinal CMT)
  goes to the line's first pointer, replacing the code there when that code is not of the family.
- otherwise `appeal` quoting the note sentences that mention the billed diagnosis or its symptoms
  (keyword list per diagnosis family below, plus the codes themselves).
"""

from __future__ import annotations

import re
from typing import Any

from app.drafting.corrected_claim import build_corrected_claim
from app.enums import ActionType, DenialCategory
from app.playbooks.base import (
    FLAG_CONFLICTING_DOCUMENTATION,
    FLAG_MAINTENANCE_DOCUMENTED,
    FLAG_NO_NOTES,
    Playbook,
    code_is_numeric_in,
    dedupe_citations,
    latest_note,
    split_sentences,
    usable_notes,
)
from app.playbooks.modifier import keyword_set, load_modifier_rules, maintenance_sentences
from app.schemas import (
    ClaimLineContext,
    ClassificationResult,
    CorrectedClaimChange,
    DenialContext,
    NoteContext,
    ProposalDraft,
)

FLAG_DIAGNOSIS_FROM_NOTES = "diagnosis_added_from_notes"
FLAG_WEAK_NOTE_SUPPORT = "weak_note_support"

# ICD-10-CM codes written with a subcategory (L57.0, C44.319, U07.1). Three-character categories
# alone are not matched: too many false positives in free text (vitamin B12, room E12).
ICD10_IN_TEXT = re.compile(r"(?<![A-Za-z0-9])([A-Z][0-9]{2}\.[0-9A-Z]{1,4})(?![A-Za-z0-9])")


def _code_key(code: str) -> str:
    """Comparison key for an ICD-10 code: upper case, no dot (L57.0 and L570 are the same code)."""
    return code.strip().upper().replace(".", "")


# Keywords per diagnosis family (ICD-10 category prefixes), chiropractic profile (docs/SPECIALTY.md).
# The table is data: another specialty replaces the families, not the playbook.
DIAGNOSIS_FAMILIES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (
        ("M99",),
        (
            "subluxation",
            "segmental dysfunction",
            "somatic dysfunction",
            "segmental",
            "motion segment",
            "restricted segmental motion",
            "restricted range of motion",
            "restricted motion",
            "asymmetry",
            "misalignment",
            "fixation",
            "hypertonicity",
            "tissue tone",
            "tenderness",
            "palpation",
            "PART findings",
            "PART examination",
            "PART exam",
            "level",
        ),
    ),
    (
        ("M54",),
        (
            "pain",
            "neck pain",
            "back pain",
            "low back",
            "cervicalgia",
            "lumbago",
            "sciatica",
            "radiating",
            "radiculopathy",
            "spasm",
            "tenderness",
            "stiffness",
            "restricted range of motion",
            "restricted rotation",
        ),
    ),
    (
        ("M50", "M51"),
        ("disc", "radiculopathy", "radiating", "numbness", "tingling", "herniation", "degeneration"),
    ),
    (
        ("M47", "M43", "M53", "M46", "M48"),
        ("spondylosis", "stiffness", "instability", "facet", "dorsopathy", "degenerative", "stenosis"),
    ),
    (
        ("S13", "S23", "S33", "S16", "S29", "S39"),
        ("sprain", "strain", "whiplash", "injury", "accident", "trauma", "motor vehicle", "fall", "lifting"),
    ),
    (("G44", "G43", "R51"), ("headache", "cervicogenic", "tension", "suboccipital", "migraine")),
    (
        ("M79", "M62", "M60", "M25"),
        ("myalgia", "muscle spasm", "trigger point", "myofascial", "joint pain", "shoulder", "extremity"),
    ),
)

GENERIC_KEYWORDS: tuple[str, ...] = (
    "medically necessary",
    "medical necessity",
    "symptomatic",
    "symptoms",
    "presents with",
    "complains of",
    "history of",
    "worsening",
    "failed",
    "not responded",
    "diagnosis",
    "assessment",
    "indicated",
    "treatment",
    "treatment plan",
    "plan of care",
    "re-examination",
    "re-evaluation",
    "improvement",
    "improved",
    "outcome score",
    "measurable",
    "range of motion",
    "goals",
    "active treatment",
    "corrective",
    "not maintenance",
)


def _in_family(code: str, rule: dict[str, Any]) -> bool:
    return any(code.strip().upper().startswith(str(p).strip().upper()) for p in rule.get("prefixes", []))


def _primary_rule_for(
    code: str, lines: list[ClaimLineContext], rules: list[dict[str, Any]]
) -> tuple[dict[str, Any] | None, ClaimLineContext | None]:
    """The `primary_diagnosis_rules` entry whose family the code belongs to and the denied line it
    must lead, or (None, None)."""
    for rule in rules:
        if not _in_family(code, rule):
            continue
        for line in lines:
            if code_is_numeric_in(line.procedure_code, rule.get("procedure_code_ranges", [])):
                return rule, line
    return None, None


class MedicalNecessityPlaybook(Playbook):
    category = DenialCategory.MEDICAL_NECESSITY

    # ---- helpers ------------------------------------------------------------------------

    def keywords_for(self, ctx: DenialContext) -> list[str]:
        codes = [c.strip().upper() for c in ctx.claim.diagnosis_codes if c and c.strip()]
        keywords: list[str] = list(codes)
        keywords.extend(code.replace(".", "") for code in codes if "." in code)
        for prefixes, words in DIAGNOSIS_FAMILIES:
            if any(code.startswith(prefixes) for code in codes):
                keywords.extend(words)
        keywords.extend(GENERIC_KEYWORDS)
        return list(dict.fromkeys(k for k in keywords if k))

    def documented_codes_missing(self, ctx: DenialContext) -> list[tuple[str, NoteContext, str]]:
        """(code, note, sentence) for every dotted ICD-10 code in the usable notes that the claim lacks."""
        on_claim = {_code_key(c) for c in ctx.claim.diagnosis_codes if c}
        found: list[tuple[str, NoteContext, str]] = []
        seen: set[str] = set()
        for note in usable_notes(ctx):
            for sentence in split_sentences(note.text):
                for code in ICD10_IN_TEXT.findall(sentence):
                    code = code.upper()
                    key = _code_key(code)
                    if key not in on_claim and key not in seen:
                        seen.add(key)
                        found.append((code, note, sentence))
        return found

    def denied_lines(self, ctx: DenialContext) -> list[ClaimLineContext]:
        """The lines the denial names; every claim line for a claim-level denial."""
        numbers = self.reason_lines(ctx)
        if not numbers:
            return list(ctx.claim.lines)
        return [line for line in ctx.claim.lines if line.line_number in numbers]

    def maintenance_applies(self, ctx: DenialContext) -> bool:
        """True when a denied service is one the maintenance-care rule covers (spinal CMT)."""
        ranges = keyword_set("maintenance_documentation").get("code_ranges", [])
        return any(code_is_numeric_in(line.procedure_code, ranges) for line in self.denied_lines(ctx))

    def diagnosis_changes(
        self, ctx: DenialContext, new_codes: list[str], refs: list[str]
    ) -> list[CorrectedClaimChange]:
        """The diagnosis edits for the documented codes the claim lacks. A code of the family that
        must lead the denied line (`primary_diagnosis_rules`) takes the line's first pointer: it
        replaces the code there when that code is not of the family (the symptom code the claim led
        with), else it is appended and the line points to it too; every other code is appended."""
        codes = list(ctx.claim.diagnosis_codes)
        rules: list[dict[str, Any]] = load_modifier_rules().get("primary_diagnosis_rules") or []
        # only a line the denial names has a "first pointer" to lead with
        named = self.denied_lines(ctx) if self.reason_lines(ctx) else []
        pointers_after: dict[int, list[int]] = {}
        replaced: list[tuple[str, str, ClaimLineContext]] = []
        added: list[str] = []
        for code in new_codes:
            rule, line = _primary_rule_for(code, named, rules)
            if rule is None or line is None or not line.diagnosis_pointers:
                if code not in codes:
                    codes.append(code)
                    added.append(code)
                continue
            first = line.diagnosis_pointers[0]
            current = codes[first - 1] if 0 < first <= len(codes) else None
            if current is not None and not _in_family(current, rule):
                codes[first - 1] = code
                replaced.append((current, code, line))
                continue
            codes.append(code)
            added.append(code)
            pointers = pointers_after.get(line.line_number, list(line.diagnosis_pointers))
            if len(pointers) < 4:
                pointers_after[line.line_number] = [*pointers, len(codes)]
        reasons = [
            f"Replace {old}, the code line {line.line_number} ({line.procedure_code}) led with, by the documented "
            f"{new}: the line must lead with it"
            for old, new, line in replaced
        ]
        if added:
            reasons.append(
                "Add the diagnosis code(s) documented in the notes but absent from the claim: "
                + ", ".join(added)
            )
        changes = [
            CorrectedClaimChange(
                path="diagnosis_codes",
                before=list(ctx.claim.diagnosis_codes),
                after=codes,
                reason="; ".join(reasons),
                coding_change=True,
                citation_refs=list(refs),
            )
        ]
        for number, pointers in pointers_after.items():
            line = self.line_by_number(ctx, number)
            changes.append(
                CorrectedClaimChange(
                    path=f"lines[{number}].diagnosis_pointers",
                    before=list(line.diagnosis_pointers) if line else None,
                    after=pointers,
                    reason=f"Point line {number} to the documented diagnosis as well",
                    coding_change=True,
                    citation_refs=list(refs),
                )
            )
        return changes

    # ---- proposal -----------------------------------------------------------------------

    def maintenance_proposal(
        self, ctx: DenialContext, blockers: list[tuple[NoteContext, str]]
    ) -> ProposalDraft:
        """Maintenance care documented: the denial stands, or the coder decides on conflicting notes."""
        support = [s for s in self.find_supporting_notes(ctx, self.keywords_for(ctx)) if s not in blockers]
        rationale_text = " ".join(
            str(keyword_set("maintenance_documentation").get("rationale") or "").split()
        )
        citations = [
            *self.cite_reasons(ctx),
            self.cite_claim_field(ctx, "diagnosis_codes", ctx.claim.diagnosis_codes),
            *self.note_citations(ctx, blockers),
        ]
        if support:
            citations.extend(self.note_citations(ctx, support))
            question = (
                f"Claim {ctx.claim.claim_control_number} (DOS {self.dos_label(ctx)}) was denied for medical necessity "
                f'({self.reasons_summary(ctx)}). The notes document maintenance care ("{blockers[0][1]}") and also '
                f'active treatment ("{support[0][1]}"). Please have the treating chiropractor or a coder state '
                "whether this visit was active, corrective care (an appeal with the measured progress) or maintenance "
                "care (the denial stands)."
            )
            return self.build(
                ctx,
                action_type=ActionType.CLINIC_QUESTION,
                summary="Coder decision: the notes document both maintenance and active care",
                rationale=f"Denied for {self.reasons_summary(ctx)}. {rationale_text} The notes also carry "
                "active-treatment wording, so the coder decides.",
                citations=dedupe_citations(citations),
                clinic_question=question,
                confidence=0.45,
                flags=[FLAG_MAINTENANCE_DOCUMENTED, FLAG_CONFLICTING_DOCUMENTATION],
            )
        return self.build(
            ctx,
            action_type=ActionType.NO_ACTION,
            summary="Denial stands: the notes document maintenance care",
            rationale=f"Denied for {self.reasons_summary(ctx)}. {rationale_text}",
            citations=dedupe_citations(citations),
            confidence=0.75,
            flags=[FLAG_MAINTENANCE_DOCUMENTED],
        )

    def propose(self, ctx: DenialContext, classification: ClassificationResult) -> ProposalDraft:
        dx = ", ".join(ctx.claim.diagnosis_codes) or "the billed diagnosis"
        if not usable_notes(ctx):  # a note flagged for planted instructions counts as absent
            question = (
                f"Claim {ctx.claim.claim_control_number} (DOS {self.dos_label(ctx)}) was denied for medical necessity "
                f"({self.reasons_summary(ctx)}). No note excerpt is on file. Please supply the note sentences that "
                f"document the diagnosis ({dx}), the symptoms or findings, and why the service was indicated."
            )
            return self.build(
                ctx,
                action_type=ActionType.CLINIC_QUESTION,
                summary="Ask the clinic for note excerpts that document medical necessity",
                rationale=f"Denied for {self.reasons_summary(ctx)}. An appeal needs quoted documentation and none is on file.",
                citations=[
                    *self.cite_reasons(ctx),
                    self.cite_claim_field(ctx, "diagnosis_codes", ctx.claim.diagnosis_codes),
                ],
                clinic_question=question,
                confidence=0.5,
                flags=[FLAG_NO_NOTES],
            )

        if self.maintenance_applies(ctx):
            blockers = maintenance_sentences(ctx)
            if blockers:
                return self.maintenance_proposal(ctx, blockers)

        missing = self.documented_codes_missing(ctx)
        if missing:
            support = [(note, sentence) for _, note, sentence in missing]
            note_cites = self.note_citations(ctx, support)
            field_cite = self.cite_claim_field(ctx, "diagnosis_codes", ctx.claim.diagnosis_codes)
            citations = [*self.cite_reasons(ctx), field_cite, *note_cites]
            new_codes = [code for code, _, _ in missing]
            changes = self.diagnosis_changes(ctx, new_codes, [c.ref for c in citations])
            for change in changes[1:]:
                number = change.path[len("lines[") : change.path.index("]")]
                line = self.line_by_number(ctx, int(number))
                citations.append(
                    self.cite_claim_field(ctx, change.path, line.diagnosis_pointers if line else None)
                )
            leads = "Replace" in changes[0].reason
            summary = (
                "Corrected claim leading the denied line with the documented diagnosis "
                + ", ".join(new_codes)
                if leads
                else "Corrected claim adding documented diagnosis code(s) " + ", ".join(new_codes)
            )
            return self.build(
                ctx,
                action_type=ActionType.CORRECTED_CLAIM,
                summary=summary,
                rationale=(
                    f"Denied for {self.reasons_summary(ctx)}. The notes document {', '.join(new_codes)}, which the "
                    f"claim does not carry. {changes[0].reason}. Changing the diagnoses is a coding change; the "
                    "quoted note sentence must be confirmed before approval."
                ),
                citations=dedupe_citations(citations),
                corrected_claim=build_corrected_claim(ctx, changes),
                coding_change=True,
                confidence=0.6,
                flags=[FLAG_DIAGNOSIS_FROM_NOTES],
            )

        support = self.find_supporting_notes(ctx, self.keywords_for(ctx))
        flags: list[str] = []
        confidence = 0.7
        if not support:
            # Nothing matched the diagnosis keywords: quote the opening of the most recent note so the
            # reviewer can judge it, and say so.
            latest = latest_note(ctx)
            assert latest is not None  # usable_notes(ctx) is non-empty here
            opening = split_sentences(latest.text)[:2]
            support = [(latest, sentence) for sentence in opening]
            flags.append(FLAG_WEAK_NOTE_SUPPORT)
            confidence = 0.45
        citations = [
            *self.cite_reasons(ctx),
            self.cite_claim_field(ctx, "diagnosis_codes", ctx.claim.diagnosis_codes),
            *self.note_citations(ctx, support),
        ]
        rationale = f"Denied for {self.reasons_summary(ctx)}. "
        if FLAG_WEAK_NOTE_SUPPORT in flags:
            rationale += (
                f"No note sentence mentions the billed diagnosis ({dx}) or its symptoms; the opening of the latest note "
                "is quoted and the reviewer should replace it with the relevant excerpt."
            )
        else:
            rationale += f"{len(support)} note sentence(s) tie the service to the billed diagnosis ({dx}) and are quoted in the appeal."
        return self.build(
            ctx,
            action_type=ActionType.APPEAL,
            summary=f"Appeal the medical-necessity denial with quoted documentation for {dx}",
            rationale=rationale,
            citations=dedupe_citations(citations),
            confidence=confidence,
            flags=flags,
        )


__all__ = [
    "DIAGNOSIS_FAMILIES",
    "FLAG_DIAGNOSIS_FROM_NOTES",
    "FLAG_WEAK_NOTE_SUPPORT",
    "ICD10_IN_TEXT",
    "MedicalNecessityPlaybook",
]
