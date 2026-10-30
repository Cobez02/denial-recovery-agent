"""Missing-information playbook: RARC -> the field to resend, the question to ask, or a modifier fix.

- M119 / N59  -> lines[n].ndc: corrected claim resending the NDC when the claim line has one, else a
                  clinic question naming the NDC
- M62 / N54   -> prior_auth_number: corrected claim when the claim record has it, else a clinic question
- N264 / N265 / N285 / N286 -> referring_provider_npi (the ordering or referring provider): resent
                  when the claim record has it; when it is empty and a note says the treating provider
                  ordered the service (a chiropractor orders the clinic's own X-rays,
                  `ORDERING_PROVIDER_NOTE_KEYWORDS`), the rendering provider's NPI is proposed as the
                  ordering provider, citing that note; otherwise a clinic question
- N290        -> rendering_provider_npi: same pattern as the NDC
- MA130 / MA122 / MA121 / M122 / N29 / N56 / M127 / N179 / N225 / N706 / N393 / M76 / M20 / M51 / M79
                  -> clinic question quoting the RARC text and naming what is needed (a diagnosis or
                  procedure code change is a coding change the clinic must document, so those are
                  never guessed; the initial treatment date, x-ray date and subluxation level have
                  no field in the claim export, so they are asked for, quoting the note sentence
                  that already states them, `QUESTION_NOTE_KEYWORDS`)
- N519 / M78 / N822 / N823 -> delegated to the modifier playbook
- CARC 125 / 16 / 206 / 226 / 227 / 252 without a mapped RARC -> clinic question quoting the reason text
"""

from __future__ import annotations

from typing import Any

from app.drafting.corrected_claim import build_corrected_claim
from app.enums import ActionType, DenialCategory
from app.playbooks.base import Playbook, dedupe_citations, rarc_text, reason_label, reason_text
from app.playbooks.modifier import ModifierPlaybook
from app.schemas import (
    Citation,
    ClassificationResult,
    CorrectedClaimChange,
    DenialContext,
    DenialReason,
    NoteContext,
    ProposalDraft,
)

FIELD_RARCS: dict[str, str] = {
    "M119": "ndc",
    "N59": "ndc",
    "M62": "prior_auth_number",
    "N54": "prior_auth_number",
    "N264": "referring_provider_npi",
    "N265": "referring_provider_npi",
    "N285": "referring_provider_npi",
    "N286": "referring_provider_npi",
    "N290": "rendering_provider_npi",
}

FIELD_LABELS: dict[str, str] = {
    "ndc": "NDC (national drug code)",
    "prior_auth_number": "prior authorization number",
    "referring_provider_npi": "ordering or referring provider NPI",
    "rendering_provider_npi": "rendering provider NPI",
}

QUESTION_RARCS: dict[str, str] = {
    "MA130": (
        "identify the incomplete or invalid element the payer rejected; the claim must be corrected and "
        "sent as a new claim because MA130 carries no appeal rights"
    ),
    "MA122": (
        "the date the course of treatment started (the initial treatment date from the initial-visit "
        "note, CMS-1500 Item 14 / 837P DTP*454) for a new claim"
    ),
    "MA121": (
        "the x-ray date (CMS-1500 Item 19 / 837P DTP*455) when an x-ray demonstrated the subluxation, "
        "or confirmation that no x-ray date should be reported"
    ),
    "M122": "the level of subluxation (the M99.0x code) for each spinal region treated, from the note",
    "N29": "the missing documentation (orders, notes, summary, report or chart) the payer asked for",
    "N706": "the documentation the payer requested (initial evaluation, treatment plan and daily notes)",
    "N393": "the progress notes or report the payer requested",
    "N56": "the correct procedure code for the service and date of service billed",
    "M20": "the correct HCPCS code for the service billed, with the note sentence that documents it",
    "M51": "the correct procedure code for the service billed, with the note sentence that documents it",
    "M76": (
        "the complete, valid ICD-10 diagnosis code(s) for the service, coded to the highest specificity, "
        "with the note sentence that documents them"
    ),
    "M79": "the correct charge amount for the service",
    "M127": "the patient medical record for this service",
    "N179": "confirmation that the patient has answered the payer's request for additional information",
    "N225": "complete documentation (orders, notes, summary, report or chart) for the service",
}

