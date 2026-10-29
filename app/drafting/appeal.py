"""Appeal letters from templates, claim facts and quoted note excerpts (docs/SPEC.md section 9).

The letter is plain text built locally from `ctx` and the note sentences the playbook cited. It is
addressed to the payer and therefore carries the patient's name, date of birth and member id from
`ctx.claim.patient_identifiers`; none of that is ever sent to the model. When a guard call is
available and drafting with the model is enabled, the model may rewrite the narrative paragraph only;
`factcheck.verify` rejects any rewrite that introduces a number, code or date absent from the inputs.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from app.config import get_settings
from app.drafting.corrected_claim import format_money, format_units
from app.drafting.factcheck import verify
from app.drafting.letters import BASE_TEMPLATE, narrative_template_name, render_block, render_template
from app.enums import CitationKind, DenialCategory
from app.schemas import ClassificationResult, DenialContext, NoteContext, ProposalDraft

log = logging.getLogger(__name__)

FLAG_MODEL_NARRATIVE = "model_narrative"
FLAG_MODEL_DRAFT_REJECTED = "model_draft_rejected"
FLAG_MODEL_UNAVAILABLE = "model_unavailable"

PROMPT_NAME = "draft_appeal"
MAX_EXCERPTS = 8
MAX_NARRATIVE_CHARS = 4000

GuardCall = Callable[[str, dict[str, Any]], Any]


# --------------------------------------------------------------------------------------
# Facts
# --------------------------------------------------------------------------------------


def _amount_in_dispute(ctx: DenialContext) -> Decimal:
    amount = Decimal(ctx.remittance.denied_amount or 0)
    if amount <= 0:
        amount = sum((Decimal(r.amount or 0) for r in ctx.reasons), Decimal("0"))
    if amount <= 0:
        amount = Decimal(ctx.claim.total_charge or 0)
    return max(amount, Decimal("0"))


def _dates_of_service(ctx: DenialContext) -> str:
    start, end = ctx.claim.service_date_from, ctx.claim.service_date_to
    if start and end and end != start:
        return f"{start.isoformat()} to {end.isoformat()}"
    if start:
        return start.isoformat()
    dates = sorted({line.service_date.isoformat() for line in ctx.claim.lines if line.service_date})
    return ", ".join(dates) if dates else "date of service not recorded"


def _line_facts(ctx: DenialContext) -> list[dict[str, Any]]:
    lines = []
    for line in ctx.claim.lines:
        mods = [str(m) for m in line.modifiers]
        lines.append(
            {
                "line_number": line.line_number,
                "procedure_code": line.procedure_code,
                "modifiers": mods,
                "code_with_modifiers": "-".join([line.procedure_code, *mods]),
                "units": format_units(line.units),
                "charge_amount": format_money(line.charge_amount),
                "service_date": line.service_date.isoformat()
                if line.service_date
                else _dates_of_service(ctx),
            }
        )
    return lines


def _reason_facts(ctx: DenialContext) -> list[dict[str, str]]:
    reasons: list[dict[str, str]] = []
    seen: set[str] = set()
    for reason in ctx.reasons:
        code = f"{reason.group_code}-{reason.carc}" if reason.group_code else reason.carc
        if code not in seen:
            seen.add(code)
            reasons.append(
                {
                    "label": f"CARC {code}",
                    "code": code,
                    "text": reason.carc_text or f"reason code {reason.carc}",
                }
            )
        for rarc in reason.rarcs:
            if rarc not in seen:
                seen.add(rarc)
                reasons.append(
                    {
                        "label": f"RARC {rarc}",
                        "code": rarc,
                        "text": reason.rarc_texts.get(rarc) or f"remark code {rarc}",
                    }
                )
    return reasons


def _modifier_sentence(draft: ProposalDraft, ctx: DenialContext) -> str:
    """'modifier 59 added to line 2 (17000)' from the corrected claim's modifier changes."""
    if draft.corrected_claim is None:
        return ""
    pieces: list[str] = []
    for change in draft.corrected_claim.changes:
        if not change.path.startswith("lines[") or not change.path.endswith(".modifiers"):
            continue
        before = [str(m) for m in (change.before or [])]
        after = [str(m) for m in (change.after or [])]
        line_number = change.path[len("lines[") : change.path.index("]")]
        line = next((ln for ln in ctx.claim.lines if str(ln.line_number) == line_number), None)
        code = f" ({line.procedure_code})" if line else ""
        added = [m for m in after if m not in before]
        removed = [m for m in before if m not in after]
        if added:
            pieces.append(f"modifier {', '.join(added)} added to line {line_number}{code}")
        if removed:
            pieces.append(f"modifier {', '.join(removed)} removed from line {line_number}{code}")
    return "; ".join(pieces)


