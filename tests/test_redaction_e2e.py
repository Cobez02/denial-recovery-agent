"""Redaction, end to end (plan testing table; docs/SPEC.md section 8, M5 done-when).

The committed synthetic batch is imported and processed with the stub model while every rendered
prompt that reaches the client is captured (after the guard's tokenization and masking). No payload
may contain a name, member id, date of birth, address or account number of any synthetic patient;
payer documents and note excerpts must travel inside ``<untrusted_document>`` fences; redaction
tokens must be visible; and restoration happens on our side only: the appeal letters carry the
patient's name, the payloads never do. Audit details, model-call rows and rationales are checked too.
"""

from __future__ import annotations

import hashlib
import re

from sqlalchemy import select

from app.enums import ModelCallStatus
from app.models import AuditLog, ModelCall
from synthetic.profile import load_profile
from tests import helpers

MIN_PAYLOADS = 10
MIN_LETTERS = 10
REQUIRED_TOKEN_PREFIXES = ("[[PATIENT_NAME_", "[[MEMBER_ID_", "[[DOB_", "[[DATE_")
DOCUMENT_COUNT_LINES = {
    "classify_denial": re.compile(r"^Payer documents: (\d+)", re.MULTILINE),
    "draft_appeal": re.compile(r"^Quoted note excerpts: (\d+)", re.MULTILINE),
}
LEFTOVER_TOKEN = re.compile(r"\[\[[A-Z_]+_\d+\]\]")
FENCE_OPEN = re.compile(r"<untrusted_document\s+source=")
FENCE_SPAN = re.compile(r"<untrusted_document\s[^>]*>.*?</untrusted_document>", re.DOTALL)


def _payer_letterheads(profile_name: str) -> set[str]:
    """The payers' own return addresses as the synthetic letter templates print them.

    Four synthetic payers are located in towns that synthetic patients also live in, so a whole-batch
    address scan would otherwise report a payer's PO box line as a patient address. These strings
    are the payer's address printed on every letter, not patient data (the registry built for a
    claim still tokenizes them when they are that patient's own city and zip).
    """
    letterheads: set[str] = set()
    for payer in load_profile(profile_name).payers:
        address = payer.address or {}
        city, state, zip_code = address.get("city", ""), address.get("state", ""), address.get("zip", "")
        if city and zip_code:
            letterheads.add(f"{city}, {state} {zip_code}".lower())
            letterheads.add(f"{city} {state} {zip_code}".lower())
    return letterheads


def _phrase_pattern(value: str) -> re.Pattern[str]:
    """Case-insensitive whole-phrase match tolerant of whitespace runs (as the tokenizer matches)."""
    parts = [re.escape(part) for part in value.split()]
    return re.compile(r"(?<![a-z0-9])" + r"\s+".join(parts) + r"(?![a-z0-9])", re.IGNORECASE)


def _outside_fences(text: str, pattern: re.Pattern[str]) -> int:
    """How many matches of ``pattern`` fall outside every ``<untrusted_document>`` fence in ``text``."""
    spans = [m.span() for m in FENCE_SPAN.finditer(text)]
    return sum(
        1
        for match in pattern.finditer(text)
        if not any(start <= match.start() and match.end() <= end for start, end in spans)
    )


def _document_count(prompt) -> int:
    pattern = DOCUMENT_COUNT_LINES.get(prompt.name)
    assert pattern is not None, f"unexpected prompt {prompt.name}"
    match = pattern.search(prompt.user)
    assert match is not None, f"{prompt.name} payload has no document count line"
    return int(match.group(1))