MODIFIER_RARCS: frozenset[str] = frozenset({"N519", "M78", "N822", "N823"})
QUESTION_CARCS: frozenset[str] = frozenset({"125", "16", "206", "226", "227", "252"})
DRUG_CODE_PREFIXES: tuple[str, ...] = ("J", "Q", "S0", "C9")
# RARCs that say the claim is unprocessable: the fix is a new claim, never an appeal.
NEW_CLAIM_RARCS: frozenset[str] = frozenset({"MA130", "N704"})

# A note sentence with one of these says the treating (rendering) provider ordered the service, so
# the rendering NPI is the ordering provider the payer asks for (N264 / N265).
ORDERING_PROVIDER_NOTE_KEYWORDS: tuple[str, ...] = (
    "ordered by the treating",
    "ordered and taken by the treating",
    "treating chiropractor ordered",
    "treating provider ordered",
    "ordering provider is the rendering",
    "ordering provider is the treating",
)

# RARC -> words of a note sentence that already states what the remark asks for (the date, the
# level): the question quotes it so the clinic confirms instead of searching the chart again.
QUESTION_NOTE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "MA122": ("initial treatment date", "date of initial treatment", "initial examination", "start of care"),
    "MA121": ("x-ray date", "radiographs", "x-rays taken"),
    "M122": ("subluxation level", "level of subluxation", "subluxation at"),
}

FLAG_ORDERING_PROVIDER_FROM_NOTE = "ordering_provider_from_note"


