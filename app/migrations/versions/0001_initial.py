"""initial schema (milestone M2)

Generated with `alembic revision --autogenerate` against a temporary SQLite database and then
hand-cleaned: tables are created in dependency order, indexes use plain `op.create_index` (no
SQLite batch quirks), Numeric precisions match app/models.py, and the PostgreSQL-only append-only
guard on audit_log (function + BEFORE UPDATE/DELETE triggers) is added under a dialect check.

Revision ID: 0001
Revises:
Create Date: 2026-10-28

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MONEY = sa.Numeric(precision=12, scale=2)

AUDIT_TRIGGER_FUNCTION = """
CREATE OR REPLACE FUNCTION audit_log_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only';
END;
$$ LANGUAGE plpgsql;
"""


def _dialect_name() -> str:
    bind = op.get_bind()
    if bind is not None:
        return bind.dialect.name
    return op.get_context().dialect.name  # offline (--sql) mode


def upgrade() -> None:
    # ---- reference data ------------------------------------------------------------------
    op.create_table(
        "clinics",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("npi", sa.String(length=10), nullable=True),
        sa.Column("tax_id", sa.String(length=20), nullable=True),
        sa.Column("pm_system", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "payers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("payer_id", sa.String(length=80), nullable=False),
        sa.Column("timely_filing_days", sa.Integer(), nullable=False),
        sa.Column("corrected_claim_days", sa.Integer(), nullable=False),
        sa.Column("appeal_days", sa.Integer(), nullable=False),
        sa.Column("address", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("payer_id"),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("email", sa.String(length=254), nullable=False),
        sa.Column("password_hash", sa.String(length=255), nullable=False),
        sa.Column("totp_secret", sa.String(length=64), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("failed_logins", sa.Integer(), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )
    op.create_table(
        "import_batches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("file_name", sa.String(length=255), nullable=False),
        sa.Column("file_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("counts", sa.JSON(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=80), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )

    # ---- patients and claims (what the clinic sends) -----------------------------------------
    op.create_table(
        "patients",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("clinic_id", sa.Integer(), nullable=False),
        sa.Column("token", sa.String(length=32), nullable=False),
        sa.Column("account_number", sa.String(length=64), nullable=False),
        sa.Column("first_name", sa.String(length=100), nullable=False),
        sa.Column("last_name", sa.String(length=100), nullable=False),
        sa.Column("dob", sa.Date(), nullable=True),
        sa.Column("sex", sa.String(length=1), nullable=True),
        sa.Column("address_line1", sa.String(length=200), nullable=True),
        sa.Column("city", sa.String(length=100), nullable=True),
        sa.Column("state", sa.String(length=2), nullable=True),
        sa.Column("zip", sa.String(length=10), nullable=True),
        sa.Column("member_id", sa.String(length=80), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["clinic_id"], ["clinics.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("clinic_id", "account_number", name="uq_patient_clinic_account"),
        sa.UniqueConstraint("token"),
    )
    op.create_table(
        "claims",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("clinic_id", sa.Integer(), nullable=False),
        sa.Column("payer_id", sa.Integer(), nullable=False),
        sa.Column("patient_id", sa.Integer(), nullable=False),
        sa.Column("claim_control_number", sa.String(length=64), nullable=False),
        sa.Column("frequency_code", sa.String(length=1), nullable=False),
        sa.Column("original_claim_id", sa.Integer(), nullable=True),
        sa.Column("billing_provider_npi", sa.String(length=10), nullable=True),
        sa.Column("rendering_provider_npi", sa.String(length=10), nullable=True),
        sa.Column("rendering_provider_name", sa.String(length=200), nullable=True),
        sa.Column("referring_provider_npi", sa.String(length=10), nullable=True),
        sa.Column("place_of_service", sa.String(length=2), nullable=True),
        sa.Column("diagnosis_codes", sa.JSON(), nullable=False),
        sa.Column("prior_auth_number", sa.String(length=64), nullable=True),
        sa.Column("service_date_from", sa.Date(), nullable=True),
        sa.Column("service_date_to", sa.Date(), nullable=True),
        sa.Column("total_charge", MONEY, nullable=False),
        sa.Column("original_submission_date", sa.Date(), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("import_batch_id", sa.Integer(), nullable=True),
        sa.Column("extra", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["clinic_id"], ["clinics.id"]),
        sa.ForeignKeyConstraint(["import_batch_id"], ["import_batches.id"]),
        sa.ForeignKeyConstraint(["original_claim_id"], ["claims.id"]),
        sa.ForeignKeyConstraint(["patient_id"], ["patients.id"]),
        sa.ForeignKeyConstraint(["payer_id"], ["payers.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("clinic_id", "claim_control_number", name="uq_claim_clinic_ccn"),
    )
    op.create_index("ix_claims_patient", "claims", ["patient_id"], unique=False)
    op.create_index("ix_claims_payer", "claims", ["payer_id"], unique=False)
    op.create_table(
        "claim_lines",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("claim_id", sa.Integer(), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("service_date", sa.Date(), nullable=True),
        sa.Column("procedure_code", sa.String(length=10), nullable=False),
        sa.Column("modifiers", sa.JSON(), nullable=False),
        sa.Column("units", sa.Numeric(precision=8, scale=2), nullable=False),
        sa.Column("charge_amount", MONEY, nullable=False),
        sa.Column("diagnosis_pointers", sa.JSON(), nullable=False),
        sa.Column("ndc", sa.String(length=20), nullable=True),
        sa.Column("line_item_control_number", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["claim_id"], ["claims.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("claim_id", "line_number", name="uq_claim_line_number"),
    )
    op.create_table(
        "note_excerpts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("claim_id", sa.Integer(), nullable=False),
        sa.Column("note_date", sa.Date(), nullable=True),
        sa.Column("author", sa.String(length=200), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["claim_id"], ["claims.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    # ---- remittances (what the payer sends: X12 835) -----------------------------------------
    op.create_table(
        "remittances",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("payer_id", sa.Integer(), nullable=True),
        sa.Column("file_name", sa.String(length=255), nullable=False),
        sa.Column("file_sha256", sa.String(length=64), nullable=False),
        sa.Column("transaction_index", sa.Integer(), nullable=False),
        sa.Column("trace_number", sa.String(length=50), nullable=True),
        sa.Column("payer_identifier", sa.String(length=50), nullable=True),
        sa.Column("payment_method", sa.String(length=3), nullable=True),
        sa.Column("payment_amount", MONEY, nullable=False),
        sa.Column("payment_date", sa.Date(), nullable=True),
        sa.Column("production_date", sa.Date(), nullable=True),
        sa.Column("payer_name", sa.String(length=200), nullable=True),
        sa.Column("payee_npi", sa.String(length=10), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("import_batch_id", sa.Integer(), nullable=True),
        sa.Column("raw_meta", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["import_batch_id"], ["import_batches.id"]),
        sa.ForeignKeyConstraint(["payer_id"], ["payers.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("file_sha256", "transaction_index", name="uq_remittance_file_transaction"),
    )
    op.create_table(
        "remittance_claims",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("remittance_id", sa.Integer(), nullable=False),
        sa.Column("claim_id", sa.Integer(), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("patient_control_number", sa.String(length=64), nullable=False),
        sa.Column("claim_status_code", sa.String(length=2), nullable=False),
        sa.Column("charge_amount", MONEY, nullable=False),
        sa.Column("paid_amount", MONEY, nullable=False),
        sa.Column("patient_responsibility", MONEY, nullable=False),
        sa.Column("filing_indicator", sa.String(length=2), nullable=True),
        sa.Column("payer_claim_control_number", sa.String(length=64), nullable=True),
        sa.Column("facility_type_code", sa.String(length=2), nullable=True),
        sa.Column("frequency_code", sa.String(length=1), nullable=True),
        sa.Column("patient_last_name", sa.String(length=100), nullable=True),
        sa.Column("patient_first_name", sa.String(length=100), nullable=True),
        sa.Column("member_id", sa.String(length=80), nullable=True),
        sa.Column("rendering_provider_npi", sa.String(length=10), nullable=True),
        sa.Column("claim_received_date", sa.Date(), nullable=True),
        sa.Column("statement_from", sa.Date(), nullable=True),
        sa.Column("statement_to", sa.Date(), nullable=True),
        sa.Column("remark_codes", sa.JSON(), nullable=False),
        sa.Column("is_denial", sa.Boolean(), nullable=False),
        sa.Column("denied_amount", MONEY, nullable=False),
        sa.Column("match_method", sa.String(length=40), nullable=True),
        sa.ForeignKeyConstraint(["claim_id"], ["claims.id"]),
        sa.ForeignKeyConstraint(["remittance_id"], ["remittances.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("remittance_id", "sequence", name="uq_remit_claim_sequence"),
    )
    op.create_index("ix_remit_claims_claim", "remittance_claims", ["claim_id"], unique=False)
    op.create_index("ix_remit_claims_pcn", "remittance_claims", ["patient_control_number"], unique=False)
    op.create_table(
        "remittance_lines",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("remittance_claim_id", sa.Integer(), nullable=False),
        sa.Column("claim_line_id", sa.Integer(), nullable=True),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("procedure_code", sa.String(length=10), nullable=True),
        sa.Column("modifiers", sa.JSON(), nullable=False),
        sa.Column("charge_amount", MONEY, nullable=False),
        sa.Column("paid_amount", MONEY, nullable=False),
        sa.Column("units", sa.Numeric(precision=8, scale=2), nullable=False),
        sa.Column("service_date", sa.Date(), nullable=True),
        sa.Column("line_item_control_number", sa.String(length=64), nullable=True),
        sa.Column("remark_codes", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["claim_line_id"], ["claim_lines.id"]),
        sa.ForeignKeyConstraint(["remittance_claim_id"], ["remittance_claims.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "adjustments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("remittance_claim_id", sa.Integer(), nullable=False),
        sa.Column("remittance_line_id", sa.Integer(), nullable=True),
        sa.Column("level", sa.String(length=5), nullable=False),
        sa.Column("group_code", sa.String(length=2), nullable=False),
        sa.Column("carc", sa.String(length=5), nullable=False),
        sa.Column("amount", MONEY, nullable=False),
        sa.Column("quantity", sa.Numeric(precision=8, scale=2), nullable=True),
        sa.Column("is_denial", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["remittance_claim_id"], ["remittance_claims.id"]),
        sa.ForeignKeyConstraint(["remittance_line_id"], ["remittance_lines.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "payer_documents",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("claim_id", sa.Integer(), nullable=True),
        sa.Column("remittance_claim_id", sa.Integer(), nullable=True),
        sa.Column("document_type", sa.String(length=20), nullable=False),
        sa.Column("received_date", sa.Date(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("flagged_injection", sa.Boolean(), nullable=False),
        sa.Column("injection_findings", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["claim_id"], ["claims.id"]),
        sa.ForeignKeyConstraint(["remittance_claim_id"], ["remittance_claims.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    # ---- work: denials to review, proposals, human actions, outcomes ---------------------------
    op.create_table(
        "work_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("claim_id", sa.Integer(), nullable=False),
        sa.Column("remittance_claim_id", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(length=40), nullable=False),
        sa.Column("category_confidence", sa.Numeric(precision=4, scale=3), nullable=False),
        sa.Column("classification_source", sa.String(length=20), nullable=False),
        sa.Column("classification_rationale", sa.Text(), nullable=True),
        sa.Column("all_reasons", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("dollar_value", MONEY, nullable=False),
        sa.Column("deadline", sa.Date(), nullable=True),
        sa.Column("deadline_kind", sa.String(length=20), nullable=True),
        sa.Column("priority_score", sa.Numeric(precision=12, scale=4), nullable=False),
        sa.Column("assigned_user_id", sa.Integer(), nullable=True),
        sa.Column("first_viewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_claim_control_number", sa.String(length=64), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("flags", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["assigned_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["claim_id"], ["claims.id"]),
        sa.ForeignKeyConstraint(["remittance_claim_id"], ["remittance_claims.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_work_items_deadline", "work_items", ["deadline"], unique=False)
    op.create_index("ix_work_items_status", "work_items", ["status"], unique=False)
    op.create_table(
        "proposals",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("work_item_id", sa.Integer(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("is_current", sa.Boolean(), nullable=False),
        sa.Column("action_type", sa.String(length=20), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("citations", sa.JSON(), nullable=False),
        sa.Column("corrected_claim", sa.JSON(), nullable=True),
        sa.Column("appeal_letter", sa.Text(), nullable=True),
        sa.Column("clinic_question", sa.Text(), nullable=True),
        sa.Column("coding_change", sa.Boolean(), nullable=False),
        sa.Column("requires_note_excerpt", sa.Boolean(), nullable=False),
        sa.Column("confidence", sa.Numeric(precision=4, scale=3), nullable=False),
        sa.Column("prompt_version", sa.String(length=120), nullable=True),
        sa.Column("model_id", sa.String(length=80), nullable=True),
        sa.Column("generated_by", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["work_item_id"], ["work_items.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("work_item_id", "version", name="uq_proposal_version"),
    )
    op.create_table(
        "actions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("work_item_id", sa.Integer(), nullable=False),
        sa.Column("proposal_id", sa.Integer(), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("action", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("edited_payload", sa.JSON(), nullable=True),
        sa.Column("review_seconds", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["proposal_id"], ["proposals.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["work_item_id"], ["work_items.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "outcomes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("work_item_id", sa.Integer(), nullable=False),
        sa.Column("remittance_claim_id", sa.Integer(), nullable=False),
        sa.Column("result", sa.String(length=20), nullable=False),
        sa.Column("paid_amount", MONEY, nullable=False),
        sa.Column("payment_date", sa.Date(), nullable=True),
        sa.Column("matched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["remittance_claim_id"], ["remittance_claims.id"]),
        sa.ForeignKeyConstraint(["work_item_id"], ["work_items.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    # ---- operations: audit, model calls ---------------------------------------------------------
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor_type", sa.String(length=10), nullable=False),
        sa.Column("actor_id", sa.String(length=80), nullable=False),
        sa.Column("action", sa.String(length=60), nullable=False),
        sa.Column("entity_type", sa.String(length=40), nullable=True),
        sa.Column("entity_id", sa.Integer(), nullable=True),
        sa.Column("claim_id", sa.Integer(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("prev_hash", sa.String(length=64), nullable=True),
        sa.Column("hash", sa.String(length=64), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_claim", "audit_log", ["claim_id"], unique=False)
    op.create_index("ix_audit_entity", "audit_log", ["entity_type", "entity_id"], unique=False)
    op.create_table(
        "model_calls",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("prompt_name", sa.String(length=80), nullable=False),
        sa.Column("prompt_version", sa.String(length=120), nullable=False),
        sa.Column("model_id", sa.String(length=80), nullable=False),
        sa.Column("purpose", sa.String(length=80), nullable=False),
        sa.Column("work_item_id", sa.Integer(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("redaction_counts", sa.JSON(), nullable=False),
        sa.Column("request_id", sa.String(length=80), nullable=True),
        sa.Column("status", sa.String(length=10), nullable=False),
        sa.Column("error", sa.String(length=80), nullable=True),
        sa.Column("payload_sha256", sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(["work_item_id"], ["work_items.id"]),
        sa.PrimaryKeyConstraint("id"),
    )

    # ---- PostgreSQL only: audit_log is append-only at the database level too ---------------------
    if _dialect_name() == "postgresql":
        op.execute(AUDIT_TRIGGER_FUNCTION)
        op.execute(
            "CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log "
            "FOR EACH ROW EXECUTE FUNCTION audit_log_immutable();"
        )
        op.execute(
            "CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log "
            "FOR EACH ROW EXECUTE FUNCTION audit_log_immutable();"
        )


def downgrade() -> None:
    if _dialect_name() == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS audit_log_no_delete ON audit_log;")
        op.execute("DROP TRIGGER IF EXISTS audit_log_no_update ON audit_log;")
        op.execute("DROP FUNCTION IF EXISTS audit_log_immutable();")

    op.drop_table("model_calls")
    op.drop_index("ix_audit_entity", table_name="audit_log")
    op.drop_index("ix_audit_claim", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_table("outcomes")
    op.drop_table("actions")
    op.drop_table("proposals")
    op.drop_index("ix_work_items_status", table_name="work_items")
    op.drop_index("ix_work_items_deadline", table_name="work_items")
    op.drop_table("work_items")
    op.drop_table("payer_documents")
    op.drop_table("adjustments")
    op.drop_table("remittance_lines")
    op.drop_index("ix_remit_claims_pcn", table_name="remittance_claims")
    op.drop_index("ix_remit_claims_claim", table_name="remittance_claims")
    op.drop_table("remittance_claims")
    op.drop_table("remittances")
    op.drop_table("note_excerpts")
    op.drop_table("claim_lines")
    op.drop_index("ix_claims_payer", table_name="claims")
    op.drop_index("ix_claims_patient", table_name="claims")
    op.drop_table("claims")
    op.drop_table("patients")
    op.drop_table("import_batches")
    op.drop_table("users")
    op.drop_table("payers")
    op.drop_table("clinics")