def _changes_sentence(draft: ProposalDraft) -> str:
    if draft.corrected_claim is None:
        return ""
    pieces = []
    for change in draft.corrected_claim.changes:
        before = (
            ", ".join(map(str, change.before))
            if isinstance(change.before, list)
            else (change.before or "none")
        )
        after = (
            ", ".join(map(str, change.after)) if isinstance(change.after, list) else (change.after or "none")
        )
        pieces.append(f"{change.path}: {before} -> {after}")
    return "; ".join(pieces)


def build_letter_facts(
    ctx: DenialContext, classification: ClassificationResult, draft: ProposalDraft
) -> dict[str, Any]:
    """Facts the templates and the model see. No patient identifiers: those stay in the letter header."""
    submitted = ctx.claim.original_submission_date
    dos_from = ctx.claim.service_date_from
    days_to_submit = (submitted - dos_from).days if submitted and dos_from else None
    provider = ctx.claim.rendering_provider_name or "the rendering provider"
    lines = _line_facts(ctx)
    return {
        "category": str(DenialCategory(classification.category)),
        "action_type": str(draft.action_type),
        "payer_name": ctx.claim.payer_name or ctx.payer.name,
        "claim_control_number": ctx.claim.claim_control_number,
        "payer_claim_control_number": ctx.remittance.payer_claim_control_number,
        "trace_number": ctx.remittance.trace_number,
        "payment_date": ctx.remittance.payment_date.isoformat() if ctx.remittance.payment_date else None,
        "dates_of_service": _dates_of_service(ctx),
        "total_charge": format_money(ctx.claim.total_charge),
        "denied_amount": format_money(_amount_in_dispute(ctx)),
        "diagnosis_codes": list(ctx.claim.diagnosis_codes),
        "diagnosis_list": ", ".join(ctx.claim.diagnosis_codes) or "the diagnosis on the claim",
        "lines": lines,
        "codes_list": ", ".join(line["code_with_modifiers"] for line in lines) or "the services on the claim",
        "reasons": _reason_facts(ctx),
        "prior_auth_number": ctx.claim.prior_auth_number,
        "original_submission_date": submitted.isoformat() if submitted else None,
        "days_to_submit": days_to_submit,
        "timely_filing_days": ctx.payer.timely_filing_days,
        "corrected_claim_days": ctx.payer.corrected_claim_days,
        "appeal_days": ctx.payer.appeal_days,
        "provider": provider,
        "provider_npi": ctx.claim.rendering_provider_npi or ctx.claim.billing_provider_npi,
        "place_of_service": ctx.claim.place_of_service,
        "modifier_sentence": _modifier_sentence(draft, ctx),
        "changes_sentence": _changes_sentence(draft),
        "summary": draft.summary,
    }


def _note_for_ref(ctx: DenialContext, ref: str) -> NoteContext | None:
    key = ref.split(":", 1)[1] if ":" in ref else ref
    for note in ctx.notes:
        if note.note_id is not None and str(note.note_id) == key:
            return note
    if key.isdigit():
        index = int(key)
        if 0 <= index < len(ctx.notes) and all(n.note_id is None for n in ctx.notes):
            return ctx.notes[index]
    return None


def note_excerpts(ctx: DenialContext, draft: ProposalDraft) -> list[dict[str, str]]:
    """The note sentences the playbook cited, as {date, text}, in citation order (deduplicated)."""
    excerpts: list[dict[str, str]] = []
    seen: set[str] = set()
    for citation in draft.citations:
        if CitationKind(citation.kind) != CitationKind.NOTE_EXCERPT or not citation.text.strip():
            continue
        text = citation.text.strip()
        if text in seen:
            continue
        seen.add(text)
        note = _note_for_ref(ctx, citation.ref)
        note_date = note.note_date.isoformat() if note and note.note_date else "undated"
        excerpts.append({"date": note_date, "text": text})
        if len(excerpts) >= MAX_EXCERPTS:
            break
    return excerpts


def _header(ctx: DenialContext) -> dict[str, Any]:
    ids = ctx.claim.patient_identifiers
    if ids is not None:
        name = (
            " ".join(part for part in (ids.first_name, ids.last_name) if part).strip()
            or ctx.claim.patient_token
        )
        dob = ids.dob.isoformat() if ids.dob else None
        member_id = ids.member_id
    else:
        name, dob, member_id = ctx.claim.patient_token, None, None
    provider = ctx.claim.rendering_provider_name or "the rendering provider"
    npi = ctx.claim.rendering_provider_npi or ctx.claim.billing_provider_npi
    return {
        "today": ctx.today.isoformat(),
        "patient_name": name,
        "patient_dob": dob,
        "member_id": member_id,
        "rendering_provider": f"{provider} (NPI {npi})" if npi else provider,
        "letter_title": f"Appeal of claim {ctx.claim.claim_control_number}",
    }


