"""Playbook base class and the citation helpers every category playbook shares.

A playbook turns a `DenialContext` plus its `ClassificationResult` into a `ProposalDraft`. Every
draft it returns:

- cites the CARC/RARC it relies on and every claim field, note sentence or payer document it used
  (`citations`, never empty: `build()` falls back to the reason citations);
- carries a deadline and deadline kind from `app.playbooks.deadlines`;
- marks any code or modifier edit as a coding change that needs a quoted note excerpt before approval.

Notes and payer documents are untrusted data. The helpers here only quote sentences that contain the
keywords a playbook looks for; nothing in a note or letter is ever treated as an instruction.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any

from app.drafting.corrected_claim import claim_to_export_dict, resolve_path
from app.enums import ActionType, CitationKind, DenialCategory
from app.playbooks.deadlines import deadline_for
from app.schemas import (
    Citation,
    ClassificationResult,
    CorrectedClaim,
    DenialContext,
    DenialReason,
    DocumentContext,
    NoteContext,
    ProposalDraft,
)

# Sentence boundary: terminal punctuation followed by whitespace and a capital, digit or quote, or a
# line break. Common abbreviations (Dr., Mr., Ms., Mrs., vs., e.g., i.e.) do not end a sentence, so a
# quoted excerpt is never cut off at "Dr.".
_SENTENCE_SPLIT = re.compile(
    r"(?:(?<!\bDr\.)(?<!\bMr\.)(?<!\bMs\.)(?<!\bMrs\.)(?<!\bvs\.)(?<!\be\.g\.)(?<!\bi\.e\.)"
    r"(?<=[.!?])\s+(?=[\"'(\[A-Z0-9]))|\n+"
)
_WS = re.compile(r"\s+")

# Flags shared by the playbooks.
FLAG_CODING_CHANGE = "coding_change"
FLAG_UNSUPPORTED_CODING_CHANGE = "unsupported_coding_change"
FLAG_MISSED_MONEY_REVIEW = "missed_money_review"
FLAG_PAYER_FROM_DOCUMENT = "payer_from_document"
FLAG_PAYER_CHANGE_UNVERIFIED = "payer_change_unverified"
FLAG_CONTRACTUAL_BUNDLING = "contractual_bundling"
FLAG_NO_NOTES = "no_note_excerpts"


# --------------------------------------------------------------------------------------
# Text helpers
# --------------------------------------------------------------------------------------


def normalize_ws(text: str) -> str:
    return _WS.sub(" ", text).strip()


def split_sentences(text: str) -> list[str]:
    """Split a note into sentences (whitespace-normalized, empty pieces dropped)."""
    return [normalize_ws(part) for part in _SENTENCE_SPLIT.split(text or "") if normalize_ws(part)]


def keyword_pattern(keyword: str) -> re.Pattern[str]:
    """Case-insensitive match of a keyword or phrase on word boundaries."""
    return re.compile(r"(?<!\w)" + re.escape(keyword.strip()) + r"(?!\w)", re.IGNORECASE)


def sentence_matches(sentence: str, keywords: Iterable[str]) -> list[str]:
    """The keywords (as given) that appear in the sentence."""
    return [kw for kw in keywords if kw and keyword_pattern(kw).search(sentence)]


def notes_mention(ctx: DenialContext, keywords: Iterable[str]) -> bool:
    kws = list(keywords)
    return any(
        sentence_matches(sentence, kws) for note in ctx.notes for sentence in split_sentences(note.text)
    )


def latest_note(ctx: DenialContext) -> NoteContext | None:
    """The most recently dated note; undated notes rank last (first one in context order wins)."""
    if not ctx.notes:
        return None
    dated = [n for n in ctx.notes if n.note_date is not None]
    if dated:
        return max(dated, key=lambda n: n.note_date)  # type: ignore[arg-type, return-value]
    return ctx.notes[0]


def code_is_numeric_in(code: str, ranges: Sequence[Sequence[int]]) -> bool:
    if not code or not code.isdigit():
        return False
    value = int(code)
    return any(int(lo) <= value <= int(hi) for lo, hi in ranges)


# --------------------------------------------------------------------------------------
# Citation helpers (module level so tests and other modules can reuse them)
# --------------------------------------------------------------------------------------


def reason_ref(reason: DenialReason) -> str:
    return f"{reason.group_code}-{reason.carc}" if reason.group_code else reason.carc


def reason_text(reason: DenialReason) -> str:
    return reason.carc_text or f"CARC {reason.carc}"


def rarc_text(reason: DenialReason, rarc: str) -> str:
    return reason.rarc_texts.get(rarc) or f"RARC {rarc}"


def reason_label(reason: DenialReason) -> str:
    """'CARC CO-50 (line 2, 17000)' style label for summaries."""
    label = f"CARC {reason_ref(reason)}"
    if reason.line_number is not None:
        label += f" (line {reason.line_number}"
        if reason.procedure_code:
            label += f", {reason.procedure_code}"
        label += ")"
    return label


def note_ref(note: NoteContext, index: int | None = None) -> str:
    if note.note_id is not None:
        return f"note:{note.note_id}"
    return f"note:{index if index is not None else 0}"


def document_ref(doc: DocumentContext, index: int | None = None) -> str:
    if doc.document_id is not None:
        return f"document:{doc.document_id}"
    return f"document:{index if index is not None else 0}"


def document_for_ref(ctx: DenialContext, ref: str) -> DocumentContext | None:
    """The document a 'document:<id or index>' ref points at, or None."""
    key = ref.split(":", 1)[1] if ref.startswith("document:") else ref
    for doc in ctx.documents:
        if doc.document_id is not None and str(doc.document_id) == key:
            return doc
    if key.isdigit() and all(doc.document_id is None for doc in ctx.documents):
        index = int(key)
        return ctx.documents[index] if 0 <= index < len(ctx.documents) else None
    return None


def dedupe_citations(citations: Iterable[Citation]) -> list[Citation]:
    seen: set[tuple[str, str, str]] = set()
    out: list[Citation] = []
    for c in citations:
        key = (str(c.kind), c.ref, c.text)
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def _format_value(value: Any) -> str:
    if value is None:
        return "(empty)"
    if isinstance(value, list | tuple):
        return ", ".join(str(v) for v in value) if value else "(none)"
    return str(value)


def citation_resolves(ctx: DenialContext, citation: Citation) -> bool:
    """True when the citation points at something that exists in the context (SPEC M6 done-when)."""
    kind = CitationKind(citation.kind)
    if kind == CitationKind.CARC:
        return any(citation.ref in (reason_ref(r), r.carc) for r in ctx.reasons)
    if kind == CitationKind.RARC:
        codes = {rc for r in ctx.reasons for rc in r.rarcs} | set(ctx.remittance.remark_codes)
        return citation.ref in codes
    if kind == CitationKind.CLAIM_FIELD:
        path = citation.ref[len("claim.") :] if citation.ref.startswith("claim.") else citation.ref
        try:
            resolve_path(claim_to_export_dict(ctx.claim), path)
        except ValueError:
            return path in claim_to_export_dict(ctx.claim)
        return True
    if kind == CitationKind.NOTE_EXCERPT:
        quoted = normalize_ws(citation.text)
        return any(quoted and quoted in normalize_ws(note.text) for note in ctx.notes)
    if kind == CitationKind.PAYER_DOCUMENT:
        # Resolves when the quoted snippet is in a document on file, or when the ref names one
        # (a citation of the document as a whole carries a label, not a quote).
        quoted = normalize_ws(citation.text)
        if any(quoted and quoted in normalize_ws(doc.text) for doc in ctx.documents):
            return True
        return document_for_ref(ctx, citation.ref) is not None
    if kind == CitationKind.PAYER_POLICY:
        attr = citation.ref[len("payer.") :] if citation.ref.startswith("payer.") else citation.ref
        return hasattr(ctx.payer, attr)
    if kind == CitationKind.REMITTANCE_FIELD:
        attr = citation.ref[len("remittance.") :] if citation.ref.startswith("remittance.") else citation.ref
        return hasattr(ctx.remittance, attr)
    return False


# --------------------------------------------------------------------------------------
# Playbook
# --------------------------------------------------------------------------------------


class Playbook:
    """Base class. Subclasses set `category` and implement `propose`."""

    category: DenialCategory = DenialCategory.OTHER

    def propose(self, ctx: DenialContext, classification: ClassificationResult) -> ProposalDraft:
        raise NotImplementedError

    # ---- citations -----------------------------------------------------------------

    def cite_reasons(
        self, ctx: DenialContext, reasons: Iterable[DenialReason] | None = None
    ) -> list[Citation]:
        """One CARC citation per reason (text from the catalog or the code itself) and one RARC citation
        per remark code."""
        out: list[Citation] = []
        for reason in reasons if reasons is not None else ctx.reasons:
            out.append(Citation(kind=CitationKind.CARC, ref=reason_ref(reason), text=reason_text(reason)))
            for rarc in reason.rarcs:
                out.append(Citation(kind=CitationKind.RARC, ref=rarc, text=rarc_text(reason, rarc)))
        return dedupe_citations(out)

    def cite_claim_field(self, ctx: DenialContext, path: str, value: Any) -> Citation:
        return Citation(
            kind=CitationKind.CLAIM_FIELD, ref=f"claim.{path}", text=f"{path} = {_format_value(value)}"
        )

    def cite_note(self, note: NoteContext, sentence: str, index: int | None = None) -> Citation:
        return Citation(
            kind=CitationKind.NOTE_EXCERPT, ref=note_ref(note, index), text=normalize_ws(sentence)
        )

    def cite_document(
        self, doc: DocumentContext, text: str | None = None, index: int | None = None
    ) -> Citation:
        snippet = (
            normalize_ws(text)
            if text
            else f"{doc.document_type} received {doc.received_date or 'on an unknown date'}"
        )
        return Citation(kind=CitationKind.PAYER_DOCUMENT, ref=document_ref(doc, index), text=snippet)

    def cite_payer_window(self, ctx: DenialContext, attr: str) -> Citation:
        days = getattr(ctx.payer, attr)
        label = attr.replace("_", " ")
        return Citation(
            kind=CitationKind.PAYER_POLICY, ref=f"payer.{attr}", text=f"{ctx.payer.name} {label}: {days}"
        )

    def cite_remittance_field(self, ctx: DenialContext, attr: str) -> Citation:
        return Citation(
            kind=CitationKind.REMITTANCE_FIELD,
            ref=f"remittance.{attr}",
            text=f"{attr} = {_format_value(getattr(ctx.remittance, attr, None))}",
        )

    def find_supporting_notes(
        self, ctx: DenialContext, keywords: Iterable[str], *, limit: int | None = None
    ) -> list[tuple[NoteContext, str]]:
        """(note, sentence) for every note sentence that contains one of the keywords, in note order."""
        kws = [k for k in keywords if k]
        found: list[tuple[NoteContext, str]] = []
        for note in ctx.notes:
            for sentence in split_sentences(note.text):
                if sentence_matches(sentence, kws):
                    found.append((note, sentence))
                    if limit is not None and len(found) >= limit:
                        return found
        return found

    def note_citations(
        self, ctx: DenialContext, support: Iterable[tuple[NoteContext, str]]
    ) -> list[Citation]:
        out: list[Citation] = []
        for note, sentence in support:
            index = next((i for i, n in enumerate(ctx.notes) if n is note), None)
            out.append(self.cite_note(note, sentence, index))
        return dedupe_citations(out)

    # ---- drafts --------------------------------------------------------------------

    def build(
        self,
        ctx: DenialContext,
        *,
        action_type: ActionType,
        summary: str,
        rationale: str,
        citations: Iterable[Citation] = (),
        corrected_claim: CorrectedClaim | None = None,
        clinic_question: str | None = None,
        coding_change: bool = False,
        requires_note_excerpt: bool | None = None,
        confidence: float = 0.5,
        flags: Iterable[str] = (),
    ) -> ProposalDraft:
        """Assemble a draft with the invariants every proposal must satisfy."""
        cited = dedupe_citations(citations) or self.cite_reasons(ctx)
        if not cited:
            cited = [self.cite_remittance_field(ctx, "claim_status_code")]
        deadline, kind = deadline_for(action_type, ctx)
        flag_list = list(dict.fromkeys(flags))
        if coding_change:
            requires_note_excerpt = True
            if FLAG_CODING_CHANGE not in flag_list:
                flag_list.insert(0, FLAG_CODING_CHANGE)
        if FLAG_UNSUPPORTED_CODING_CHANGE in flag_list:
            confidence = min(confidence, 0.4)
        return ProposalDraft(
            action_type=action_type,
            summary=summary,
            rationale=rationale,
            citations=cited,
            corrected_claim=corrected_claim,
            clinic_question=clinic_question,
            coding_change=coding_change,
            requires_note_excerpt=bool(requires_note_excerpt),
            confidence=max(0.0, min(1.0, confidence)),
            deadline=deadline,
            deadline_kind=kind,
            flags=flag_list,
        )

    # ---- shared conveniences -------------------------------------------------------

    @staticmethod
    def reasons_summary(ctx: DenialContext) -> str:
        parts: list[str] = []
        for reason in ctx.reasons:
            text = reason_text(reason)
            rarcs = ", ".join(f"{rc} ({rarc_text(reason, rc)})" for rc in reason.rarcs)
            piece = f"{reason_label(reason)}: {text}"
            if rarcs:
                piece += f"; RARC {rarcs}"
            parts.append(piece)
        return " | ".join(parts) if parts else "no denial reason codes on the remittance"

    @staticmethod
    def reason_lines(ctx: DenialContext) -> list[int]:
        return sorted({r.line_number for r in ctx.reasons if r.line_number is not None})

    @staticmethod
    def line_by_number(ctx: DenialContext, line_number: int | None):
        if line_number is None:
            return None
        return next((line for line in ctx.claim.lines if line.line_number == line_number), None)

    @staticmethod
    def dos_label(ctx: DenialContext) -> str:
        start, end = ctx.claim.service_date_from, ctx.claim.service_date_to
        if start and end and end != start:
            return f"{start.isoformat()} to {end.isoformat()}"
        if start:
            return start.isoformat()
        dates = sorted({line.service_date.isoformat() for line in ctx.claim.lines if line.service_date})
        return ", ".join(dates) if dates else "unknown date of service"


__all__ = [
    "FLAG_CODING_CHANGE",
    "FLAG_CONTRACTUAL_BUNDLING",
    "FLAG_MISSED_MONEY_REVIEW",
    "FLAG_NO_NOTES",
    "FLAG_PAYER_CHANGE_UNVERIFIED",
    "FLAG_PAYER_FROM_DOCUMENT",
    "FLAG_UNSUPPORTED_CODING_CHANGE",
    "Playbook",
    "citation_resolves",
    "code_is_numeric_in",
    "dedupe_citations",
    "document_for_ref",
    "document_ref",
    "keyword_pattern",
    "latest_note",
    "normalize_ws",
    "note_ref",
    "notes_mention",
    "rarc_text",
    "reason_label",
    "reason_ref",
    "reason_text",
    "sentence_matches",
    "split_sentences",
]
