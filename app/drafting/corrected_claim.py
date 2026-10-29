"""Corrected-claim builder: claim-export dict, path-based edits, before/after diff and CSV rows.

A corrected claim is a replacement claim (frequency code 7) that references the claim it replaces.
`build_corrected_claim` applies a list of `CorrectedClaimChange` to a copy of the claim-export dict
and never touches anything a change does not name, so the diff shown to the reviewer is exactly the
list of edits the playbook proposed plus the replacement header (control number, frequency code).

Nothing here submits anything: the CSV rows are written into the export package for a human.
"""

from __future__ import annotations

import copy
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from app.schemas import (
    CLAIM_EXPORT_COLUMNS,
    ClaimContext,
    ClaimLineContext,
    CorrectedClaim,
    CorrectedClaimChange,
    DenialContext,
)

# Claim-level paths a change may name (SPEC 4.1 column names).
CLAIM_PATHS: frozenset[str] = frozenset(
    {
        "prior_auth_number",
        "payer_id",
        "payer_name",
        "diagnosis_codes",
        "referring_provider_npi",
        "rendering_provider_npi",
        "rendering_provider_name",
        "billing_provider_npi",
        "place_of_service",
        "original_submission_date",
    }
)

# Line-level paths: `lines[<n>].<field>` where <n> is the 1-based line_number (not a list index).
LINE_PATHS: frozenset[str] = frozenset(
    {"modifiers", "ndc", "procedure_code", "diagnosis_pointers", "units", "service_date", "charge_amount"}
)

LIST_FIELDS: frozenset[str] = frozenset({"diagnosis_codes", "modifiers", "diagnosis_pointers"})
MONEY_FIELDS: frozenset[str] = frozenset({"charge_amount", "claim_total_charge"})
DATE_FIELDS: frozenset[str] = frozenset(
    {"service_date", "original_submission_date", "service_date_from", "service_date_to"}
)

LINE_COLUMNS: tuple[str, ...] = (
    "line_number",
    "service_date",
    "procedure_code",
    "modifiers",
    "units",
    "charge_amount",
    "diagnosis_pointers",
    "ndc",
    "line_item_control_number",
)

_PATH_RE = re.compile(r"^(?:lines\[(?P<line>\d+)\]\.)?(?P<field>[a-z_]+)$")
_REVISION_RE = re.compile(r"(?:-R\d+)+$")

# Human labels for the diff view.
_LABELS: dict[str, str] = {
    "prior_auth_number": "Prior authorization number",
    "payer_id": "Payer ID",
    "payer_name": "Payer",
    "diagnosis_codes": "Diagnosis codes",
    "referring_provider_npi": "Referring provider NPI",
    "rendering_provider_npi": "Rendering provider NPI",
    "rendering_provider_name": "Rendering provider",
    "billing_provider_npi": "Billing provider NPI",
    "place_of_service": "Place of service",
    "original_submission_date": "Original submission date",
    "modifiers": "Modifiers",
    "ndc": "NDC",
    "procedure_code": "Procedure code",
    "diagnosis_pointers": "Diagnosis pointers",
    "units": "Units",
    "service_date": "Service date",
    "charge_amount": "Charge amount",
    "claim_control_number": "Claim control number",
    "claim_frequency_code": "Claim frequency code",
    "original_claim_control_number": "Original claim control number",
}


# --------------------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------------------


def format_money(value: Any) -> str:
    """Money with two decimals; empty string for None."""
    if value is None or value == "":
        return ""
    try:
        return f"{Decimal(str(value)):.2f}"
    except (InvalidOperation, ValueError):
        return str(value)


def format_units(value: Any) -> str:
    """Units without trailing zeros (1.00 -> 1, 1.50 -> 1.5)."""
    if value is None or value == "":
        return ""
    try:
        return format(Decimal(str(value)).normalize(), "f")
    except (InvalidOperation, ValueError):
        return str(value)


def format_date(value: Any) -> str | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _coerce(field: str, value: Any) -> Any:
    """Coerce a change's `after` value into the export-dict representation of the field."""
    if value is None:
        return [] if field in LIST_FIELDS else None
    if field == "diagnosis_pointers":
        items = value if isinstance(value, list | tuple) else str(value).split("|")
        return [int(str(x).strip()) for x in items if str(x).strip()]
    if field in LIST_FIELDS:
        items = value if isinstance(value, list | tuple) else str(value).split("|")
        return [str(x).strip() for x in items if str(x).strip()]
    if field == "units":
        return format_units(value)
    if field in MONEY_FIELDS:
        return format_money(value)
    if field in DATE_FIELDS:
        return format_date(value)
    return str(value).strip() or None