def test_no_identifier_reaches_a_model_payload(session, settings, clinic, captured_llm_payloads):
    helpers.load_fixture_batch(session, settings, clinic)
    created = helpers.run_pipeline(session)
    labels = helpers.load_labels()
    assert len(created) == len(labels)
    payloads = list(captured_llm_payloads)
    assert len(payloads) >= MIN_PAYLOADS
    assert {p.name for p in payloads} == set(DOCUMENT_COUNT_LINES)

    # -- 1. no patient identifier in any payload -------------------------------------------
    identifiers = helpers.identifiers_for_batch()
    letterheads = _payer_letterheads(helpers.load_manifest()["profile"])
    patient_identifiers = [(kind, value) for kind, value in identifiers if value.lower() not in letterheads]
    excluded = len(identifiers) - len(patient_identifiers)
    assert 0 < excluded <= 2 * len(letterheads), "only payer letterhead city/zip lines may be set aside"
    assert all(kind == helpers.KIND_ADDRESS for kind, value in identifiers if value.lower() in letterheads)

    findings = []
    for index, prompt in enumerate(payloads):
        for part, text in (("system", prompt.system), ("user", prompt.user)):
            for kind, preview in helpers.scan_text_for_identifiers(text, patient_identifiers):
                findings.append((index, prompt.name, part, kind, preview))
    assert not findings, f"{len(findings)} identifier(s) in model payloads, e.g. {findings[:10]}"

    # The set-aside letterhead lines may appear only inside a fenced payer document (where the
    # letter prints the payer's return address), never in the structured facts or the system prompt.
    outside = []
    for index, prompt in enumerate(payloads):
        for value in sorted(letterheads):
            pattern = _phrase_pattern(value)
            if pattern.search(prompt.system):
                outside.append((index, prompt.name, "system", helpers.mask_preview(value)))
            if _outside_fences(prompt.user, pattern):
                outside.append((index, prompt.name, "user", helpers.mask_preview(value)))
    assert not outside, f"payer letterhead lines outside fenced documents, e.g. {outside[:10]}"
    assert any(_phrase_pattern(value).search(p.user) for p in payloads for value in letterheads)

    # -- 2. documents and note excerpts travel as fenced data ------------------------------
    with_documents = 0
    for prompt in payloads:
        count = _document_count(prompt)
        opened = len(FENCE_OPEN.findall(prompt.user))
        closed = prompt.user.count("</untrusted_document>")
        assert opened == closed == count, (prompt.name, count, opened, closed)
        if count:
            with_documents += 1
            assert "<untrusted_document" in prompt.user
        # The system prompt may explain the fence; it never carries a fenced document itself.
        assert not FENCE_OPEN.search(prompt.system)
    assert with_documents > 0
    classify_with_docs = sum(1 for p in payloads if p.name == "classify_denial" and _document_count(p) > 0)
    assert classify_with_docs == sum(1 for label in labels if label["has_letter"])

    # -- 3. tokens are what the model sees ---------------------------------------------------
    combined = "\n".join(p.system + "\n" + p.user for p in payloads)
    for prefix in REQUIRED_TOKEN_PREFIXES:
        assert prefix in combined, f"no {prefix} token in any payload"

    # -- 4. model_calls rows: counts and hashes only ---------------------------------------
    calls = list(session.execute(select(ModelCall).order_by(ModelCall.id)).scalars())
    assert len(calls) == len(payloads)
    for call, prompt in zip(calls, payloads, strict=True):
        assert call.status == str(ModelCallStatus.OK) and call.error is None
        assert call.prompt_name == prompt.name and call.prompt_version == prompt.version
        assert (
            call.payload_sha256 == hashlib.sha256((prompt.system + prompt.user).encode("utf-8")).hexdigest()
        )
        assert call.work_item_id is not None and call.input_tokens > 0
        assert isinstance(call.redaction_counts, dict)
    assert any(call.redaction_counts.get("PATIENT_NAME", 0) > 0 for call in calls)

    # -- 5. nothing we persist about a call or a decision carries an identifier -----------
    by_ccn = helpers.work_items_by_ccn(session)
    events = list(session.execute(select(AuditLog).order_by(AuditLog.id)).scalars())
    values = [(f"model_call:{c.id}.redaction_counts", c.redaction_counts) for c in calls]
    values += [(f"model_call:{c.id}.error", c.error) for c in calls if c.error]
    values += [(f"audit:{e.id}:{e.action}.details", e.details) for e in events]
    values += [(f"audit:{e.id}:{e.action}.reason", e.reason) for e in events if e.reason]
    for ccn, work_item in by_ccn.items():
        proposal = work_item.current_proposal
        values.append((f"{ccn}.classification_rationale", work_item.classification_rationale))
        values.append((f"{ccn}.proposal.rationale", proposal.rationale))
        values.append((f"{ccn}.proposal.summary", proposal.summary))
    stored = helpers.scan_values(values, identifiers)
    assert not stored, f"identifiers in stored call/audit/rationale fields, e.g. {stored[:10]}"

    # -- 6. restoration happened on our side: letters name the patient, payloads never did --
    letters = [(wi, wi.current_proposal) for wi in by_ccn.values() if wi.current_proposal.appeal_letter]
    assert len(letters) >= MIN_LETTERS
    for work_item, proposal in letters:
        patient = work_item.claim.patient
        assert patient.first_name in proposal.appeal_letter and patient.last_name in proposal.appeal_letter
        assert not LEFTOVER_TOKEN.search(proposal.appeal_letter), work_item.claim.claim_control_number
        assert "model_narrative" in work_item.flags or "model_draft_rejected" in work_item.flags
    draft_payloads = [p for p in payloads if p.name == "draft_appeal"]
    assert len(draft_payloads) == len(letters)
