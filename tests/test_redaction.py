"""Redaction tests (milestone M5): registry, tokenizer round trips, scanner, and the guard.

Every value here is synthetic. The guard tests prove that nothing from the registry reaches a
model client and that the originals come back after restoration.
"""

from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from app.enums import IdentifierKind, ModelCallStatus
from app.llm import guard
from app.llm.base import LLMClient
from app.llm.guard import LLMFailedError, LLMRefusedError, guarded_call, make_guard_call
from app.models import AuditLog, ModelCall
from app.redact.registry import IdentifierRegistry, date_formats, name_variants, provider_name_variants
from app.redact.scanner import RedactionError, mask_generic, scan
from app.redact.tokenizer import TOKEN_RE, find_tokens, redact, restore
from app.schemas import (
    ClaimContext,
    ClaimLineContext,
    DenialContext,
    DenialReason,
    DocumentContext,
    LLMResult,
    LLMUsage,
    NoteContext,
    PatientIdentifiers,
    PayerContext,
    RedactionMap,
    RemittanceContext,
    RenderedPrompt,
)

FIRST = "Jonah"
LAST = "Pemberton"
DOB = date(1981, 7, 9)
MEMBER_ID = "XYZ987654321"
ACCOUNT = "ACCT-00042"
ADDRESS = "4471 Maple Hollow Rd"
CITY = "Riverbend"
STATE = "OR"
ZIP = "97401"
CLAIM_CCN = "CLM-2026-000123"
PAYER_CCN = "PCN-55881"
PROVIDER = "Hart, Amelia MD"
SERVICE_DATE = date(2026, 3, 14)
SUBMISSION_DATE = date(2026, 3, 20)
PAYMENT_DATE = date(2026, 4, 2)
RECEIVED_DATE = date(2026, 3, 21)
NOTE_DATE = date(2026, 3, 14)
DOC_DATE = date(2026, 4, 5)

TOKEN_SHAPE = re.compile(r"^\[\[[A-Z_]+_\d+\]\]$")


def build_context(document_text: str | None = None) -> DenialContext:
    doc_text = document_text or (
        f"Dear Provider, claim {CLAIM_CCN} for {FIRST} {LAST} (DOB 07/09/1981, member {MEMBER_ID}) "
        f"at {ADDRESS}, {CITY}, {STATE} {ZIP} was denied: CARC 197. Service date 3/14/2026."
    )
    return DenialContext(
        claim=ClaimContext(
            claim_id=1,
            claim_control_number=CLAIM_CCN,
            patient_token="PT-7F3A2C",
            patient_identifiers=PatientIdentifiers(
                first_name=FIRST,
                last_name=LAST,
                dob=DOB,
                member_id=MEMBER_ID,
                address_line1=ADDRESS,
                city=CITY,
                state=STATE,
                zip=ZIP,
                account_number=ACCOUNT,
            ),
            payer_name="Acme Health Plan",
            payer_id="ACME01",
            billing_provider_npi="1234567893",
            rendering_provider_npi="1992837465",
            rendering_provider_name=PROVIDER,
            diagnosis_codes=["L40.0"],
            service_date_from=SERVICE_DATE,
            service_date_to=SERVICE_DATE,
            total_charge=Decimal("450.00"),
            original_submission_date=SUBMISSION_DATE,
            lines=[
                ClaimLineContext(
                    line_number=1,
                    service_date=SERVICE_DATE,
                    procedure_code="99213",
                    modifiers=["25"],
                    charge_amount=Decimal("150.00"),
                    diagnosis_pointers=[1],
                ),
                ClaimLineContext(
                    line_number=2,
                    service_date=SERVICE_DATE,
                    procedure_code="11102",
                    charge_amount=Decimal("300.00"),
                    diagnosis_pointers=[1],
                    paid_amount=Decimal("0"),
                ),
            ],
        ),
        payer=PayerContext(name="Acme Health Plan", payer_id="ACME01"),
        remittance=RemittanceContext(
            remittance_claim_id=5,
            payment_date=PAYMENT_DATE,
            claim_received_date=RECEIVED_DATE,
            claim_status_code="4",
            charge_amount=Decimal("450.00"),
            denied_amount=Decimal("450.00"),
            payer_claim_control_number=PAYER_CCN,
            remark_codes=["N54"],
        ),
        reasons=[
            DenialReason(
                level="claim",
                group_code="CO",
                carc="197",
                carc_text="Precertification/authorization absent.",
                rarcs=["N54"],
                rarc_texts={"N54": "Claim information is inconsistent with pre-certified services."},
                amount=Decimal("450.00"),
            )
        ],
        documents=[
            DocumentContext(document_id=9, document_type="letter", received_date=DOC_DATE, text=doc_text)
        ],
        notes=[
            NoteContext(
                note_id=3,
                note_date=NOTE_DATE,
                author="Dr. Amelia Hart",
                text=f"Patient {LAST}, {FIRST} seen for plaque psoriasis; biopsy performed.",
            )
        ],
        today=date(2026, 4, 10),
    )


