"""Eligibility and coordination-of-benefits playbooks.

Both ask the clinic a question (for a work, auto or liability injury, CARC 19 / 20 / 21, the
question asks for that carrier, its claim number and the date of injury). When a payer document
names the payer that should be billed ("bill <Name>", "primary payer: <Name>"), a claim to that payer
is proposed instead, flagged `payer_from_document` and `payer_change_unverified` (the X12 payer id and
member id must be confirmed by the clinic). The named payer never received the original claim, so
the proposal is a new original claim (frequency 1, no original-claim reference) filed under that
payer's own rules, not a replacement; lines carrying a modifier set for the first payer's rules
(`payer_specific_modifiers` in modifier_rules.yaml: GY, GA, GZ, AT, GP, ...) are flagged
`payer_specific_modifiers` for the reviewer to re-derive. The document is untrusted data: only a
payer name is extracted from it, never an instruction, and the extraction is a bounded regular
expression that stops at the first lowercase word or punctuation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.drafting.corrected_claim import build_corrected_claim
from app.enums import ActionType, DenialCategory
from app.playbooks.base import (
    FLAG_PAYER_CHANGE_UNVERIFIED,
    FLAG_PAYER_FROM_DOCUMENT,
    Playbook,
    dedupe_citations,
    normalize_ws,
)
from app.playbooks.modifier import load_modifier_rules
from app.schemas import (
    ClassificationResult,
    CorrectedClaim,
    CorrectedClaimChange,
    DenialContext,
    DocumentContext,
    ProposalDraft,
)

FLAG_NEW_CLAIM_TO_OTHER_PAYER = "new_claim_to_other_payer"
FLAG_PAYER_SPECIFIC_MODIFIERS = "payer_specific_modifiers"
FLAG_ACCIDENT_CARRIER = "accident_carrier"
# CARC -> the carrier a work, liability or no-fault injury redirects the claim to.
ACCIDENT_CARRIERS: dict[str, str] = {
    "19": "workers' compensation",
    "20": "liability",
    "21": "no-fault (auto / personal injury protection)",
}

# A payer name: capitalized (or all-caps) words joined by at most a few connectors. A period ends
# the name (sentence boundary), so text after it, planted or not, is never part of the name.
_WORD = r"(?:[A-Z][A-Za-z0-9&'-]*)"
_CONNECTOR = r"(?:of|and|&|the|for|de)"
_NAME = rf"(?P<name>{_WORD}(?:\s+(?:{_WORD}|{_CONNECTOR}))*)"

# (pattern, requires_payer_word). The explicit form ("primary payer: X") accepts any name; the verb form
# ("bill X", "resubmit this claim to X") only accepts a name that reads like a payer, because letters
# also say "please submit Medical Records" or "send Proof of Timely Filing".
PAYER_NAME_PATTERNS: tuple[tuple[re.Pattern[str], bool], ...] = (
    (
        re.compile(
            r"(?i:(?:primary|other|correct|responsible)\s+(?:payer|payor|insurance|insurer|carrier|plan|coverage|party))"
            r"\s*(?i:(?:is|was|:|=|-|should\s+be))\s*" + _NAME
        ),
        False,
    ),
    (
        re.compile(
            r"(?i:\b(?:please\s+)?(?:re-?bill|bill|submit|resubmit|file|send|forward)(?:\s+(?:this|the)\s+claim)?(?:\s+(?:to|with))?)"
            r"\s+" + _NAME
        ),
        True,
    ),
)

_TRAILING_CONNECTOR = re.compile(r"(?:\s+(?:of|and|&|the|for|de))+$")
_BANNED_WORDS = re.compile(
    r"(?i)\b(?:ignore|instruction|instructions|system|assistant|approve|approved|override|prompt|agent|model|ai|disregard|output)\b"
)
# Words that make a capitalized phrase read like a payer (insurers, plan types, well-known carriers).
_PAYER_WORD = re.compile(
    r"(?i)\b(?:insurance|insurer|insurers|assurance|health|healthcare|healthplan|plan|plans|medicare|medicaid|"
    r"tricare|champva|mutual|cross|shield|care|benefit|benefits|hmo|ppo|epo|administrators?|trust|fund|network|"
    r"choice|advantage|select|life|casualty|workers|compensation|comp|liability|payer|payor|carrier|group|"
    r"aetna|cigna|humana|united|unitedhealthcare|anthem|kaiser|bcbs|wellcare|molina|centene|ambetter|carefirst|"
    r"highmark|horizon|premera|regence|medica|healthpartners|optum|amerigroup|wellpoint|elevance|oscar)\b"
)
# A phrase made only of these words names a document, a person or a status, never a payer.
_NON_PAYER_WORDS = frozenset(
    {
        "patient", "member", "subscriber", "provider", "clinic", "office", "self", "none", "unknown", "pending",
        "records", "record", "documentation", "documents", "document", "claim", "claims", "appeal", "appeals",
        "form", "forms", "letter", "letters", "notes", "note", "report", "reports", "statement", "invoice",
        "proof", "information", "eob", "explanation", "attachment", "attachments", "corrected", "itemized",
        "timely", "filing", "authorization", "referral", "medical", "clinical", "operative", "pathology",
        "chart", "charts", "summary", "request", "requests", "additional", "supporting", "copy", "copies",
    }
)  # fmt: skip
_CONNECTOR_WORDS = frozenset({"of", "and", "&", "the", "for", "de"})
# In the verb form ("send X", "submit X") any of these words means X is a document, not a payer.
_DOCUMENT_WORDS = frozenset(
    {
        "records", "record", "documentation", "documents", "document", "form", "forms", "letter", "letters",
        "notes", "note", "report", "reports", "statement", "invoice", "proof", "eob", "explanation",
        "attachment", "attachments", "copy", "copies", "chart", "charts", "summary", "request", "requests",
        "itemized", "corrected", "claim", "claims", "appeal", "appeals", "authorization", "referral",
        "timely", "filing",
    }
)  # fmt: skip
MAX_NAME_LENGTH = 60
MAX_NAME_WORDS = 8


@dataclass(frozen=True)
class PayerNameHit:
    name: str
    document: DocumentContext
    index: int
    snippet: str


def _clean_name(raw: str, *, requires_payer_word: bool = False) -> str | None:
    name = _TRAILING_CONNECTOR.sub("", normalize_ws(raw)).strip(" .,;:")
    if len(name) < 3 or len(name) > MAX_NAME_LENGTH:
        return None
    if _BANNED_WORDS.search(name):
        return None
    if not re.search(r"[A-Za-z]{2}", name):
        return None
    words = [w.lower().strip("'&-") for w in name.split()]
    if len(words) > MAX_NAME_WORDS:
        return None
    if all((not w) or w in _NON_PAYER_WORDS or w in _CONNECTOR_WORDS for w in words):
        return None
    if requires_payer_word and (not _PAYER_WORD.search(name) or any(w in _DOCUMENT_WORDS for w in words)):
        return None
    return name


def extract_payer_name(documents: list[DocumentContext]) -> PayerNameHit | None:
    """The first payer name a document names, or None. Only a name is extracted; the text is data."""
    for index, doc in enumerate(documents):
        text = normalize_ws(doc.text or "")
        for pattern, requires_payer_word in PAYER_NAME_PATTERNS:
            for match in pattern.finditer(text):
                name = _clean_name(match.group("name"), requires_payer_word=requires_payer_word)
                if name:
                    snippet = normalize_ws(match.group(0))[:120]
                    return PayerNameHit(name=name, document=doc, index=index, snippet=snippet)
    return None


class _CoveragePlaybook(Playbook):
    summary_question: str = "Ask the clinic to verify coverage"

    def question(self, ctx: DenialContext) -> str:
        raise NotImplementedError

    def propose(self, ctx: DenialContext, classification: ClassificationResult) -> ProposalDraft:
        hit = extract_payer_name(ctx.documents)
        if hit is not None and hit.name.strip().lower() != (ctx.claim.payer_name or "").strip().lower():
            return self.corrected_claim_to_payer(ctx, hit)
        return self.build(
            ctx,
            action_type=ActionType.CLINIC_QUESTION,
            summary=self.summary_question,
            rationale=(
                f"Denied for {self.reasons_summary(ctx)}. The payer did not name another payer in a document on file, "
                "so coverage must be verified by the clinic before anything is resubmitted."
            ),
            citations=[
                *self.cite_reasons(ctx),
                self.cite_claim_field(ctx, "payer_name", ctx.claim.payer_name),
                self.cite_claim_field(ctx, "service_date_from", ctx.claim.service_date_from),
            ],
            clinic_question=self.question(ctx),
            confidence=0.6,
        )

    def corrected_claim_to_payer(self, ctx: DenialContext, hit: PayerNameHit) -> ProposalDraft:
        doc_cite = self.cite_document(hit.document, hit.snippet, hit.index)
        citations = [
            *self.cite_reasons(ctx),
            doc_cite,
            self.cite_claim_field(ctx, "payer_name", ctx.claim.payer_name),
            self.cite_claim_field(ctx, "payer_id", ctx.claim.payer_id),
        ]
        refs = [c.ref for c in citations]
        changes = [
            CorrectedClaimChange(
                path="payer_name",
                before=ctx.claim.payer_name,
                after=hit.name,
                reason=f"Payer document names {hit.name} as the payer to bill",
                coding_change=False,
                citation_refs=refs,
            ),
            CorrectedClaimChange(
                path="payer_id",
                before=ctx.claim.payer_id,
                after=None,
                reason=f"X12 payer id for {hit.name} must be confirmed by the clinic before export",
                coding_change=False,
                citation_refs=refs,
            ),
        ]
        payer_specific = {str(m).upper() for m in load_modifier_rules().get("payer_specific_modifiers", [])}
        held = [
            (line, [m for m in line.modifiers if str(m).strip().upper() in payer_specific])
            for line in ctx.claim.lines
        ]
        held = [(line, mods) for line, mods in held if mods]
        for line, _mods in held:
            citations.append(
                self.cite_claim_field(ctx, f"lines[{line.line_number}].modifiers", line.modifiers)
            )
        question = (
            f"A payer document names {hit.name} as the payer for claim {ctx.claim.claim_control_number} "
            f"(DOS {self.dos_label(ctx)}). Please confirm the patient's coverage with {hit.name} on the date of service, "
            f"the member id, and the X12 payer id to use on the new claim, which is filed under {hit.name}'s own "
            "timely-filing limit."
        )
        flags = [FLAG_PAYER_FROM_DOCUMENT, FLAG_PAYER_CHANGE_UNVERIFIED, FLAG_NEW_CLAIM_TO_OTHER_PAYER]
        if held:
            listed = "; ".join(
                f"line {line.line_number} ({line.procedure_code}: {', '.join(mods)})" for line, mods in held
            )
            question += (
                f" These modifiers were set for {ctx.claim.payer_name}'s rules and must be checked against "
                f"{hit.name}'s before export: {listed}."
            )
            flags.append(FLAG_PAYER_SPECIFIC_MODIFIERS)
        return self.build(
            ctx,
            action_type=ActionType.CORRECTED_CLAIM,
            summary=f"New claim to {hit.name}, the payer named in the payer document",
            rationale=(
                f"Denied for {self.reasons_summary(ctx)}. The payer document names {hit.name} as the payer to bill. "
                f"{hit.name} never received the original claim, so this is a new original claim (frequency 1, no "
                "original-claim reference) addressed to that payer, not a replacement. Not a coding change. The payer "
                "id and member id are unverified until the clinic confirms them."
                + (" Payer-specific modifiers are flagged for review." if held else "")
            ),
            citations=dedupe_citations(citations),
            corrected_claim=as_new_claim(build_corrected_claim(ctx, changes)),
            clinic_question=question,
            confidence=0.5,
            flags=flags,
        )


def as_new_claim(corrected: CorrectedClaim) -> CorrectedClaim:
    """The same edits as a new original claim (frequency 1, no original-claim reference): what a
    payer that never received the first claim needs."""
    after = dict(corrected.after)
    after["claim_frequency_code"] = "1"
    after["original_claim_control_number"] = None
    return corrected.model_copy(update={"after": after})


class EligibilityPlaybook(_CoveragePlaybook):
    category = DenialCategory.ELIGIBILITY
    summary_question = (
        "Ask the clinic to verify eligibility on the date of service and the correct payer and member id"
    )

    def question(self, ctx: DenialContext) -> str:
        return (
            f"Claim {ctx.claim.claim_control_number} was denied for eligibility ({self.reasons_summary(ctx)}). "
            f"Please verify the patient's coverage with {ctx.claim.payer_name} on {self.dos_label(ctx)} "
            "(eligibility check or card copy), confirm the member id and plan, and tell us whether another payer "
            "was active on that date so a corrected claim can be prepared."
        )


class CoordinationOfBenefitsPlaybook(_CoveragePlaybook):
    category = DenialCategory.COORDINATION_OF_BENEFITS
    summary_question = "Ask the clinic for the patient's other-coverage information"

    @staticmethod
    def accident_carrier(ctx: DenialContext) -> str | None:
        """The carrier a work, liability or no-fault injury redirects the claim to (CARC 19 / 20 / 21)."""
        for reason in ctx.reasons:
            if reason.carc in ACCIDENT_CARRIERS:
                return ACCIDENT_CARRIERS[reason.carc]
        return None

    def propose(self, ctx: DenialContext, classification: ClassificationResult) -> ProposalDraft:
        draft = super().propose(ctx, classification)
        if self.accident_carrier(ctx) and draft.action_type == ActionType.CLINIC_QUESTION:
            draft.summary = "Ask the clinic for the accident carrier, its claim number and the date of injury"
            draft.flags = [*draft.flags, FLAG_ACCIDENT_CARRIER]
        return draft

    def question(self, ctx: DenialContext) -> str:
        carrier = self.accident_carrier(ctx)
        if carrier:
            return (
                f"Claim {ctx.claim.claim_control_number} (DOS {self.dos_label(ctx)}) was denied because the plan holds a "
                f"{carrier} carrier liable for this injury ({self.reasons_summary(ctx)}). Please send the accident "
                f"intake: the {carrier} carrier's name, its claim number, the date of injury and the adjuster or "
                "employer contact on file, so the claim can be billed to that carrier; if that carrier has denied the "
                "claim or the benefits are exhausted, send its denial or exhaustion letter for an appeal to this plan; "
                "if the condition is not related to the accident, say so and the history of onset will be quoted in an "
                "appeal."
            )
        return (
            f"Claim {ctx.claim.claim_control_number} was denied for coordination of benefits ({self.reasons_summary(ctx)}). "
            f"Please obtain the patient's other-coverage information for {self.dos_label(ctx)}: the primary payer's name, "
            "member id and effective dates (or the primary payer's remittance if it already paid), so the claim can be "
            "sent to the primary payer or resubmitted with the primary payment details."
        )


__all__ = [
    "ACCIDENT_CARRIERS",
    "CoordinationOfBenefitsPlaybook",
    "EligibilityPlaybook",
    "FLAG_ACCIDENT_CARRIER",
    "FLAG_NEW_CLAIM_TO_OTHER_PAYER",
    "FLAG_PAYER_SPECIFIC_MODIFIERS",
    "PAYER_NAME_PATTERNS",
    "PayerNameHit",
    "as_new_claim",
    "extract_payer_name",
]
