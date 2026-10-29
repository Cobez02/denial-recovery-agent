"""Medical-necessity playbook (CARC 50 / 56 / 167 / 11 / 150 / 151, RARC N115 / M76 / N661).

- `corrected_claim` (coding change, flagged) when a note mentions an ICD-10 code that is absent from
  the claim: the documented diagnosis is added to `diagnosis_codes`.
- otherwise `appeal` quoting the note sentences that mention the billed diagnosis or its symptoms
  (keyword list per diagnosis family below, plus the codes themselves).
- `clinic_question` when no note excerpt is on file at all.
"""

from __future__ import annotations

import re

from app.drafting.corrected_claim import build_corrected_claim
from app.enums import ActionType, DenialCategory
from app.playbooks.base import FLAG_NO_NOTES, Playbook, dedupe_citations, latest_note, split_sentences
from app.schemas import ClassificationResult, CorrectedClaimChange, DenialContext, NoteContext, ProposalDraft

FLAG_DIAGNOSIS_FROM_NOTES = "diagnosis_added_from_notes"
FLAG_WEAK_NOTE_SUPPORT = "weak_note_support"

# ICD-10-CM codes written with a subcategory (L57.0, C44.319, U07.1). Three-character categories
# alone are not matched: too many false positives in free text (vitamin B12, room E12).
ICD10_IN_TEXT = re.compile(r"(?<![A-Za-z0-9])([A-Z][0-9]{2}\.[0-9A-Z]{1,4})(?![A-Za-z0-9])")


def _code_key(code: str) -> str:
    """Comparison key for an ICD-10 code: upper case, no dot (L57.0 and L570 are the same code)."""
    return code.strip().upper().replace(".", "")


# Keywords per diagnosis family (ICD-10 category prefixes), dermatology profile.
DIAGNOSIS_FAMILIES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (
        ("L57",),
        (
            "actinic",
            "keratosis",
            "keratoses",
            "premalignant",
            "scaly",
            "sun-damaged",
            "sun damaged",
            "rough",
            "lesion",
            "lesions",
        ),
    ),
    (
        ("L82",),
        (
            "seborrheic",
            "keratosis",
            "keratoses",
            "irritated",
            "inflamed",
            "bleeding",
            "itching",
            "lesion",
            "lesions",
        ),
    ),
    (
        ("L40",),
        (
            "psoriasis",
            "psoriatic",
            "plaque",
            "plaques",
            "scaling",
            "erythematous",
            "body surface",
            "bsa",
            "phototherapy",
            "failed topical",
        ),
    ),
    (
        ("L70",),
        (
            "acne",
            "comedones",
            "cystic",
            "nodules",
            "nodular",
            "scarring",
            "isotretinoin",
            "papules",
            "pustules",
        ),
    ),
    (
        ("L20", "L30", "L23", "L24", "L25"),
        ("eczema", "dermatitis", "pruritus", "itching", "itchy", "flare", "excoriation", "erythema"),
    ),
    (
        ("D22", "D23", "D48", "D49"),
        (
            "nevus",
            "nevi",
            "mole",
            "changing",
            "irregular",
            "asymmetric",
            "atypical",
            "dysplastic",
            "pigmented",
            "suspicious",
            "uncertain",
            "rule out",
        ),
    ),
    (
        ("C43", "C44", "D03", "D04"),
        (
            "melanoma",
            "carcinoma",
            "basal cell",
            "squamous",
            "malignant",
            "malignancy",
            "cancer",
            "mohs",
            "margins",
            "tumor",
        ),
    ),
    (("L72",), ("cyst", "cysts", "inflamed", "tender", "drainage", "infected", "ruptured")),
    (("L90", "L91"), ("keloid", "scar", "hypertrophic", "painful")),
    (("B07",), ("wart", "warts", "verruca", "verrucae", "plantar", "painful", "spreading")),
    (
        ("L03", "L08", "L02"),
        ("cellulitis", "infection", "infected", "abscess", "erythema", "warmth", "purulent"),
    ),
    (("L28", "L29"), ("pruritus", "itching", "itch", "prurigo")),
    (("L50",), ("urticaria", "hives", "wheals", "angioedema")),
    (("L63", "L64", "L65"), ("alopecia", "hair loss", "shedding", "thinning")),
    (("L73", "L74"), ("hidradenitis", "folliculitis", "hyperhidrosis", "sweating")),
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
)


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
        """(code, note, sentence) for every dotted ICD-10 code in the notes that the claim lacks."""
        on_claim = {_code_key(c) for c in ctx.claim.diagnosis_codes if c}
        found: list[tuple[str, NoteContext, str]] = []
        seen: set[str] = set()
        for note in ctx.notes:
            for sentence in split_sentences(note.text):
                for code in ICD10_IN_TEXT.findall(sentence):
                    code = code.upper()
                    key = _code_key(code)
                    if key not in on_claim and key not in seen:
                        seen.add(key)
                        found.append((code, note, sentence))
        return found

    # ---- proposal -----------------------------------------------------------------------

    def propose(self, ctx: DenialContext, classification: ClassificationResult) -> ProposalDraft:
        dx = ", ".join(ctx.claim.diagnosis_codes) or "the billed diagnosis"
        if not ctx.notes:
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

        missing = self.documented_codes_missing(ctx)
        if missing:
            support = [(note, sentence) for _, note, sentence in missing]
            note_cites = self.note_citations(ctx, support)
            field_cite = self.cite_claim_field(ctx, "diagnosis_codes", ctx.claim.diagnosis_codes)
            citations = [*self.cite_reasons(ctx), field_cite, *note_cites]
            new_codes = [code for code, _, _ in missing]
            change = CorrectedClaimChange(
                path="diagnosis_codes",
                before=list(ctx.claim.diagnosis_codes),
                after=[*ctx.claim.diagnosis_codes, *new_codes],
                reason="Add the diagnosis code(s) documented in the notes but absent from the claim: "
                + ", ".join(new_codes),
                coding_change=True,
                citation_refs=[c.ref for c in citations],
            )
            return self.build(
                ctx,
                action_type=ActionType.CORRECTED_CLAIM,
                summary="Corrected claim adding documented diagnosis code(s) " + ", ".join(new_codes),
                rationale=(
                    f"Denied for {self.reasons_summary(ctx)}. The notes document {', '.join(new_codes)}, which the "
                    "claim does not carry. Adding a diagnosis is a coding change; the quoted note sentence must be "
                    "confirmed before approval."
                ),
                citations=dedupe_citations(citations),
                corrected_claim=build_corrected_claim(ctx, [change]),
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
            assert latest is not None  # ctx.notes is non-empty here
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