def build_registry() -> IdentifierRegistry:
    return IdentifierRegistry.from_context(build_context())


class CapturingClient:
    """Fake model client that records every rendered prompt and echoes the tokens it saw."""

    name = "capture"
    model_id = "capture-1"

    def __init__(self, status: ModelCallStatus = ModelCallStatus.OK, error: str | None = None) -> None:
        self.calls: list[RenderedPrompt] = []
        self.status = status
        self.error = error

    def complete(self, rendered: RenderedPrompt) -> LLMResult:
        self.calls.append(rendered)
        tokens = " ".join(dict.fromkeys(find_tokens(rendered.user)))
        content: dict[str, Any] | str
        if rendered.schema_ is not None:
            content = {
                "category": "authorization",
                "confidence": 0.8,
                "rationale": f"tokens seen: {tokens}",
                "injection_detected": False,
                "secondary_categories": [],
            }
        else:
            content = f"Rewritten paragraph mentioning {tokens}."
        return LLMResult(
            content=content,
            model_id=self.model_id,
            prompt_name=rendered.name,
            prompt_version=rendered.version,
            usage=LLMUsage(input_tokens=42, output_tokens=7, cache_read_tokens=3),
            request_id="req_capture",
            stop_reason="end_turn",
            status=self.status,
            latency_ms=12,
            error=self.error,
        )


def classify_variables(ctx: DenialContext) -> dict[str, Any]:
    return {
        "reason_codes": ["CO-197"],
        "reasons": ctx.reasons,
        "claim": ctx.claim,
        "payer": ctx.payer,
        "remittance": ctx.remittance,
        "rules_suggestion": {"category": "authorization", "confidence": 0.9},
        "payer_documents": ctx.documents,
        "notes": ctx.notes,
    }


def assert_no_registry_values(text: str, registry: IdentifierRegistry) -> None:
    lowered = text.lower()
    leaked = [ident.kind.value for ident in registry.identifiers() if ident.value.lower() in lowered]
    assert not leaked, f"registry values leaked for kinds {sorted(set(leaked))}"
    assert scan(text, registry) == []


# --------------------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------------------


def test_registry_collects_names_ids_addresses_dates_and_providers():
    registry = build_registry()
    names = registry.values(IdentifierKind.PATIENT_NAME)
    assert {f"{FIRST} {LAST}", f"{LAST}, {FIRST}", f"{LAST} {FIRST}", FIRST, LAST} <= set(names)
    assert registry.values(IdentifierKind.MEMBER_ID) == [MEMBER_ID]
    assert registry.values(IdentifierKind.ACCOUNT) == [ACCOUNT]
    assert {ADDRESS, CITY, ZIP} <= set(registry.values(IdentifierKind.ADDRESS))
    assert STATE not in registry.values(IdentifierKind.ADDRESS)  # two characters: skipped
    assert {CLAIM_CCN, PAYER_CCN} <= set(registry.values(IdentifierKind.CLAIM_ID))
    assert {PROVIDER, "Amelia Hart", "Dr. Amelia Hart"} <= set(registry.values(IdentifierKind.PROVIDER_NAME))
    dob_values = set(registry.values(IdentifierKind.DOB))
    assert {"1981-07-09", "07/09/1981", "7/9/1981", "07091981", "19810709", "July 9, 1981"} <= dob_values
    dates = set(registry.values(IdentifierKind.DATE))
    for d in (SERVICE_DATE, SUBMISSION_DATE, PAYMENT_DATE, RECEIVED_DATE, NOTE_DATE, DOC_DATE):
        assert {d.isoformat(), d.strftime("%m/%d/%Y"), d.strftime("%m%d%Y"), d.strftime("%Y%m%d")} <= dates
    assert not registry.is_empty
    assert "Jonah Pemberton" in registry and "JONAH PEMBERTON" in registry


