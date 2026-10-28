"""SQLAlchemy models (milestone M2).

Dialect-neutral on purpose: the test suite runs on SQLite and the runtime on PostgreSQL 16.
Money is Numeric(12, 2) (Decimal). Timestamps are timezone-aware UTC. JSON columns use the
generic JSON type.

Table-by-table purpose is documented in docs/SPEC.md section 2.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


Money = Numeric(12, 2)


# --------------------------------------------------------------------------------------
# Reference data
# --------------------------------------------------------------------------------------


class Clinic(Base):
    __tablename__ = "clinics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    npi: Mapped[str | None] = mapped_column(String(10))
    tax_id: Mapped[str | None] = mapped_column(String(20))
    pm_system: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    patients: Mapped[list[Patient]] = relationship(back_populates="clinic")
    claims: Mapped[list[Claim]] = relationship(back_populates="clinic")


class Payer(Base):
    __tablename__ = "payers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    payer_id: Mapped[str] = mapped_column(String(80), unique=True)  # X12 payer identifier
    timely_filing_days: Mapped[int] = mapped_column(Integer, default=90)
    corrected_claim_days: Mapped[int] = mapped_column(Integer, default=90)
    appeal_days: Mapped[int] = mapped_column(Integer, default=180)
    address: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    totp_secret: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(20), default="reviewer")  # UserRole
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# --------------------------------------------------------------------------------------
# Patients and claims (what the clinic sends)
# --------------------------------------------------------------------------------------


class Patient(Base):
    """PHI lives here, on the encrypted disk. Only `token` ever reaches a model."""

    __tablename__ = "patients"
    __table_args__ = (UniqueConstraint("clinic_id", "account_number", name="uq_patient_clinic_account"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    clinic_id: Mapped[int] = mapped_column(ForeignKey("clinics.id"))
    token: Mapped[str] = mapped_column(String(32), unique=True)  # e.g. PT-7F3A2C
    account_number: Mapped[str] = mapped_column(String(64))
    first_name: Mapped[str] = mapped_column(String(100))
    last_name: Mapped[str] = mapped_column(String(100))
    dob: Mapped[date | None] = mapped_column(Date)
    sex: Mapped[str | None] = mapped_column(String(1))
    address_line1: Mapped[str | None] = mapped_column(String(200))
    city: Mapped[str | None] = mapped_column(String(100))
    state: Mapped[str | None] = mapped_column(String(2))
    zip: Mapped[str | None] = mapped_column(String(10))
    member_id: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    clinic: Mapped[Clinic] = relationship(back_populates="patients")
    claims: Mapped[list[Claim]] = relationship(back_populates="patient")


class Claim(Base):
    __tablename__ = "claims"
    __table_args__ = (
        UniqueConstraint("clinic_id", "claim_control_number", name="uq_claim_clinic_ccn"),
        Index("ix_claims_patient", "patient_id"),
        Index("ix_claims_payer", "payer_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    clinic_id: Mapped[int] = mapped_column(ForeignKey("clinics.id"))
    payer_id: Mapped[int] = mapped_column(ForeignKey("payers.id"))
    patient_id: Mapped[int] = mapped_column(ForeignKey("patients.id"))
    claim_control_number: Mapped[str] = mapped_column(String(64))
    frequency_code: Mapped[str] = mapped_column(String(1), default="1")  # 1 original, 7 replacement, 8 void
    original_claim_id: Mapped[int | None] = mapped_column(ForeignKey("claims.id"))
    billing_provider_npi: Mapped[str | None] = mapped_column(String(10))
    rendering_provider_npi: Mapped[str | None] = mapped_column(String(10))
    rendering_provider_name: Mapped[str | None] = mapped_column(String(200))
    referring_provider_npi: Mapped[str | None] = mapped_column(String(10))
    place_of_service: Mapped[str | None] = mapped_column(String(2))
    diagnosis_codes: Mapped[list[Any]] = mapped_column(JSON, default=list)
    prior_auth_number: Mapped[str | None] = mapped_column(String(64))
    service_date_from: Mapped[date | None] = mapped_column(Date)
    service_date_to: Mapped[date | None] = mapped_column(Date)
    total_charge: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    original_submission_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="submitted")  # ClaimStatus
    import_batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batches.id"))
    extra: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    clinic: Mapped[Clinic] = relationship(back_populates="claims")
    payer: Mapped[Payer] = relationship()
    patient: Mapped[Patient] = relationship(back_populates="claims")
    lines: Mapped[list[ClaimLine]] = relationship(
        back_populates="claim", cascade="all, delete-orphan", order_by="ClaimLine.line_number"
    )
    original_claim: Mapped[Claim | None] = relationship(remote_side=[id], foreign_keys=[original_claim_id])
    notes: Mapped[list[NoteExcerpt]] = relationship(back_populates="claim", cascade="all, delete-orphan")
    documents: Mapped[list[PayerDocument]] = relationship(back_populates="claim", cascade="all, delete-orphan")
    work_items: Mapped[list[WorkItem]] = relationship(back_populates="claim")


class ClaimLine(Base):
    __tablename__ = "claim_lines"
    __table_args__ = (UniqueConstraint("claim_id", "line_number", name="uq_claim_line_number"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    claim_id: Mapped[int] = mapped_column(ForeignKey("claims.id"))
    line_number: Mapped[int] = mapped_column(Integer)
    service_date: Mapped[date | None] = mapped_column(Date)
    procedure_code: Mapped[str] = mapped_column(String(10))
    modifiers: Mapped[list[Any]] = mapped_column(JSON, default=list)
    units: Mapped[Decimal] = mapped_column(Numeric(8, 2), default=Decimal("1"))
    charge_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    diagnosis_pointers: Mapped[list[Any]] = mapped_column(JSON, default=list)
    ndc: Mapped[str | None] = mapped_column(String(20))
    line_item_control_number: Mapped[str | None] = mapped_column(String(64))

    claim: Mapped[Claim] = relationship(back_populates="lines")


class NoteExcerpt(Base):
    """Quoted clinical note excerpts supplied by the clinic for appeal drafting (minimum necessary)."""

    __tablename__ = "note_excerpts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    claim_id: Mapped[int] = mapped_column(ForeignKey("claims.id"))
    note_date: Mapped[date | None] = mapped_column(Date)
    author: Mapped[str | None] = mapped_column(String(200))
    text: Mapped[str] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    claim: Mapped[Claim] = relationship(back_populates="notes")


class PayerDocument(Base):
    """Payer letters / portal text. Untrusted data: scanned for planted instructions on import."""

    __tablename__ = "payer_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    claim_id: Mapped[int | None] = mapped_column(ForeignKey("claims.id"))
    remittance_claim_id: Mapped[int | None] = mapped_column(ForeignKey("remittance_claims.id"))
    document_type: Mapped[str] = mapped_column(String(20), default="letter")  # DocumentType
    received_date: Mapped[date | None] = mapped_column(Date)
    text: Mapped[str] = mapped_column(Text)
    flagged_injection: Mapped[bool] = mapped_column(Boolean, default=False)
    injection_findings: Mapped[list[Any]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    claim: Mapped[Claim | None] = relationship(back_populates="documents")


# --------------------------------------------------------------------------------------
# Remittances (what the payer sends: X12 835)
# --------------------------------------------------------------------------------------


class Remittance(Base):
    __tablename__ = "remittances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    payer_id: Mapped[int | None] = mapped_column(ForeignKey("payers.id"))
    file_name: Mapped[str] = mapped_column(String(255))
    file_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    transaction_index: Mapped[int] = mapped_column(Integer, default=0)
    trace_number: Mapped[str | None] = mapped_column(String(50))  # TRN02
    payer_identifier: Mapped[str | None] = mapped_column(String(50))  # TRN03
    payment_method: Mapped[str | None] = mapped_column(String(3))  # BPR04
    payment_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))  # BPR02
    payment_date: Mapped[date | None] = mapped_column(Date)  # BPR16
    production_date: Mapped[date | None] = mapped_column(Date)  # DTM*405
    payer_name: Mapped[str | None] = mapped_column(String(200))
    payee_npi: Mapped[str | None] = mapped_column(String(10))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    import_batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batches.id"))
    raw_meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    payer: Mapped[Payer | None] = relationship()
    claims: Mapped[list[RemittanceClaim]] = relationship(
        back_populates="remittance", cascade="all, delete-orphan", order_by="RemittanceClaim.sequence"
    )


class RemittanceClaim(Base):
    """One CLP loop."""

    __tablename__ = "remittance_claims"
    __table_args__ = (
        UniqueConstraint("remittance_id", "sequence", name="uq_remit_claim_sequence"),
        Index("ix_remit_claims_claim", "claim_id"),
        Index("ix_remit_claims_pcn", "patient_control_number"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    remittance_id: Mapped[int] = mapped_column(ForeignKey("remittances.id"))
    claim_id: Mapped[int | None] = mapped_column(ForeignKey("claims.id"))
    sequence: Mapped[int] = mapped_column(Integer)
    patient_control_number: Mapped[str] = mapped_column(String(64))  # CLP01
    claim_status_code: Mapped[str] = mapped_column(String(2))  # CLP02
    charge_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))  # CLP03
    paid_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))  # CLP04
    patient_responsibility: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))  # CLP05
    filing_indicator: Mapped[str | None] = mapped_column(String(2))  # CLP06
    payer_claim_control_number: Mapped[str | None] = mapped_column(String(64))  # CLP07
    facility_type_code: Mapped[str | None] = mapped_column(String(2))  # CLP08
    frequency_code: Mapped[str | None] = mapped_column(String(1))  # CLP09
    patient_last_name: Mapped[str | None] = mapped_column(String(100))
    patient_first_name: Mapped[str | None] = mapped_column(String(100))
    member_id: Mapped[str | None] = mapped_column(String(80))
    rendering_provider_npi: Mapped[str | None] = mapped_column(String(10))
    claim_received_date: Mapped[date | None] = mapped_column(Date)  # DTM*050
    statement_from: Mapped[date | None] = mapped_column(Date)  # DTM*232
    statement_to: Mapped[date | None] = mapped_column(Date)  # DTM*233
    remark_codes: Mapped[list[Any]] = mapped_column(JSON, default=list)  # claim-level LQ*HE
    is_denial: Mapped[bool] = mapped_column(Boolean, default=False)
    denied_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    match_method: Mapped[str | None] = mapped_column(String(40))

    remittance: Mapped[Remittance] = relationship(back_populates="claims")
    claim: Mapped[Claim | None] = relationship()
    lines: Mapped[list[RemittanceLine]] = relationship(
        back_populates="remittance_claim", cascade="all, delete-orphan", order_by="RemittanceLine.line_number"
    )
    adjustments: Mapped[list[Adjustment]] = relationship(
        back_populates="remittance_claim", cascade="all, delete-orphan"
    )
    work_items: Mapped[list[WorkItem]] = relationship(back_populates="remittance_claim")


class RemittanceLine(Base):
    """One SVC loop."""

    __tablename__ = "remittance_lines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    remittance_claim_id: Mapped[int] = mapped_column(ForeignKey("remittance_claims.id"))
    claim_line_id: Mapped[int | None] = mapped_column(ForeignKey("claim_lines.id"))
    line_number: Mapped[int] = mapped_column(Integer)
    procedure_code: Mapped[str | None] = mapped_column(String(10))
    modifiers: Mapped[list[Any]] = mapped_column(JSON, default=list)
    charge_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    paid_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    units: Mapped[Decimal] = mapped_column(Numeric(8, 2), default=Decimal("1"))
    service_date: Mapped[date | None] = mapped_column(Date)
    line_item_control_number: Mapped[str | None] = mapped_column(String(64))
    remark_codes: Mapped[list[Any]] = mapped_column(JSON, default=list)

    remittance_claim: Mapped[RemittanceClaim] = relationship(back_populates="lines")
    claim_line: Mapped[ClaimLine | None] = relationship()
    adjustments: Mapped[list[Adjustment]] = relationship(back_populates="remittance_line")


class Adjustment(Base):
    """One CAS group/code/amount triplet at claim or line level."""

    __tablename__ = "adjustments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    remittance_claim_id: Mapped[int] = mapped_column(ForeignKey("remittance_claims.id"))
    remittance_line_id: Mapped[int | None] = mapped_column(ForeignKey("remittance_lines.id"))
    level: Mapped[str] = mapped_column(String(5))  # claim | line
    group_code: Mapped[str] = mapped_column(String(2))  # CO PR OA PI CR
    carc: Mapped[str] = mapped_column(String(5))
    amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    is_denial: Mapped[bool] = mapped_column(Boolean, default=False)

    remittance_claim: Mapped[RemittanceClaim] = relationship(back_populates="adjustments")
    remittance_line: Mapped[RemittanceLine | None] = relationship(back_populates="adjustments")


# --------------------------------------------------------------------------------------
# Work: denials to review, proposals, human actions, outcomes
# --------------------------------------------------------------------------------------


class WorkItem(Base):
    __tablename__ = "work_items"
    __table_args__ = (
        Index("ix_work_items_status", "status"),
        Index("ix_work_items_deadline", "deadline"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    claim_id: Mapped[int] = mapped_column(ForeignKey("claims.id"))
    remittance_claim_id: Mapped[int] = mapped_column(ForeignKey("remittance_claims.id"))
    category: Mapped[str] = mapped_column(String(40))  # DenialCategory
    category_confidence: Mapped[float] = mapped_column(Numeric(4, 3), default=0)
    classification_source: Mapped[str] = mapped_column(String(20), default="rules")
    classification_rationale: Mapped[str | None] = mapped_column(Text)
    all_reasons: Mapped[list[Any]] = mapped_column(JSON, default=list)  # list[DenialReason]
    status: Mapped[str] = mapped_column(String(20), default="new")  # WorkItemStatus
    dollar_value: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    deadline: Mapped[date | None] = mapped_column(Date)
    deadline_kind: Mapped[str | None] = mapped_column(String(20))  # DeadlineKind
    priority_score: Mapped[float] = mapped_column(Numeric(12, 4), default=0)
    assigned_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    first_viewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    submitted_claim_control_number: Mapped[str | None] = mapped_column(String(64))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    flags: Mapped[list[Any]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    claim: Mapped[Claim] = relationship(back_populates="work_items")
    remittance_claim: Mapped[RemittanceClaim] = relationship(back_populates="work_items")
    proposals: Mapped[list[Proposal]] = relationship(
        back_populates="work_item", cascade="all, delete-orphan", order_by="Proposal.version"
    )
    actions: Mapped[list[ReviewAction]] = relationship(
        back_populates="work_item", cascade="all, delete-orphan", order_by="ReviewAction.created_at"
    )
    outcomes: Mapped[list[Outcome]] = relationship(back_populates="work_item")

    @property
    def current_proposal(self) -> Proposal | None:
        current = [p for p in self.proposals if p.is_current]
        if current:
            return current[-1]
        return self.proposals[-1] if self.proposals else None


class Proposal(Base):
    __tablename__ = "proposals"
    __table_args__ = (UniqueConstraint("work_item_id", "version", name="uq_proposal_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    work_item_id: Mapped[int] = mapped_column(ForeignKey("work_items.id"))
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_current: Mapped[bool] = mapped_column(Boolean, default=True)
    action_type: Mapped[str] = mapped_column(String(20))  # ActionType
    summary: Mapped[str] = mapped_column(Text)
    rationale: Mapped[str] = mapped_column(Text)
    citations: Mapped[list[Any]] = mapped_column(JSON, default=list)  # list[Citation]
    corrected_claim: Mapped[dict[str, Any] | None] = mapped_column(JSON)  # CorrectedClaim
    appeal_letter: Mapped[str | None] = mapped_column(Text)
    clinic_question: Mapped[str | None] = mapped_column(Text)
    coding_change: Mapped[bool] = mapped_column(Boolean, default=False)
    requires_note_excerpt: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence: Mapped[float] = mapped_column(Numeric(4, 3), default=0)
    prompt_version: Mapped[str | None] = mapped_column(String(120))
    model_id: Mapped[str | None] = mapped_column(String(80))
    generated_by: Mapped[str] = mapped_column(String(40), default="playbook")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    work_item: Mapped[WorkItem] = relationship(back_populates="proposals")


class ReviewAction(Base):
    """Every human decision on a work item."""

    __tablename__ = "actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    work_item_id: Mapped[int] = mapped_column(ForeignKey("work_items.id"))
    proposal_id: Mapped[int | None] = mapped_column(ForeignKey("proposals.id"))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(20))  # ReviewActionType
    reason: Mapped[str | None] = mapped_column(Text)
    edited_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    review_seconds: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    work_item: Mapped[WorkItem] = relationship(back_populates="actions")
    user: Mapped[User | None] = relationship()


class Outcome(Base):
    __tablename__ = "outcomes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    work_item_id: Mapped[int] = mapped_column(ForeignKey("work_items.id"))
    remittance_claim_id: Mapped[int] = mapped_column(ForeignKey("remittance_claims.id"))
    result: Mapped[str] = mapped_column(String(20))  # OutcomeResult
    paid_amount: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    payment_date: Mapped[date | None] = mapped_column(Date)
    matched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    work_item: Mapped[WorkItem] = relationship(back_populates="outcomes")
    remittance_claim: Mapped[RemittanceClaim] = relationship()


# --------------------------------------------------------------------------------------
# Operations: audit, imports, model calls
# --------------------------------------------------------------------------------------


class AuditLog(Base):
    """Append-only. ORM listeners in app.audit.log refuse UPDATE/DELETE; Postgres adds a trigger."""

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_claim", "claim_id"),
        Index("ix_audit_entity", "entity_type", "entity_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    actor_type: Mapped[str] = mapped_column(String(10))  # ActorType
    actor_id: Mapped[str] = mapped_column(String(80))
    action: Mapped[str] = mapped_column(String(60))
    entity_type: Mapped[str | None] = mapped_column(String(40))
    entity_id: Mapped[int | None] = mapped_column(Integer)
    claim_id: Mapped[int | None] = mapped_column(Integer)
    reason: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    prev_hash: Mapped[str | None] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))


class ImportBatch(Base):
    __tablename__ = "import_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))  # ImportKind
    file_name: Mapped[str] = mapped_column(String(255))
    file_sha256: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="pending")  # ImportStatus
    counts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str | None] = mapped_column(String(80))


class ModelCall(Base):
    """One record per model call. Counts and hashes only, never content."""

    __tablename__ = "model_calls"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    prompt_name: Mapped[str] = mapped_column(String(80))
    prompt_version: Mapped[str] = mapped_column(String(120))
    model_id: Mapped[str] = mapped_column(String(80))
    purpose: Mapped[str] = mapped_column(String(80))
    work_item_id: Mapped[int | None] = mapped_column(ForeignKey("work_items.id"))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    redaction_counts: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    request_id: Mapped[str | None] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(10), default="ok")  # ModelCallStatus
    payload_sha256: Mapped[str | None] = mapped_column(String(64))


__all__ = [
    "Base",
    "Clinic",
    "Payer",
    "User",
    "Patient",
    "Claim",
    "ClaimLine",
    "NoteExcerpt",
    "PayerDocument",
    "Remittance",
    "RemittanceClaim",
    "RemittanceLine",
    "Adjustment",
    "WorkItem",
    "Proposal",
    "ReviewAction",
    "Outcome",
    "AuditLog",
    "ImportBatch",
    "ModelCall",
    "utcnow",
]
