"""Shared enumerations. Every module imports these; never redefine them elsewhere."""

from __future__ import annotations

from enum import StrEnum


class DenialCategory(StrEnum):
    ELIGIBILITY = "eligibility"
    AUTHORIZATION = "authorization"
    MODIFIER = "modifier"
    DUPLICATE = "duplicate"
    TIMELY_FILING = "timely_filing"
    MEDICAL_NECESSITY = "medical_necessity"
    BUNDLING = "bundling"
    COORDINATION_OF_BENEFITS = "coordination_of_benefits"
    MISSING_INFORMATION = "missing_information"
    CONTRACTUAL = "contractual"
    PATIENT_RESPONSIBILITY = "patient_responsibility"
    OTHER = "other"


# Categories that create review work. Contractual and patient-responsibility adjustments are
# informational and never create a work item on their own.
ACTIONABLE_CATEGORIES: frozenset[DenialCategory] = frozenset(
    {
        DenialCategory.ELIGIBILITY,
        DenialCategory.AUTHORIZATION,
        DenialCategory.MODIFIER,
        DenialCategory.DUPLICATE,
        DenialCategory.TIMELY_FILING,
        DenialCategory.MEDICAL_NECESSITY,
        DenialCategory.BUNDLING,
        DenialCategory.COORDINATION_OF_BENEFITS,
        DenialCategory.MISSING_INFORMATION,
        DenialCategory.OTHER,
    }
)

# When a claim carries several denial reasons, the category with the lowest index wins.
CATEGORY_PRIORITY: tuple[DenialCategory, ...] = (
    DenialCategory.TIMELY_FILING,
    DenialCategory.ELIGIBILITY,
    DenialCategory.COORDINATION_OF_BENEFITS,
    DenialCategory.AUTHORIZATION,
    DenialCategory.DUPLICATE,
    DenialCategory.BUNDLING,
    DenialCategory.MODIFIER,
    DenialCategory.MEDICAL_NECESSITY,
    DenialCategory.MISSING_INFORMATION,
    DenialCategory.OTHER,
    DenialCategory.CONTRACTUAL,
    DenialCategory.PATIENT_RESPONSIBILITY,
)


class ActionType(StrEnum):
    CORRECTED_CLAIM = "corrected_claim"
    APPEAL = "appeal"
    CLINIC_QUESTION = "clinic_question"
    NO_ACTION = "no_action"


class WorkItemStatus(StrEnum):
    NEW = "new"
    PROPOSED = "proposed"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPORTED = "exported"
    SUBMITTED = "submitted"
    CLOSED = "closed"


class ReviewActionType(StrEnum):
    VIEW = "view"
    APPROVE = "approve"
    EDIT = "edit"
    REJECT = "reject"
    EXPORT = "export"
    MARK_SUBMITTED = "mark_submitted"
    REOPEN = "reopen"
    CLOSE = "close"
    ATTACH_NOTE = "attach_note"


class ClaimStatus(StrEnum):
    SUBMITTED = "submitted"
    DENIED = "denied"
    PAID = "paid"
    PARTIAL = "partial"
    CORRECTED = "corrected"
    APPEALED = "appealed"
    CLOSED = "closed"


class OutcomeResult(StrEnum):
    PAID = "paid"
    PARTIAL = "partial"
    DENIED_AGAIN = "denied_again"
    REVERSED = "reversed"


class ClassificationSource(StrEnum):
    RULES = "rules"
    MODEL = "model"
    RULES_AND_MODEL = "rules+model"


class ActorType(StrEnum):
    USER = "user"
    AGENT = "agent"
    SYSTEM = "system"


class DeadlineKind(StrEnum):
    CORRECTED_CLAIM = "corrected_claim"
    APPEAL = "appeal"


class UserRole(StrEnum):
    REVIEWER = "reviewer"
    ADMIN = "admin"


class ImportKind(StrEnum):
    REMIT_835 = "835"
    CLAIMS = "claims"
    NOTES = "notes"
    PAYER_DOCUMENTS = "payer_documents"


class ImportStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"
    DUPLICATE = "duplicate"


class DocumentType(StrEnum):
    LETTER = "letter"
    PORTAL = "portal"
    OTHER = "other"


class CitationKind(StrEnum):
    CARC = "carc"
    RARC = "rarc"
    CLAIM_FIELD = "claim_field"
    REMITTANCE_FIELD = "remittance_field"
    NOTE_EXCERPT = "note_excerpt"
    PAYER_DOCUMENT = "payer_document"
    PAYER_POLICY = "payer_policy"


class ModelCallStatus(StrEnum):
    OK = "ok"
    REFUSAL = "refusal"
    ERROR = "error"


class IdentifierKind(StrEnum):
    """Kinds of identifiers the redaction layer tokenizes before a model call."""

    PATIENT_NAME = "PATIENT_NAME"
    MEMBER_ID = "MEMBER_ID"
    DOB = "DOB"
    ADDRESS = "ADDRESS"
    ACCOUNT = "ACCOUNT"
    CLAIM_ID = "CLAIM_ID"
    PROVIDER_NAME = "PROVIDER_NAME"
    DATE = "DATE"