def test_registry_skips_short_values_and_case_duplicates():
    registry = IdentifierRegistry()
    assert registry.is_empty
    assert registry.add(IdentifierKind.ADDRESS, "OR") is False  # two-letter state code
    assert registry.add(IdentifierKind.MEMBER_ID, "12") is False
    assert registry.add(IdentifierKind.DATE, "26") is False
    assert registry.add(IdentifierKind.PATIENT_NAME, "A") is False  # a single letter is never a name
    assert registry.add(IdentifierKind.PATIENT_NAME, "Al") is True  # two-letter names are real names
    assert registry.add(IdentifierKind.PROVIDER_NAME, "Li") is True
    assert registry.add(IdentifierKind.PATIENT_NAME, "Ann") is True
    assert registry.add(IdentifierKind.PATIENT_NAME, "ANN") is False
    assert registry.add(IdentifierKind.PATIENT_NAME, "  ann ") is False
    assert registry.add(IdentifierKind.MEMBER_ID, None) is False
    assert registry.values(IdentifierKind.PATIENT_NAME) == ["Al", "Ann"]
    assert registry.values(IdentifierKind.PROVIDER_NAME) == ["Li"]
    assert len(registry) == 3


def test_two_letter_names_are_tokenized_as_whole_words():
    registry = IdentifierRegistry()
    registry.add_name("Ed", "Li")
    text = "Patient Li (Mr. Li, first name Ed) and Ed Li; the lid, Lima and edit are untouched."
    redacted, redaction_map = redact(text, registry)
    assert redacted == (
        "Patient [[PATIENT_NAME_1]] (Mr. [[PATIENT_NAME_1]], first name [[PATIENT_NAME_2]]) and "
        "[[PATIENT_NAME_3]]; the lid, Lima and edit are untouched."
    )
    assert redaction_map.tokens == {
        "[[PATIENT_NAME_1]]": "Li",
        "[[PATIENT_NAME_2]]": "Ed",
        "[[PATIENT_NAME_3]]": "Ed Li",
    }
    assert scan(text, registry) and scan(redacted, registry) == []
    assert restore(redacted, redaction_map) == text


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (
            "Priya Thornquist, MD",
            {"Priya Thornquist, MD", "Priya Thornquist", "Thornquist, Priya", "Thornquist"},
        ),
        (
            "Thornquist, Priya MD",
            {"Thornquist, Priya MD", "Thornquist, Priya", "Priya Thornquist", "Thornquist"},
        ),
        (
            "Thornquist, Priya, M.D.",
            {"Thornquist, Priya, M.D.", "Thornquist, Priya", "Priya Thornquist", "Thornquist"},
        ),
        (
            "Dr. Priya Thornquist",
            {"Dr. Priya Thornquist", "Priya Thornquist", "Thornquist, Priya", "Thornquist"},
        ),
        (
            "Dr Priya J. Thornquist, DO",
            {
                "Dr Priya J. Thornquist, DO",
                "Priya J. Thornquist",
                "Thornquist, Priya J.",
                "Priya Thornquist",
                "Thornquist, Priya",
                "Thornquist",
            },
        ),
        (
            "PRIYA THORNQUIST MD",
            {"PRIYA THORNQUIST MD", "PRIYA THORNQUIST", "THORNQUIST, PRIYA", "THORNQUIST"},
        ),
        (
            "Noor Vasquez-Lund, PA-C",
            {"Noor Vasquez-Lund, PA-C", "Noor Vasquez-Lund", "Vasquez-Lund, Noor", "Vasquez-Lund"},
        ),
        (
            "Desmond Kilbride Jr., NP",
            {"Desmond Kilbride Jr., NP", "Desmond Kilbride", "Kilbride, Desmond", "Kilbride"},
        ),
        ("Dr. Thornquist", {"Dr. Thornquist", "Thornquist"}),
        ("Thornquist", {"Thornquist"}),
        ("", set()),
        (None, set()),
    ],
)
def test_provider_name_variants_cover_claim_export_formats(name, expected):
    """The claim export and the synthetic generator write providers as 'First Last, CRED'."""
    variants = provider_name_variants(name)
    assert set(variants) == expected
    assert len(variants) == len(set(variants))
    assert not any(v.upper().startswith(("MD ", "DO ", "NP ")) for v in variants)


def test_registry_tokenizes_provider_surname_in_notes():
    ctx = build_context()
    ctx.claim.rendering_provider_name = "Priya Thornquist, MD"
    ctx.notes[0].author = "Priya Thornquist, MD"
    ctx.notes[0].text = "Seen by Dr. Thornquist today; Priya Thornquist recommends a biopsy (per Thornquist)."
    registry = IdentifierRegistry.from_context(ctx)
    assert {"Priya Thornquist, MD", "Priya Thornquist", "Thornquist, Priya", "Thornquist"} <= set(
        registry.values(IdentifierKind.PROVIDER_NAME)
    )
    redacted, _ = redact(ctx.notes[0].text, registry)
    assert "thornquist" not in redacted.lower() and "priya" not in redacted.lower()
    assert redacted == (
        "Seen by Dr. [[PROVIDER_NAME_1]] today; [[PROVIDER_NAME_2]] recommends a biopsy (per [[PROVIDER_NAME_1]])."
    )
    assert scan(ctx.notes[0].text, registry)  # the scanner uses the same spellings