# --------------------------------------------------------------------------------------
# Export dict
# --------------------------------------------------------------------------------------


def line_to_export_dict(line: ClaimLineContext) -> dict[str, Any]:
    return {
        "line_number": line.line_number,
        "service_date": format_date(line.service_date),
        "procedure_code": line.procedure_code,
        "modifiers": list(line.modifiers),
        "units": format_units(line.units),
        "charge_amount": format_money(line.charge_amount),
        "diagnosis_pointers": [int(p) for p in line.diagnosis_pointers],
        "ndc": line.ndc,
        "line_item_control_number": line.line_item_control_number,
    }


def claim_to_export_dict(claim: ClaimContext) -> dict[str, Any]:
    """The claim in the SPEC 4.1 export shape: claim-level keys plus `lines`.

    Patient name, date of birth, address and member id are not part of this dict; the export step
    supplies them from the patient record (`patient_fields` of `corrected_claim_to_csv_rows`).
    """
    identifiers = claim.patient_identifiers
    return {
        "claim_control_number": claim.claim_control_number,
        "patient_token": claim.patient_token,
        "patient_account_number": identifiers.account_number if identifiers else None,
        "payer_id": claim.payer_id,
        "payer_name": claim.payer_name,
        "billing_provider_npi": claim.billing_provider_npi,
        "rendering_provider_npi": claim.rendering_provider_npi,
        "rendering_provider_name": claim.rendering_provider_name,
        "referring_provider_npi": claim.referring_provider_npi,
        "place_of_service": claim.place_of_service,
        "diagnosis_codes": list(claim.diagnosis_codes),
        "prior_auth_number": claim.prior_auth_number,
        "original_submission_date": format_date(claim.original_submission_date),
        "claim_frequency_code": claim.frequency_code or "1",
        "original_claim_control_number": None,
        "service_date_from": format_date(claim.service_date_from),
        "service_date_to": format_date(claim.service_date_to),
        "claim_total_charge": format_money(claim.total_charge),
        "lines": [line_to_export_dict(line) for line in claim.lines],
    }


# --------------------------------------------------------------------------------------
# Paths
# --------------------------------------------------------------------------------------


def parse_path(path: str) -> tuple[int | None, str]:
    """'lines[2].modifiers' -> (2, 'modifiers'); 'prior_auth_number' -> (None, 'prior_auth_number')."""
    m = _PATH_RE.match(path.strip())
    if not m:
        raise ValueError(f"unsupported corrected-claim path: {path!r}")
    line = m.group("line")
    field = m.group("field")
    if line is not None:
        if field not in LINE_PATHS:
            raise ValueError(f"unsupported line field in path: {path!r}")
        return int(line), field
    if field not in CLAIM_PATHS:
        raise ValueError(f"unsupported claim field in path: {path!r}")
    return None, field


def _find_line(data: dict[str, Any], line_number: int) -> dict[str, Any]:
    for line in data.get("lines", []):
        if int(line.get("line_number", -1)) == line_number:
            return line
    raise ValueError(f"corrected-claim path names line {line_number}, which the claim does not have")


def resolve_path(data: dict[str, Any], path: str) -> Any:
    """Current value at `path` in an export dict (raises ValueError for an unknown path)."""
    line_number, field = parse_path(path)
    if line_number is None:
        return data.get(field)
    return _find_line(data, line_number).get(field)


def set_path(data: dict[str, Any], path: str, value: Any) -> None:
    line_number, field = parse_path(path)
    coerced = _coerce(field, value)
    if line_number is None:
        data[field] = coerced
    else:
        _find_line(data, line_number)[field] = coerced


# --------------------------------------------------------------------------------------
# Control numbers
# --------------------------------------------------------------------------------------


def base_control_number(ccn: str) -> str:
    """Strip any '-R<k>' revision suffixes: 'C1-R2' -> 'C1'."""
    return _REVISION_RE.sub("", ccn.strip())


def next_control_number(ccn: str) -> str:
    """'C1' -> 'C1-R1', 'C1-R1' -> 'C1-R2', 'C1-R2' -> 'C1-R3'.

    k is one more than the highest revision already in the control number (the number of '-R'
    suffixes is a floor, so a stacked 'C1-R1-R1' still moves forward).
    """
    base = base_control_number(ccn)
    revisions = [int(n) for n in re.findall(r"-R(\d+)", ccn[len(base) :])]
    k = max(len(revisions), max(revisions, default=0)) + 1
    return f"{base}-R{k}"


# --------------------------------------------------------------------------------------
# Builder
# --------------------------------------------------------------------------------------


