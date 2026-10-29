"""Pydantic contracts passed between modules (docs/SPEC.md section 5).

These names are final. Add fields rather than renaming; every module imports from here.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.enums import (
    ActionType,
    CitationKind,
    ClassificationSource,
    DeadlineKind,
    DenialCategory,
    IdentifierKind,
    ModelCallStatus,
)

# --------------------------------------------------------------------------------------
# Parsed X12 835 (output of app.ingest.parse_835)
# --------------------------------------------------------------------------------------


class ParsedAdjustment(BaseModel):
    """One CAS group/reason/amount triplet."""

    group_code: str  # CO, PR, OA, PI, CR
    carc: str
    amount: Decimal = Decimal("0")
    quantity: Decimal | None = None


class ParsedServicePayment(BaseModel):
    """One SVC loop (2110)."""

    line_number: int
    procedure_code: str | None = None
    modifiers: list[str] = Field(default_factory=list)
    qualifier: str | None = None  # HC, AD, ...
    charge_amount: Decimal = Decimal("0")
    paid_amount: Decimal = Decimal("0")
    revenue_code: str | None = None
    units: Decimal = Decimal("1")
    original_procedure_code: str | None = None
    service_date: date | None = None
    line_item_control_number: str | None = None  # REF*6R
    allowed_amount: Decimal | None = None  # AMT*B6
    adjustments: list[ParsedAdjustment] = Field(default_factory=list)
    remark_codes: list[str] = Field(default_factory=list)  # LQ*HE


class ParsedClaimPayment(BaseModel):
    """One CLP loop (2100)."""

    sequence: int
    patient_control_number: str  # CLP01
    claim_status_code: str  # CLP02
    charge_amount: Decimal = Decimal("0")  # CLP03
    paid_amount: Decimal = Decimal("0")  # CLP04
    patient_responsibility: Decimal = Decimal("0")  # CLP05
    filing_indicator: str | None = None  # CLP06
    payer_claim_control_number: str | None = None  # CLP07
    facility_type_code: str | None = None  # CLP08
    frequency_code: str | None = None  # CLP09
    patient_last_name: str | None = None
    patient_first_name: str | None = None
    member_id: str | None = None
    insured_last_name: str | None = None
    insured_first_name: str | None = None
    rendering_provider_npi: str | None = None
    claim_received_date: date | None = None  # DTM*050
    statement_from: date | None = None  # DTM*232
    statement_to: date | None = None  # DTM*233
    coverage_amount: Decimal | None = None  # AMT*AU
    adjustments: list[ParsedAdjustment] = Field(default_factory=list)  # claim level CAS
    services: list[ParsedServicePayment] = Field(default_factory=list)
    remark_codes: list[str] = Field(default_factory=list)  # claim level LQ*HE
    references: dict[str, str] = Field(default_factory=dict)  # REF qualifiers -> values


class ParsedProviderAdjustment(BaseModel):
    """PLB segment."""

    provider_id: str
    fiscal_period_date: date | None = None
    reason_code: str | None = None
    reference: str | None = None
    amount: Decimal = Decimal("0")


class ParsedPayer(BaseModel):
    name: str | None = None
    payer_id: str | None = None  # REF*2U or N104
    address_line1: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    contact: str | None = None


class ParsedPayee(BaseModel):
    name: str | None = None
    npi: str | None = None  # N104 when N103 == XX
    tax_id: str | None = None  # REF*TJ
    address_line1: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None


class ParsedPayment(BaseModel):
    """BPR / TRN."""

    transaction_handling_code: str | None = None  # BPR01
    amount: Decimal = Decimal("0")  # BPR02
    credit_debit: str | None = None  # BPR03
    method: str | None = None  # BPR04 (ACH, CHK, NON)
    payment_date: date | None = None  # BPR16
    trace_number: str | None = None  # TRN02
    payer_identifier: str | None = None  # TRN03
    production_date: date | None = None  # DTM*405


class Parsed835(BaseModel):
    interchange_control_number: str | None = None  # ISA13
    group_control_number: str | None = None  # GS06
    transaction_control_number: str | None = None  # ST02
    interchange_date: date | None = None  # ISA09
    sender_id: str | None = None  # ISA06
    receiver_id: str | None = None  # ISA08
    payment: ParsedPayment = Field(default_factory=ParsedPayment)
    payer: ParsedPayer = Field(default_factory=ParsedPayer)
    payee: ParsedPayee = Field(default_factory=ParsedPayee)
    claims: list[ParsedClaimPayment] = Field(default_factory=list)
    provider_adjustments: list[ParsedProviderAdjustment] = Field(default_factory=list)
    unparsed_segments: list[str] = Field(default_factory=list)
    segment_count: int = 0


# --------------------------------------------------------------------------------------
# Claim export rows (docs/SPEC.md section 4.1)
# --------------------------------------------------------------------------------------

CLAIM_EXPORT_COLUMNS: tuple[str, ...] = (
    "claim_control_number",
    "patient_account_number",
    "patient_first_name",
    "patient_last_name",
    "patient_dob",
    "patient_sex",
    "patient_address_line1",
    "patient_city",
    "patient_state",
    "patient_zip",
    "member_id",
    "payer_id",
    "payer_name",
    "billing_provider_npi",
    "rendering_provider_npi",
    "rendering_provider_name",
    "referring_provider_npi",
    "place_of_service",
    "diagnosis_codes",
    "prior_auth_number",
    "original_submission_date",
    "claim_frequency_code",
    "original_claim_control_number",
    "line_number",
    "service_date",
    "procedure_code",
    "modifiers",
    "units",
    "charge_amount",
    "diagnosis_pointers",
    "ndc",
    "line_item_control_number",
    "claim_total_charge",
)


def _split_pipe(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [x.strip() for x in str(v).split("|") if x.strip()]


class ClaimExportRow(BaseModel):
    """One CSV row = one claim line. Empty strings become None for optional fields."""

    model_config = ConfigDict(str_strip_whitespace=True)

    claim_control_number: str
    patient_account_number: str
    patient_first_name: str
    patient_last_name: str
    patient_dob: date | None = None
    patient_sex: str | None = None
    patient_address_line1: str | None = None
    patient_city: str | None = None
    patient_state: str | None = None
    patient_zip: str | None = None
    member_id: str | None = None
    payer_id: str
    payer_name: str
    billing_provider_npi: str | None = None
    rendering_provider_npi: str | None = None
    rendering_provider_name: str | None = None
    referring_provider_npi: str | None = None
    place_of_service: str | None = None
    diagnosis_codes: list[str] = Field(default_factory=list)
    prior_auth_number: str | None = None
    original_submission_date: date | None = None
    claim_frequency_code: str = "1"
    original_claim_control_number: str | None = None
    line_number: int
    service_date: date
    procedure_code: str
    modifiers: list[str] = Field(default_factory=list)
    units: Decimal = Decimal("1")
    charge_amount: Decimal
    diagnosis_pointers: list[int] = Field(default_factory=list)
    ndc: str | None = None
    line_item_control_number: str | None = None
    claim_total_charge: Decimal | None = None

    @field_validator(
        "patient_sex",
        "patient_address_line1",
        "patient_city",
        "patient_state",
        "patient_zip",
        "member_id",
        "billing_provider_npi",
        "rendering_provider_npi",
        "rendering_provider_name",
        "referring_provider_npi",
        "place_of_service",
        "prior_auth_number",
        "original_claim_control_number",
        "ndc",
        "line_item_control_number",
        mode="before",
    )
    @classmethod
    def _empty_to_none(cls, v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("patient_dob", "original_submission_date", "claim_total_charge", mode="before")
    @classmethod
    def _empty_date_to_none(cls, v: Any) -> Any:
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("diagnosis_codes", "modifiers", mode="before")
    @classmethod
    def _pipe_list(cls, v: Any) -> list[str]:
        return _split_pipe(v)

    @field_validator("diagnosis_pointers", mode="before")
    @classmethod
    def _pipe_int_list(cls, v: Any) -> list[int]:
        return [int(x) for x in _split_pipe(v)]

    @field_validator("claim_frequency_code", mode="before")
    @classmethod
    def _freq(cls, v: Any) -> str:
        s = str(v).strip() if v is not None else ""
        return s or "1"


class NoteRow(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    claim_control_number: str
    note_date: date | None = None
    author: str | None = None
    text: str

    @field_validator("note_date", mode="before")
    @classmethod
    def _empty_date(cls, v: Any) -> Any:
        return None if isinstance(v, str) and not v.strip() else v


class PayerDocumentRow(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    claim_control_number: str
    document_type: str = "letter"
    received_date: date | None = None
    text: str

    @field_validator("received_date", mode="before")
    @classmethod
    def _empty_date(cls, v: Any) -> Any:
        return None if isinstance(v, str) and not v.strip() else v


# --------------------------------------------------------------------------------------
# Denial context: what the classifier, playbooks and drafter see
# --------------------------------------------------------------------------------------


class DenialReason(BaseModel):
    level: Literal["claim", "line"]
    group_code: str
    carc: str
    carc_text: str | None = None
    rarcs: list[str] = Field(default_factory=list)
    rarc_texts: dict[str, str] = Field(default_factory=dict)
    amount: Decimal = Decimal("0")
    line_number: int | None = None
    procedure_code: str | None = None
    modifiers: list[str] = Field(default_factory=list)


class ClaimLineContext(BaseModel):
    line_number: int
    service_date: date | None = None
    procedure_code: str
    modifiers: list[str] = Field(default_factory=list)
    units: Decimal = Decimal("1")
    charge_amount: Decimal = Decimal("0")
    diagnosis_pointers: list[int] = Field(default_factory=list)
    ndc: str | None = None
    line_item_control_number: str | None = None
    paid_amount: Decimal | None = None  # from the matching SVC, when known


class PatientIdentifiers(BaseModel):
    """Used ONLY to build the redaction registry. Never rendered into a prompt."""

    first_name: str
    last_name: str
    dob: date | None = None
    member_id: str | None = None
    address_line1: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    account_number: str | None = None


class ClaimContext(BaseModel):
    claim_id: int | None = None
    claim_control_number: str
    patient_token: str
    patient_identifiers: PatientIdentifiers | None = None
    payer_name: str
    payer_id: str
    billing_provider_npi: str | None = None
    rendering_provider_npi: str | None = None
    rendering_provider_name: str | None = None
    referring_provider_npi: str | None = None
    place_of_service: str | None = None
    diagnosis_codes: list[str] = Field(default_factory=list)
    prior_auth_number: str | None = None
    service_date_from: date | None = None
    service_date_to: date | None = None
    total_charge: Decimal = Decimal("0")
    original_submission_date: date | None = None
    frequency_code: str = "1"
    lines: list[ClaimLineContext] = Field(default_factory=list)


class PayerContext(BaseModel):
    name: str
    payer_id: str
    timely_filing_days: int = 90
    corrected_claim_days: int = 90
    appeal_days: int = 180


class RemittanceContext(BaseModel):
    remittance_claim_id: int | None = None
    payment_date: date | None = None
    claim_received_date: date | None = None
    claim_status_code: str
    charge_amount: Decimal = Decimal("0")
    paid_amount: Decimal = Decimal("0")
    patient_responsibility: Decimal = Decimal("0")
    payer_claim_control_number: str | None = None
    trace_number: str | None = None
    denied_amount: Decimal = Decimal("0")
    remark_codes: list[str] = Field(default_factory=list)
    # The payer's own spelling of the patient (NM1*QC / NM1*IL): registered for redaction too.
    patient_first_name: str | None = None
    patient_last_name: str | None = None
    member_id: str | None = None


class DocumentContext(BaseModel):
    document_id: int | None = None
    document_type: str = "letter"
    received_date: date | None = None
    text: str
    flagged_injection: bool = False
    injection_findings: list[InjectionFinding] = Field(default_factory=list)


class NoteContext(BaseModel):
    note_id: int | None = None
    note_date: date | None = None
    author: str | None = None
    text: str
    flagged_injection: bool = False  # planted instructions found at import or in build_context
    injection_findings: list[InjectionFinding] = Field(default_factory=list)


class DenialContext(BaseModel):
    claim: ClaimContext
    payer: PayerContext
    remittance: RemittanceContext
    reasons: list[DenialReason] = Field(default_factory=list)
    documents: list[DocumentContext] = Field(default_factory=list)
    notes: list[NoteContext] = Field(default_factory=list)
    today: date

    @property
    def primary_reason(self) -> DenialReason | None:
        return self.reasons[0] if self.reasons else None

    @property
    def has_flagged_documents(self) -> bool:
        return any(d.flagged_injection for d in self.documents)


# --------------------------------------------------------------------------------------
# Classification, proposals, citations
# --------------------------------------------------------------------------------------


class ClassificationResult(BaseModel):
    category: DenialCategory
    confidence: float = Field(ge=0, le=1)
    source: ClassificationSource = ClassificationSource.RULES
    rule_id: str | None = None
    rationale: str = ""
    injection_detected: bool = False
    secondary_categories: list[DenialCategory] = Field(default_factory=list)
    model_rationale: str | None = None


class Citation(BaseModel):
    kind: CitationKind
    ref: str  # e.g. "CO-16", "N29", "claim.lines[1].modifiers", "note:12", "document:3"
    text: str


class CorrectedClaimChange(BaseModel):
    path: str  # e.g. "lines[1].modifiers", "prior_auth_number", "payer_id"
    before: Any = None
    after: Any = None
    reason: str
    coding_change: bool = False
    citation_refs: list[str] = Field(default_factory=list)


class CorrectedClaim(BaseModel):
    before: dict[str, Any]
    after: dict[str, Any]
    changes: list[CorrectedClaimChange] = Field(default_factory=list)


class ProposalDraft(BaseModel):
    action_type: ActionType
    summary: str
    rationale: str
    citations: list[Citation] = Field(default_factory=list)
    corrected_claim: CorrectedClaim | None = None
    appeal_letter: str | None = None
    clinic_question: str | None = None
    coding_change: bool = False
    requires_note_excerpt: bool = False
    confidence: float = Field(default=0.5, ge=0, le=1)
    deadline: date | None = None
    deadline_kind: DeadlineKind | None = None
    flags: list[str] = Field(default_factory=list)
    prompt_version: str | None = None
    model_id: str | None = None
    generated_by: str = "playbook"


# --------------------------------------------------------------------------------------
# Model calls
# --------------------------------------------------------------------------------------


class LLMUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0


class RenderedPrompt(BaseModel):
    name: str
    version: str  # "<name>.v<N>@<sha8>"
    system: str
    user: str
    schema_: dict[str, Any] | None = Field(default=None, alias="schema")
    max_tokens: int = 2048

    model_config = ConfigDict(populate_by_name=True)


class LLMResult(BaseModel):
    content: dict[str, Any] | str
    model_id: str
    prompt_name: str
    prompt_version: str
    usage: LLMUsage = Field(default_factory=LLMUsage)
    request_id: str | None = None
    stop_reason: str | None = None
    status: ModelCallStatus = ModelCallStatus.OK
    latency_ms: int = 0
    error: str | None = None


class RedactionMap(BaseModel):
    tokens: dict[str, str] = Field(default_factory=dict)  # token -> original
    counts: dict[str, int] = Field(default_factory=dict)  # IdentifierKind -> count


class Identifier(BaseModel):
    kind: IdentifierKind
    value: str


class InjectionFinding(BaseModel):
    pattern: str
    snippet: str
    start: int
    end: int


class RedactionFinding(BaseModel):
    kind: str
    value_preview: str  # masked preview, never the full value
    location: str


# --------------------------------------------------------------------------------------
# Dashboard
# --------------------------------------------------------------------------------------


class CategoryMetric(BaseModel):
    key: str
    items: int = 0
    dollars_at_stake: Decimal = Decimal("0")
    submitted: int = 0
    paid: int = 0
    partial: int = 0
    denied_again: int = 0
    recovered_dollars: Decimal = Decimal("0")

    @property
    def overturn_rate(self) -> float | None:
        return (self.paid + self.partial) / self.submitted if self.submitted else None


class DeadlineBucket(BaseModel):
    label: str  # "overdue", "<=14 days", "<=30 days", ">30 days", "none"
    items: int = 0
    dollars: Decimal = Decimal("0")


class DashboardMetrics(BaseModel):
    generated_at: datetime
    total_work_items: int = 0
    items_by_status: dict[str, int] = Field(default_factory=dict)
    dollars_at_stake: Decimal = Decimal("0")
    submitted_fixes: int = 0
    paid_fixes: int = 0
    partial_fixes: int = 0
    denied_again: int = 0
    overturn_rate: float | None = None
    recovered_dollars: Decimal = Decimal("0")
    avg_review_minutes: float | None = None
    reviewed_items: int = 0
    by_category: list[CategoryMetric] = Field(default_factory=list)
    by_payer: list[CategoryMetric] = Field(default_factory=list)
    deadline_buckets: list[DeadlineBucket] = Field(default_factory=list)
    model_calls: int = 0
    model_input_tokens: int = 0
    model_output_tokens: int = 0
    audit_chain_ok: bool | None = None


DocumentContext.model_rebuild()