def test_date_formats_include_two_digit_years():
    forms = date_formats(date(1981, 7, 9))
    assert {"07/09/81", "7/9/81"} <= set(forms)
    registry = IdentifierRegistry()
    registry.add_date(IdentifierKind.DOB, date(1981, 7, 9))
    redacted, _ = redact("DOB 07/09/81, also written 7/9/81 and 07/09/1981", registry)
    assert redacted == "DOB [[DOB_1]], also written [[DOB_2]] and [[DOB_3]]"


def test_zip_plus_four_registers_the_five_digit_zip():
    registry = IdentifierRegistry()
    registry.add_address("12 Elm St", "Riverbend", "OR", "97401-1234")
    assert {"97401-1234", "97401", "Riverbend", "12 Elm St"} <= set(registry.values(IdentifierKind.ADDRESS))
    redacted, _ = redact("ZIP 97401 or 97401-1234; code 974010 stays", registry)
    assert redacted == "ZIP [[ADDRESS_1]] or [[ADDRESS_2]]; code 974010 stays"


def test_date_formats_cover_every_listed_spelling():
    forms = date_formats(date(2026, 3, 4))
    for expected in ("2026-03-04", "03/04/2026", "3/4/2026", "03042026", "20260304", "March 4, 2026"):
        assert expected in forms
    assert len(forms) == len(set(forms))
    assert name_variants("Ada", "Lovelace") == [
        "Ada Lovelace",
        "Lovelace, Ada",
        "Lovelace Ada",
        "Ada",
        "Lovelace",
    ]


def test_registry_from_patient_rows(session, clinic):
    from app.models import Claim, ClaimLine, NoteExcerpt, Patient, Payer

    payer = Payer(name="Acme Health Plan", payer_id="ACME01")
    patient = Patient(
        clinic_id=clinic.id,
        token="PT-7F3A2C",
        account_number=ACCOUNT,
        first_name=FIRST,
        last_name=LAST,
        dob=DOB,
        address_line1=ADDRESS,
        city=CITY,
        state=STATE,
        zip=ZIP,
        member_id=MEMBER_ID,
    )
    session.add_all([payer, patient])
    session.flush()
    claim = Claim(
        clinic_id=clinic.id,
        payer_id=payer.id,
        patient_id=patient.id,
        claim_control_number=CLAIM_CCN,
        rendering_provider_name=PROVIDER,
        service_date_from=SERVICE_DATE,
        service_date_to=SERVICE_DATE,
        original_submission_date=SUBMISSION_DATE,
        total_charge=Decimal("450.00"),
    )
    claim.lines.append(
        ClaimLine(
            line_number=1, service_date=SERVICE_DATE, procedure_code="99213", charge_amount=Decimal("150.00")
        )
    )
    claim.notes.append(NoteExcerpt(note_date=NOTE_DATE, author="Dr. Amelia Hart", text="synthetic note"))
    session.add(claim)
    session.flush()

    registry = IdentifierRegistry.from_patient(patient, [claim])
    assert f"{FIRST} {LAST}" in registry.values(IdentifierKind.PATIENT_NAME)
    assert registry.values(IdentifierKind.MEMBER_ID) == [MEMBER_ID]
    assert registry.values(IdentifierKind.ACCOUNT) == [ACCOUNT]
    assert CLAIM_CCN in registry.values(IdentifierKind.CLAIM_ID)
    assert {PROVIDER, "Dr. Amelia Hart"} <= set(registry.values(IdentifierKind.PROVIDER_NAME))
    assert DOB.isoformat() in registry.values(IdentifierKind.DOB)
    assert {SERVICE_DATE.isoformat(), SUBMISSION_DATE.isoformat(), NOTE_DATE.isoformat()} <= set(
        registry.values(IdentifierKind.DATE)
    )
    redacted, _ = redact(f"{LAST}, {FIRST} {MEMBER_ID} {CLAIM_CCN} 2026-03-14", registry)
    assert_no_registry_values(redacted, registry)


def test_iter_longest_first_orders_by_length():
    registry = build_registry()
    lengths = [len(ident.value) for ident in registry.iter_longest_first()]
    assert lengths == sorted(lengths, reverse=True)