class MissingInformationPlaybook(Playbook):
    category = DenialCategory.MISSING_INFORMATION

    def __init__(self) -> None:
        self.modifier_playbook = ModifierPlaybook()

    # ---- helpers ------------------------------------------------------------------------

    @staticmethod
    def rarcs_in_order(ctx: DenialContext) -> list[tuple[str, DenialReason]]:
        seen: set[str] = set()
        out: list[tuple[str, DenialReason]] = []
        for reason in ctx.reasons:
            for rarc in reason.rarcs:
                if rarc not in seen:
                    seen.add(rarc)
                    out.append((rarc, reason))
        for rarc in ctx.remittance.remark_codes:
            if rarc not in seen and ctx.reasons:
                seen.add(rarc)
                out.append((rarc, ctx.reasons[0]))
        return out

    def ndc_target_lines(self, ctx: DenialContext, reason: DenialReason) -> list[Any]:
        if reason.line_number is not None:
            line = self.line_by_number(ctx, reason.line_number)
            return [line] if line is not None else []
        drug_lines = [
            line for line in ctx.claim.lines if line.procedure_code.upper().startswith(DRUG_CODE_PREFIXES)
        ]
        return drug_lines or list(ctx.claim.lines)

    # ---- proposal -----------------------------------------------------------------------

    def propose(self, ctx: DenialContext, classification: ClassificationResult) -> ProposalDraft:
        rarcs = self.rarcs_in_order(ctx)
        field_hits = [(rarc, FIELD_RARCS[rarc], reason) for rarc, reason in rarcs if rarc in FIELD_RARCS]
        if field_hits:
            return self.field_fix(ctx, field_hits)
        if any(rarc in MODIFIER_RARCS for rarc, _ in rarcs):
            return self.modifier_playbook.propose(ctx, classification)
        question_hits = [(rarc, reason) for rarc, reason in rarcs if rarc in QUESTION_RARCS]
        if question_hits:
            return self.rarc_question(ctx, question_hits)
        return self.reason_question(ctx)

    def field_fix(self, ctx: DenialContext, hits: list[tuple[str, str, DenialReason]]) -> ProposalDraft:
        changes: list[CorrectedClaimChange] = []
        missing: list[str] = []
        citations: list[Citation] = list(self.cite_reasons(ctx))
        seen_fields: set[str] = set()
        flags_extra: list[str] = []
        for rarc, field, reason in hits:
            if field != "ndc" and field in seen_fields:
                continue  # two remarks for one field (N264 and N265) make one change
            seen_fields.add(field)
            refs = [f"{reason.group_code}-{reason.carc}", rarc]
            label = FIELD_LABELS[field]
            if field == "ndc":
                for line in self.ndc_target_lines(ctx, reason):
                    path = f"lines[{line.line_number}].ndc"
                    if line.ndc:
                        citations.append(self.cite_claim_field(ctx, path, line.ndc))
                        changes.append(
                            CorrectedClaimChange(
                                path=path,
                                before=line.ndc,
                                after=line.ndc,
                                reason=f"Resend the NDC for line {line.line_number} ({line.procedure_code}); RARC {rarc}: {rarc_text(reason, rarc)}",
                                coding_change=False,
                                citation_refs=[*refs, f"claim.{path}"],
                            )
                        )
                    else:
                        missing.append(f"{label} for line {line.line_number} ({line.procedure_code})")
                continue
            value = getattr(ctx.claim, field, None)
            if value:
                citations.append(self.cite_claim_field(ctx, field, value))
                changes.append(
                    CorrectedClaimChange(
                        path=field,
                        before=value,
                        after=value,
                        reason=f"Resend the {label} on the replacement claim; RARC {rarc}: {rarc_text(reason, rarc)}",
                        coding_change=False,
                        citation_refs=[*refs, f"claim.{field}"],
                    )
                )
                continue
            ordered = (
                self.find_supporting_notes(ctx, ORDERING_PROVIDER_NOTE_KEYWORDS, limit=1)
                if field == "referring_provider_npi" and ctx.claim.rendering_provider_npi
                else []
            )
            if ordered:
                # The note says the treating provider ordered the service: the rendering NPI is the
                # ordering provider (a chiropractor's own X-rays).
                note_cites = self.note_citations(ctx, ordered)
                rendering_cite = self.cite_claim_field(
                    ctx, "rendering_provider_npi", ctx.claim.rendering_provider_npi
                )
                citations.extend([*note_cites, rendering_cite])
                changes.append(
                    CorrectedClaimChange(
                        path=field,
                        before=None,
                        after=ctx.claim.rendering_provider_npi,
                        reason=(
                            f"Report the rendering provider as the {label}: the note says the treating provider "
                            f"ordered the service; RARC {rarc}: {rarc_text(reason, rarc)}"
                        ),
                        coding_change=False,
                        citation_refs=[*refs, *(c.ref for c in note_cites), rendering_cite.ref],
                    )
                )
                flags_extra.append(FLAG_ORDERING_PROVIDER_FROM_NOTE)
            else:
                missing.append(label)
        missing = list(dict.fromkeys(missing))
        question = (
            f"The payer reports missing or invalid information on claim {ctx.claim.claim_control_number} "
            f"(DOS {self.dos_label(ctx)}). Please supply: {'; '.join(missing)}."
            if missing
            else None
        )
        if changes:
            from_note = FLAG_ORDERING_PROVIDER_FROM_NOTE in flags_extra
            summary = (
                "Corrected claim reporting " if from_note else "Corrected claim resending "
            ) + ", ".join(FIELD_LABELS[f] for f in dict.fromkeys(field for _, field, _ in hits))
            if from_note:
                rationale = (
                    f"Denied for {self.reasons_summary(ctx)}. The claim carried no ordering provider; the note on file "
                    "says the treating provider ordered the service, so the rendering provider's NPI is reported as "
                    "the ordering provider. Not a coding change; the reviewer confirms the quoted note."
                )
            else:
                rationale = (
                    f"Denied for {self.reasons_summary(ctx)}. The claim record already holds the information the "
                    "payer reports as missing, so a replacement claim (frequency 7) resends it."
                )
            if any(rarc in NEW_CLAIM_RARCS for rarc, _ in self.rarcs_in_order(ctx)):
                rationale += (
                    " The payer returned the claim as unprocessable: it goes back as a new claim, and for a Medicare "
                    "contractor only the corrected line needs to be on it."
                )
            flags: list[str] = list(flags_extra)
            if missing:
                rationale += " Still missing from the record: " + "; ".join(missing) + "."
                flags.append("partial_information")
            return self.build(
                ctx,
                action_type=ActionType.CORRECTED_CLAIM,
                summary=summary,
                rationale=rationale,
                citations=dedupe_citations(citations),
                corrected_claim=build_corrected_claim(ctx, changes),
                clinic_question=question,
                confidence=0.75 if not missing else 0.6,
                flags=flags,
            )
        return self.build(
            ctx,
            action_type=ActionType.CLINIC_QUESTION,
            summary="Ask the clinic for: " + "; ".join(missing),
            rationale=f"Denied for {self.reasons_summary(ctx)}. The claim record does not hold the information the payer asks for.",
            citations=dedupe_citations(citations),
            clinic_question=question,
            confidence=0.7,
        )

    def rarc_question(self, ctx: DenialContext, hits: list[tuple[str, DenialReason]]) -> ProposalDraft:
        needs = []
        quoted = []
        on_file: list[tuple[str, tuple[NoteContext, str]]] = []
        for rarc, reason in hits:
            quoted.append(f'RARC {rarc}: "{rarc_text(reason, rarc)}"')
            needs.append(QUESTION_RARCS[rarc])
            keywords = QUESTION_NOTE_KEYWORDS.get(rarc)
            if keywords:
                on_file.extend((rarc, s) for s in self.find_supporting_notes(ctx, keywords, limit=1))
        question = (
            f"Claim {ctx.claim.claim_control_number} (DOS {self.dos_label(ctx)}) was denied with "
            + "; ".join(quoted)
            + ". Needed from the clinic: "
            + "; ".join(dict.fromkeys(needs))
            + "."
        )
        citations = list(self.cite_reasons(ctx))
        if on_file:
            question += " The note on file states: " + " ".join(
                f'"{sentence}"' for _, (_, sentence) in on_file
            )
            question += " Please confirm it so it can be reported"
            if any(rarc in NEW_CLAIM_RARCS for rarc, _ in self.rarcs_in_order(ctx)):
                question += " on a new claim (the payer returned this one as unprocessable)"
            question += "."
            citations.extend(self.note_citations(ctx, [support for _, support in on_file]))
        return self.build(
            ctx,
            action_type=ActionType.CLINIC_QUESTION,
            summary="Ask the clinic for the information the payer requested ("
            + ", ".join(r for r, _ in hits)
            + ")",
            rationale=(
                f"Denied for {self.reasons_summary(ctx)}. The remark code names information the claim export cannot "
                "carry, so the clinic supplies it"
                + (" (the note on file already states it and is quoted)." if on_file else ".")
            ),
            citations=dedupe_citations(citations),
            clinic_question=question,
            confidence=0.75 if on_file else 0.7,
        )

    def reason_question(self, ctx: DenialContext) -> ProposalDraft:
        quoted = "; ".join(f'{reason_label(r)}: "{reason_text(r)}"' for r in ctx.reasons) or "no reason code"
        question = (
            f"Claim {ctx.claim.claim_control_number} (DOS {self.dos_label(ctx)}) was denied for {quoted}. "
            "No remark code identifies the missing element. Please check the payer's rejection detail or portal "
            "and tell us which claim element is missing or invalid so a corrected claim can be prepared."
        )
        return self.build(
            ctx,
            action_type=ActionType.CLINIC_QUESTION,
            summary="Ask the clinic which claim element the payer found missing or invalid",
            rationale=f"Denied for {self.reasons_summary(ctx)}. Without a remark code the missing element cannot be derived from the claim record.",
            citations=self.cite_reasons(ctx),
            clinic_question=question,
            confidence=0.5,
        )


__all__ = [
    "FIELD_RARCS",
    "FLAG_ORDERING_PROVIDER_FROM_NOTE",
    "MODIFIER_RARCS",
    "NEW_CLAIM_RARCS",
    "ORDERING_PROVIDER_NOTE_KEYWORDS",
    "QUESTION_CARCS",
    "QUESTION_NOTE_KEYWORDS",
    "QUESTION_RARCS",
    "MissingInformationPlaybook",
]
