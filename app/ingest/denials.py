"""Denial detection (docs/SPEC.md section 6).

A remittance claim is a denial when CLP02 is ``4`` (denied) or when any CAS adjustment in group
CO/PI/OA/PR carries a CARC that is not purely informational and a positive amount. Informational
CARCs (contractual write-offs, patient responsibility splits, interest ...) never make a denial on
their own. Reasons are returned largest amount first; on ties claim-level reasons come before
line-level ones, then lower line numbers first.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, Protocol

from app.models import Adjustment, RemittanceClaim
from app.schemas import DenialReason

INFORMATIONAL_CARCS: frozenset[str] = frozenset(
    {"1", "2", "3", "45", "66", "94", "104", "118", "137", "144", "187", "237", "253"}
)
DENIAL_GROUPS: frozenset[str] = frozenset({"CO", "PI", "OA", "PR"})
DENIED_CLAIM_STATUS = "4"

_ZERO = Decimal("0")


@dataclass
class AdjustmentInput:
    """One CAS triplet with the context the reason needs (line facts and remark codes)."""

    level: Literal["claim", "line"]
    group_code: str
    carc: str
    amount: Decimal
    line_number: int | None = None
    procedure_code: str | None = None
    modifiers: list[str] = field(default_factory=list)
    rarcs: list[str] = field(default_factory=list)


def is_denial_adjustment(
    group_code: str | None, carc: str | None, amount: Decimal | int | float | None
) -> bool:
    """True for a CO/PI/OA/PR adjustment with a non-informational CARC and a positive amount."""
    if not group_code or not carc:
        return False
    if str(group_code).strip().upper() not in DENIAL_GROUPS:
        return False
    if str(carc).strip() in INFORMATIONAL_CARCS:
        return False
    return Decimal(amount or 0) > _ZERO


def _sort_key(level: str, amount: Decimal, line_number: int | None) -> tuple[Decimal, int, int]:
    return (-Decimal(amount), 0 if level == "claim" else 1, line_number or 0)


def _reason(adjustment: AdjustmentInput) -> DenialReason:
    return DenialReason(
        level=adjustment.level,
        group_code=str(adjustment.group_code).strip().upper(),
        carc=str(adjustment.carc).strip(),
        rarcs=list(adjustment.rarcs),
        amount=Decimal(adjustment.amount),
        line_number=adjustment.line_number,
        procedure_code=adjustment.procedure_code,
        modifiers=list(adjustment.modifiers),
    )


class ClaimFacts(Protocol):
    """What :func:`detect` reads from a remittance claim: CLP02 and CLP03.

    Both a stored ``RemittanceClaim`` and a parsed ``schemas.ParsedClaimPayment`` satisfy it.
    """

    claim_status_code: str
    charge_amount: Decimal


def _input_from_row(adjustment: Adjustment, claim_rarcs: list[str]) -> AdjustmentInput:
    """An ``AdjustmentInput`` for a stored ``Adjustment`` row (line facts from its remittance line)."""
    line = adjustment.remittance_line
    level: Literal["claim", "line"] = "line" if (adjustment.level == "line" or line is not None) else "claim"
    return AdjustmentInput(
        level=level,
        group_code=adjustment.group_code or "",
        carc=adjustment.carc or "",
        amount=Decimal(adjustment.amount or 0),
        line_number=line.line_number if line is not None else None,
        procedure_code=line.procedure_code if line is not None else None,
        modifiers=list(line.modifiers or []) if line is not None else [],
        rarcs=list(line.remark_codes or []) if line is not None else list(claim_rarcs),
    )


def _coerce(item: Any, claim_rarcs: list[str]) -> AdjustmentInput:
    """Accept an ``AdjustmentInput``, a stored ``Adjustment`` row, or any other object carrying
    ``group_code``/``carc``/``amount`` (such as ``schemas.ParsedAdjustment``), taken as claim level."""
    if isinstance(item, AdjustmentInput):
        return item
    if isinstance(item, Adjustment):
        return _input_from_row(item, claim_rarcs)
    return AdjustmentInput(
        level="claim",
        group_code=str(getattr(item, "group_code", "") or ""),
        carc=str(getattr(item, "carc", "") or ""),
        amount=Decimal(getattr(item, "amount", 0) or 0),
        rarcs=list(claim_rarcs),
    )


def detect(rc: ClaimFacts, adjustments: Iterable[Any]) -> tuple[bool, Decimal, list[DenialReason]]:
    """Return ``(is_denial, denied_amount, reasons)`` for one remittance claim (docs/SPEC.md section 6).

    ``rc`` supplies CLP02 (``claim_status_code``) and CLP03 (``charge_amount``): a stored
    ``RemittanceClaim`` or a ``ParsedClaimPayment``. ``adjustments`` are ``AdjustmentInput`` rows
    (what the importer builds from a parsed CLP), stored ``Adjustment`` rows
    (``detect(rc, rc.adjustments)``) or claim-level parsed adjustments. ``denied_amount`` is the sum
    of the denial adjustments; a claim denied outright (CLP02 ``4``) that carries no denial
    adjustment at all is denied for its full charge.
    """
    claim_rarcs = [str(code) for code in (getattr(rc, "remark_codes", None) or [])]
    inputs = [_coerce(item, claim_rarcs) for item in adjustments]
    denial_adjustments = [a for a in inputs if is_denial_adjustment(a.group_code, a.carc, a.amount)]
    status = (rc.claim_status_code or "").strip()
    is_denial = status == DENIED_CLAIM_STATUS or bool(denial_adjustments)
    denied_amount = sum((Decimal(a.amount) for a in denial_adjustments), _ZERO)
    if is_denial and not denial_adjustments:
        denied_amount = Decimal(rc.charge_amount or 0)
    ordered = sorted(denial_adjustments, key=lambda a: _sort_key(a.level, a.amount, a.line_number))
    return is_denial, denied_amount, [_reason(a) for a in ordered]


def adjustment_inputs_for(rc: RemittanceClaim, *, denials_only: bool = False) -> list[AdjustmentInput]:
    """Rebuild ``AdjustmentInput`` rows from a stored remittance claim.

    An adjustment counts as a denial when the import flagged it or when the rule says so (rows
    built by hand without the flag still work).
    """
    claim_rarcs = [str(code) for code in (rc.remark_codes or [])]
    inputs: list[AdjustmentInput] = []
    for adjustment in rc.adjustments:
        flagged = bool(adjustment.is_denial) or is_denial_adjustment(
            adjustment.group_code, adjustment.carc, adjustment.amount
        )
        if denials_only and not flagged:
            continue
        inputs.append(_input_from_row(adjustment, claim_rarcs))
    return inputs


def detect_for(rc: RemittanceClaim) -> tuple[bool, Decimal, list[DenialReason]]:
    """Run :func:`detect` on a stored remittance claim (its adjustments and lines must be loaded)."""
    return detect(rc, rc.adjustments)


def denial_reasons_for(rc: RemittanceClaim) -> list[DenialReason]:
    """Reasons for a stored remittance claim, ordered like :func:`detect`.

    Line-level reasons carry the line's procedure, modifiers and remark codes; claim-level reasons
    carry the claim's remark codes. ``carc_text``/``rarc_texts`` are left empty: the pipeline fills
    them from the code catalog.
    """
    inputs = adjustment_inputs_for(rc, denials_only=True)
    ordered = sorted(inputs, key=lambda a: _sort_key(a.level, a.amount, a.line_number))
    return [_reason(a) for a in ordered]


__all__ = [
    "DENIAL_GROUPS",
    "DENIED_CLAIM_STATUS",
    "INFORMATIONAL_CARCS",
    "AdjustmentInput",
    "adjustment_inputs_for",
    "denial_reasons_for",
    "detect",
    "detect_for",
    "is_denial_adjustment",
]