# --------------------------------------------------------------------------------------
# Tokenizer round trips
# --------------------------------------------------------------------------------------


def test_redact_round_trip_on_nested_structures():
    registry = build_registry()
    original = {
        "letter": (
            f"Patient {FIRST} {LAST} (also written {LAST}, {FIRST}) with member id {MEMBER_ID} "
            f"born 1981-07-09, 07/09/1981, 7/9/1981, 07091981, 19810709 and July 9, 1981 "
            f"lives at {ADDRESS}, {CITY}, {STATE} {ZIP}; account {ACCOUNT}; claim {CLAIM_CCN} seen 2026-03-14."
        ),
        "nested": {
            "items": [f"{PROVIDER} treated {FIRST} {LAST}", {"deep": (f"DOS 03/14/2026 {MEMBER_ID}",)}]
        },
        "untouched": {"amount": Decimal("450.00"), "count": 3, "flag": True, "nothing": None},
    }
    redacted, redaction_map = redact(original, registry)

    assert_no_registry_values(json.dumps(redacted), registry)
    for token in TOKEN_RE.findall(json.dumps(redacted)):
        assert token  # kind captured
    assert all(TOKEN_SHAPE.match(token) for token in redaction_map.tokens)
    for kind in (
        "PATIENT_NAME",
        "MEMBER_ID",
        "DOB",
        "ADDRESS",
        "ACCOUNT",
        "CLAIM_ID",
        "DATE",
        "PROVIDER_NAME",
    ):
        assert redaction_map.counts.get(kind, 0) > 0, kind
    assert redacted["untouched"] == {"amount": "450.00", "count": 3, "flag": True, "nothing": None}
    assert isinstance(redacted["nested"]["items"][1]["deep"], tuple)

    restored = restore(redacted, redaction_map)
    assert restored["letter"] == original["letter"]
    assert restored["nested"] == original["nested"]


def test_same_original_maps_to_same_token_regardless_of_casing():
    registry = build_registry()
    text = f"{FIRST} {LAST}; {FIRST.upper()} {LAST.upper()}; {FIRST.lower()} {LAST.lower()}; {MEMBER_ID}; {MEMBER_ID.lower()}"
    redacted, redaction_map = redact(text, registry)
    name_tokens = {t for t in find_tokens(redacted) if t.startswith("[[PATIENT_NAME_")}
    member_tokens = {t for t in find_tokens(redacted) if t.startswith("[[MEMBER_ID_")}
    assert len(name_tokens) == 1 and len(member_tokens) == 1
    assert redacted.count("[[PATIENT_NAME_1]]") == 3
    assert redaction_map.counts["PATIENT_NAME"] == 3
    assert redaction_map.counts["MEMBER_ID"] == 2
    assert redaction_map.tokens["[[PATIENT_NAME_1]]"] == f"{FIRST} {LAST}"


def test_longest_identifier_wins_and_tokens_are_single_units():
    registry = build_registry()
    redacted, _ = redact(f"{LAST}, {FIRST}", registry)
    assert TOKEN_SHAPE.match(redacted), redacted
    redacted2, _ = redact(f"{ADDRESS}, {CITY}", registry)
    assert TOKEN_SHAPE.match(redacted2), redacted2


def test_names_need_word_boundaries_but_ids_match_inside_other_text():
    registry = build_registry()
    redacted, _ = redact(
        f"The {LAST}s and Pembertonville are unrelated; {CLAIM_CCN}-R1 is a corrected claim", registry
    )
    assert "Pembertonville" in redacted
    assert "[[CLAIM_ID_1]]-R1" in redacted
    assert "[[PATIENT_NAME_" not in redacted.replace("Pembertonville", "")  # "Pembertons" is not the name


def test_pydantic_models_dates_and_decimals_are_walked():
    ctx = build_context()
    registry = IdentifierRegistry.from_context(ctx)
    redacted, redaction_map = redact(ctx, registry)
    assert isinstance(redacted, dict)
    assert redacted["claim"]["service_date_from"] == "[[DATE_1]]"
    assert redacted["claim"]["patient_identifiers"]["first_name"].startswith("[[PATIENT_NAME_")
    assert redacted["claim"]["total_charge"] == "450.00"
    assert_no_registry_values(json.dumps(redacted), registry)
    assert redaction_map.counts["DATE"] >= 5