def build_corrected_claim(ctx: DenialContext, changes: list[CorrectedClaimChange]) -> CorrectedClaim:
    """Apply `changes` to the claim and return the before/after export dicts plus the change list.

    - `after['claim_frequency_code']` is '7' (replacement of a prior claim)
    - `after['original_claim_control_number']` is the control number of the claim being replaced
    - `after['claim_control_number']` is '<base>-R<k>'
    - `after['claim_total_charge']` is recomputed from the lines
    - a change whose `before` is None is filled in from the claim so the diff is always complete
    """
    before = claim_to_export_dict(ctx.claim)
    after = copy.deepcopy(before)
    applied: list[CorrectedClaimChange] = []
    for change in changes:
        current = resolve_path(before, change.path)
        set_path(after, change.path, change.after)
        applied.append(
            change.model_copy(
                update={
                    "before": copy.deepcopy(current) if change.before is None else change.before,
                    "after": resolve_path(after, change.path),
                }
            )
        )
    original = ctx.claim.claim_control_number
    after["claim_frequency_code"] = "7"
    after["original_claim_control_number"] = original
    after["claim_control_number"] = next_control_number(original)
    after["claim_total_charge"] = format_money(
        sum((Decimal(line.get("charge_amount") or "0") for line in after.get("lines", [])), Decimal("0"))
    )
    return CorrectedClaim(before=before, after=after, changes=applied)


# --------------------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------------------


def _csv_value(column: str, value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list | tuple):
        return "|".join(str(x) for x in value)
    if column in MONEY_FIELDS:
        return format_money(value)
    if column == "units":
        return format_units(value)
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def corrected_claim_to_csv_rows(
    corrected: CorrectedClaim, patient_fields: dict[str, Any] | None = None
) -> list[dict[str, str]]:
    """One row per line of the corrected claim, keys in `CLAIM_EXPORT_COLUMNS` order.

    `patient_fields` supplies the patient block of the export (patient_first_name, patient_last_name,
    patient_dob, patient_sex, patient_address_line1, patient_city, patient_state, patient_zip,
    member_id and optionally patient_account_number) from the patient record; the corrected-claim
    dict carries no patient identifiers besides the account number.
    """
    after = corrected.after
    extra = patient_fields or {}
    rows: list[dict[str, str]] = []
    for line in after.get("lines", []):
        row: dict[str, str] = {}
        for column in CLAIM_EXPORT_COLUMNS:
            if column in line:
                value = line[column]
            elif column in extra:
                value = extra[column]
            else:
                value = after.get(column)
            row[column] = _csv_value(column, value)
        rows.append(row)
    return rows


def _display(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list | tuple):
        return ", ".join(str(x) for x in value)
    return str(value)


def _split_path(path: str) -> tuple[int | None, str]:
    try:
        return parse_path(path)
    except ValueError:
        return None, path


def _label(path: str) -> str:
    line_number, field = _split_path(path)
    base = _LABELS.get(field, field.replace("_", " ").capitalize())
    return f"Line {line_number}: {base}" if line_number is not None else base


def render_diff(corrected: CorrectedClaim) -> list[dict[str, Any]]:
    """Rows for the UI diff: the proposed edits first, then the replacement-claim header fields."""
    rows: list[dict[str, Any]] = []
    for change in corrected.changes:
        line_number, _field = _split_path(change.path)
        rows.append(
            {
                "path": change.path,
                "label": _label(change.path),
                "line_number": line_number,
                "before": _display(change.before),
                "after": _display(change.after),
                "changed": change.before != change.after,
                "coding_change": bool(change.coding_change),
                "reason": change.reason,
                "citation_refs": list(change.citation_refs),
                "system": False,
            }
        )
    for field, reason in (
        ("claim_control_number", "New control number for the replacement claim"),
        ("claim_frequency_code", "Replacement of a prior claim"),
        ("original_claim_control_number", "Claim being replaced"),
    ):
        rows.append(
            {
                "path": field,
                "label": _LABELS[field],
                "line_number": None,
                "before": _display(corrected.before.get(field)),
                "after": _display(corrected.after.get(field)),
                "changed": corrected.before.get(field) != corrected.after.get(field),
                "coding_change": False,
                "reason": reason,
                "citation_refs": [],
                "system": True,
            }
        )
    return rows


def diff(corrected: CorrectedClaim) -> list[dict[str, Any]]:
    """SPEC section 9 name for `render_diff`: the change list as the review UI renders it."""
    return render_diff(corrected)


__all__ = [
    "CLAIM_PATHS",
    "LINE_PATHS",
    "base_control_number",
    "build_corrected_claim",
    "claim_to_export_dict",
    "corrected_claim_to_csv_rows",
    "diff",
    "format_money",
    "format_units",
    "line_to_export_dict",
    "next_control_number",
    "parse_path",
    "render_diff",
    "resolve_path",
    "set_path",
]