# --------------------------------------------------------------------------------------
# Model rewrite of the narrative paragraph
# --------------------------------------------------------------------------------------


def _extract_narrative(result: Any) -> str | None:
    if result is None:
        return None
    llm_result = getattr(result, "llm_result", None)
    if llm_result is not None and str(getattr(llm_result, "status", "ok")) != "ok":
        return None
    content = getattr(result, "content", result)
    if isinstance(content, dict):
        for key in ("narrative", "text", "content", "letter", "paragraph"):
            value = content.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None
    if isinstance(content, str) and content.strip():
        return content.strip()
    return None


def rewrite_narrative(
    narrative: str,
    facts: dict[str, Any],
    excerpts: list[dict[str, str]],
    guard_call: GuardCall | None,
) -> tuple[str, list[str]]:
    """Ask the model (through the guard) to rewrite the narrative; keep the template on any doubt."""
    if guard_call is None or not get_settings().model_enabled_for_drafting:
        return narrative, []
    # `date` for this module's tests and callers, `note_date` for the draft_appeal prompt template.
    notes = [{"date": e["date"], "note_date": e["date"], "text": e["text"]} for e in excerpts]
    try:
        result = guard_call(PROMPT_NAME, {"facts": facts, "notes": notes, "narrative": narrative})
    except Exception as exc:
        # The guard adapter raises on a model refusal or an unusable response (status refusal/error):
        # the model answered, its draft is rejected. Anything else (transport, configuration, a payload
        # the scanner blocked) means the model was never reached.
        status = str(getattr(exc, "status", "") or "")
        if status in ("refusal", "error"):
            log.info("draft_appeal model call ended with status=%s; template kept", status)
            return narrative, [FLAG_MODEL_DRAFT_REJECTED]
        log.warning("draft_appeal model call failed: %s", type(exc).__name__)
        return narrative, [FLAG_MODEL_UNAVAILABLE]
    rewritten = _extract_narrative(result)
    if not rewritten or len(rewritten) > MAX_NARRATIVE_CHARS:
        log.info("draft_appeal model output unusable (empty, refused or too long); template kept")
        return narrative, [FLAG_MODEL_DRAFT_REJECTED]
    inputs_text = "\n".join(
        [narrative, json.dumps(facts, default=str, ensure_ascii=False), *[n["text"] for n in notes]]
    )
    unsupported = verify(rewritten, inputs_text)
    if unsupported:
        log.info("draft_appeal model output rejected: %d unsupported fact(s)", len(unsupported))
        return narrative, [FLAG_MODEL_DRAFT_REJECTED]
    return rewritten, [FLAG_MODEL_NARRATIVE]


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------


def draft_appeal(
    ctx: DenialContext,
    classification: ClassificationResult,
    draft: ProposalDraft,
    guard_call: GuardCall | None = None,
) -> tuple[str, list[str]]:
    """(letter_text, flags). Flags: model_narrative | model_draft_rejected | model_unavailable."""
    category = DenialCategory(classification.category)
    facts = build_letter_facts(ctx, classification, draft)
    excerpts = note_excerpts(ctx, draft)
    variables: dict[str, Any] = {
        "f": facts,
        "excerpts": excerpts,
        "has_excerpts": bool(excerpts),
        "category": str(category),
    }
    template_name = narrative_template_name(category)
    narrative = render_block(template_name, "narrative", variables).strip()
    request = render_block(template_name, "request", variables).strip()
    narrative, flags = rewrite_narrative(narrative, facts, excerpts, guard_call)
    letter = render_template(
        BASE_TEMPLATE,
        {**variables, **_header(ctx), "narrative": narrative, "request": request},
    )
    log.info(
        "appeal letter drafted for claim_id=%s category=%s excerpts=%d flags=%s",
        ctx.claim.claim_id,
        category,
        len(excerpts),
        ",".join(flags) or "-",
    )
    return letter, flags


__all__ = [
    "FLAG_MODEL_DRAFT_REJECTED",
    "FLAG_MODEL_NARRATIVE",
    "FLAG_MODEL_UNAVAILABLE",
    "PROMPT_NAME",
    "build_letter_facts",
    "draft_appeal",
    "note_excerpts",
    "rewrite_narrative",
]