def test_tokens_survive_json_round_trip():
    registry = build_registry()
    redacted, redaction_map = redact({"text": f"{FIRST} {LAST} {MEMBER_ID} 2026-03-14"}, registry)
    reloaded = json.loads(json.dumps(redacted))
    assert restore(reloaded, redaction_map) == {"text": f"{FIRST} {LAST} {MEMBER_ID} 2026-03-14"}
    assert restore("no tokens here", redaction_map) == "no tokens here"
    assert restore({"x": "[[PATIENT_NAME_99]] unknown"}, redaction_map) == {
        "x": "[[PATIENT_NAME_99]] unknown"
    }


def test_empty_registry_is_a_no_op():
    redacted, redaction_map = redact({"text": f"{FIRST} {LAST}"}, IdentifierRegistry())
    assert redacted == {"text": f"{FIRST} {LAST}"}
    assert redaction_map.tokens == {} and redaction_map.counts == {}
    redacted_none, _ = redact("plain", None)
    assert redacted_none == "plain"


# --------------------------------------------------------------------------------------
# Scanner
# --------------------------------------------------------------------------------------


def test_scanner_catches_leftovers_with_masked_previews():
    registry = build_registry()
    findings = scan({"doc": f"Letter about {FIRST.upper()} {LAST.upper()} member {MEMBER_ID}"}, registry)
    kinds = {f.kind for f in findings}
    assert {"PATIENT_NAME", "MEMBER_ID"} <= kinds
    for finding in findings:
        assert finding.location == "$.doc"
        assert finding.value_preview[2:] == "*" * (len(finding.value_preview) - 2)
        assert FIRST.lower() not in finding.value_preview.lower()
        assert MEMBER_ID not in finding.value_preview
    assert scan({"doc": "[[PATIENT_NAME_1]] [[MEMBER_ID_1]] clean"}, registry) == []


def test_scanner_generic_patterns():
    text = (
        "SSN 123-45-6789, email jonah.p@example.test, phone (503) 555-0147 or 503-555-0147 or 503.555.0147, "
        "Date of Birth: 07/09/1981, NPI 1234567893, amount 1,234.56, claim CLM-2026-000123"
    )
    findings = scan(text, None)
    kinds = [f.kind for f in findings]
    assert kinds.count("SSN") == 1
    assert kinds.count("EMAIL") == 1
    assert kinds.count("PHONE") == 3
    assert kinds.count("DOB") == 1
    assert scan("NPI 1234567893 trace 20260402000123 total 450.00 CARC 197 226 227 252", None) == []
    assert scan("dob: [[DOB_1]] phone: [[MASKED_PHONE_1]]", None) == []
    assert scan(text, None, generic=False) == []


def test_mask_generic_replaces_values_with_unrestorable_masks():
    masked, counts = mask_generic(
        "Call 1-800-555-0100 about DOB 7/9/1981; patient [[PATIENT_NAME_1]] born on March 3, 1990."
    )
    assert "1-800-555-0100" not in masked and "7/9/1981" not in masked and "March 3, 1990" not in masked
    assert "[[MASKED_PHONE_1]]" in masked and "[[MASKED_DOB_1]]" in masked and "[[MASKED_DOB_2]]" in masked
    assert "[[PATIENT_NAME_1]]" in masked
    assert counts == {"MASKED_PHONE": 1, "MASKED_DOB": 2}
    assert mask_generic("nothing to mask") == ("nothing to mask", {})


def test_redaction_error_mentions_counts_and_kinds_only():
    registry = build_registry()
    findings = scan(f"{FIRST} {LAST} {MEMBER_ID} 123-45-6789", registry)
    error = RedactionError(findings)
    message = str(error)
    assert "PATIENT_NAME=" in message and "MEMBER_ID=" in message and "SSN=" in message
    assert FIRST not in message and MEMBER_ID not in message and "6789" not in message
    assert error.findings == findings
    assert error.counts["SSN"] == 1


# --------------------------------------------------------------------------------------
# Guard
# --------------------------------------------------------------------------------------


def test_guarded_call_sends_no_identifiers_and_restores_them(session):
    ctx = build_context()
    registry = IdentifierRegistry.from_context(ctx)
    client = CapturingClient()

    result = guarded_call(
        session,
        client,
        "classify_denial",
        classify_variables(ctx),
        registry,
        purpose="test_classify",
        work_item_id=None,
    )

    assert len(client.calls) == 1
    rendered = client.calls[0]
    assert_no_registry_values(rendered.system, registry)
    assert_no_registry_values(rendered.user, registry)
    assert "<untrusted_document" in rendered.user
    user_tokens = set(find_tokens(rendered.user))
    assert any(t.startswith("[[PATIENT_NAME_") for t in user_tokens)
    assert "[[MEMBER_ID_1]]" in user_tokens and "[[CLAIM_ID_1]]" in user_tokens
    assert any(t.startswith("[[DOB_") for t in user_tokens) and any(
        t.startswith("[[ADDRESS_") for t in user_tokens
    )
    assert rendered.schema_ is not None

    assert isinstance(result.content, dict)
    rationale = result.content["rationale"]
    assert f"{FIRST} {LAST}" in rationale
    assert MEMBER_ID in rationale and CLAIM_CCN in rationale
    assert "[[" not in rationale
    assert result.llm_result.content["rationale"].count("[[") > 0  # the raw model result keeps tokens
    assert result.redaction_map.counts["PATIENT_NAME"] >= 1

    row = session.get(ModelCall, result.model_call_id)
    assert row is not None
    assert row.prompt_name == "classify_denial"
    assert row.prompt_version == rendered.version
    assert row.model_id == "capture-1" and row.purpose == "test_classify"
    assert row.input_tokens == 42 and row.output_tokens == 7 and row.cache_read_tokens == 3
    assert row.status == "ok" and row.request_id == "req_capture" and row.latency_ms == 12
    assert row.redaction_counts == result.redaction_map.counts
    assert len(row.payload_sha256) == 64

    events = session.execute(select(AuditLog).where(AuditLog.action == "model_call")).scalars().all()
    assert len(events) == 1
    event = events[0]
    assert event.actor_type == "agent" and event.actor_id == "llm:capture"
    assert event.details["prompt"] == "classify_denial" and event.details["status"] == "ok"
    assert event.details["input_tokens"] == 42
    assert_no_registry_values(json.dumps(event.details), registry)


def test_guard_raises_when_redaction_is_bypassed(session, monkeypatch):
    ctx = build_context()
    registry = IdentifierRegistry.from_context(ctx)
    client = CapturingClient()
    monkeypatch.setattr(guard, "redact", lambda value, registry: (value, RedactionMap()))

    variables = {
        "reason_codes": ["CO-197"],
        "reasons": [r.model_dump(mode="json") for r in ctx.reasons],
        "payer_documents": [
            {"document_id": 9, "document_type": "letter", "text": f"Letter for {FIRST} {LAST}"}
        ],
    }
    with pytest.raises(RedactionError) as excinfo:
        guarded_call(
            session, client, "classify_denial", variables, registry, purpose="bypass", work_item_id=None
        )

    assert client.calls == []
    assert "PATIENT_NAME" in str(excinfo.value)
    assert FIRST not in str(excinfo.value)
    assert session.execute(select(ModelCall)).scalars().all() == []
    blocked = session.execute(select(AuditLog).where(AuditLog.action == "model_call_blocked")).scalars().all()
    assert len(blocked) == 1
    assert blocked[0].details["findings"] >= 1
    assert blocked[0].details["finding_counts"]["PATIENT_NAME"] >= 1
    assert_no_registry_values(json.dumps(blocked[0].details), registry)


def test_guard_masks_generic_values_it_cannot_tokenize(session):
    ctx = build_context(
        document_text=f"Claim {CLAIM_CCN} denied. Call Provider Services at 1-800-555-0100 or email appeals@acme.test."
    )
    registry = IdentifierRegistry.from_context(ctx)
    client = CapturingClient()
    result = guarded_call(
        session, client, "classify_denial", classify_variables(ctx), registry, purpose="mask"
    )
    rendered = client.calls[0]
    assert "1-800-555-0100" not in rendered.user and "appeals@acme.test" not in rendered.user
    assert "[[MASKED_PHONE_1]]" in rendered.user and "[[MASKED_EMAIL_1]]" in rendered.user
    assert result.redaction_map.counts["MASKED_PHONE"] == 1
    assert_no_registry_values(rendered.user, registry)


def test_guard_without_session_skips_database_rows():
    ctx = build_context()
    registry = IdentifierRegistry.from_context(ctx)
    client = CapturingClient()
    result = guarded_call(None, client, "classify_denial", classify_variables(ctx), registry, purpose="no_db")
    assert result.model_call_id is None
    assert isinstance(result.content, dict)
    assert_no_registry_values(client.calls[0].user, registry)


def test_guard_schema_override_and_restore_toggle():
    ctx = build_context()
    registry = IdentifierRegistry.from_context(ctx)
    client = CapturingClient()
    override = {"type": "object", "properties": {"category": {"type": "string"}}, "required": ["category"]}
    result = guarded_call(
        None,
        client,
        "classify_denial",
        classify_variables(ctx),
        registry,
        purpose="schema",
        schema=override,
        restore_tokens=False,
    )
    assert client.calls[0].schema_ == override
    assert "[[" in result.content["rationale"]  # tokens kept when restore_tokens is False


def test_guard_draft_appeal_restores_text_content():
    ctx = build_context()
    registry = IdentifierRegistry.from_context(ctx)
    client = CapturingClient()
    variables = {
        "facts": {"claim": CLAIM_CCN, "service_date": SERVICE_DATE},
        "note_excerpts": ctx.notes,
        "narrative": f"We ask that the claim for {FIRST} {LAST} ({MEMBER_ID}) seen on 03/14/2026 be reconsidered.",
    }
    result = guarded_call(None, client, "draft_appeal", variables, registry, purpose="draft")
    rendered = client.calls[0]
    assert_no_registry_values(rendered.user, registry)
    assert "--- narrative ---" in rendered.user and "--- end narrative ---" in rendered.user
    assert isinstance(result.content, str)
    assert f"{FIRST} {LAST}" in result.content and MEMBER_ID in result.content


def test_make_guard_call_returns_content_and_raises_on_bad_status(session):
    ctx = build_context()
    registry = IdentifierRegistry.from_context(ctx)
    ok_call = make_guard_call(session, CapturingClient(), registry, work_item_id=None, purpose="adapter")
    content = ok_call("classify_denial", classify_variables(ctx))
    assert isinstance(content, dict) and content["category"] == "authorization"

    refused = make_guard_call(session, CapturingClient(ModelCallStatus.REFUSAL, "cyber"), registry)
    with pytest.raises(LLMRefusedError) as refusal:
        refused("classify_denial", classify_variables(ctx))
    assert refusal.value.status == ModelCallStatus.REFUSAL
    assert "refusal" in str(refusal.value) and FIRST not in str(refusal.value)

    failed = make_guard_call(session, CapturingClient(ModelCallStatus.ERROR, "invalid_json"), registry)
    with pytest.raises(LLMFailedError) as failure:
        failed("classify_denial", classify_variables(ctx), schema={"type": "object"})
    assert failure.value.status == ModelCallStatus.ERROR and failure.value.error == "invalid_json"
    assert session.execute(select(ModelCall)).scalars().all()  # error calls are still recorded


def test_capturing_client_satisfies_protocol():
    assert isinstance(CapturingClient(), LLMClient)


class FailingClient:
    """Fake model client whose transport always fails."""

    name = "failing"
    model_id = "failing-1"

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def complete(self, rendered: RenderedPrompt) -> LLMResult:
        raise self.exc


def test_guard_records_client_failures_without_content(session):
    from app.llm.clients import LLMTransportError

    ctx = build_context()
    registry = IdentifierRegistry.from_context(ctx)
    client = FailingClient(LLMTransportError("RateLimitError", 429))
    with pytest.raises(LLMTransportError):
        guarded_call(session, client, "classify_denial", classify_variables(ctx), registry, purpose="fail")

    rows = session.execute(select(ModelCall)).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.status == "error" and row.model_id == "failing-1" and row.prompt_name == "classify_denial"
    assert (
        row.purpose == "fail" and row.input_tokens == 0 and row.output_tokens == 0 and row.request_id is None
    )
    assert row.redaction_counts["PATIENT_NAME"] >= 1 and len(row.payload_sha256) == 64
    events = session.execute(select(AuditLog).where(AuditLog.action == "model_call_failed")).scalars().all()
    assert len(events) == 1
    details = events[0].details
    assert details["error"] == "RateLimitError" and details["status_code"] == 429
    assert details["model_call_id"] == row.id and details["prompt"] == "classify_denial"
    assert events[0].actor_id == "llm:failing"
    assert_no_registry_values(json.dumps(details), registry)

    # Any other client exception is recorded the same way (name only) and still propagates.
    with pytest.raises(RuntimeError):
        guarded_call(
            session,
            FailingClient(RuntimeError("boom")),
            "classify_denial",
            classify_variables(ctx),
            registry,
            purpose="fail",
        )
    assert len(session.execute(select(ModelCall)).scalars().all()) == 2
    failed = session.execute(select(AuditLog).where(AuditLog.action == "model_call_failed")).scalars().all()
    assert failed[-1].details["error"] == "RuntimeError" and failed[-1].details["status_code"] is None
    assert "boom" not in json.dumps(failed[-1].details)

    # Without a session nothing is written and the exception still propagates.
    with pytest.raises(LLMTransportError):
        guarded_call(None, client, "classify_denial", classify_variables(ctx), registry, purpose="fail")
