"""Follow-up 835 remittances for the full-loop test (docs/SPEC.md section 12).

After the review queue marks fixes as submitted, the e2e test needs a later remittance in which the
payer pays, partially pays, denies again or reverses each resubmitted claim. `generate_followup_files`
builds one 835 per payer from the work items and an outcome plan; `generate_followup_835` wraps the
same transactions into a single interchange when one file is more convenient.

The amounts are chosen so that `app.outcomes.matching` records exactly the planned result under the
SPEC section 10 rule (paid when CLP04 >= 95% of the work item's dollar value, partial when
0 < CLP04 < that, denied_again when nothing is paid, reversed for CLP02 22):

    paid          every line paid; a CO-45 contractual write-off of up to 20% of the charge is
                  taken, but the total paid never drops below the work item's dollar value
    partial       half of the dollar value paid (half the charge when the work item has none),
                  spread over the lines in proportion to their charges, CO-45 for the rest
    denied_again  CLP02 4, nothing paid, the work item's first stored reason as the CAS
    reversed      CLP02 22 with negative charge, paid and CO-45 (a prior payment taken back)

Only `app.models` and `synthetic.x12_writer` are imported, so this module works as soon as the
foundation exists. Nothing is logged but counts.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from datetime import date, timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.models import Claim, Clinic, Payer, WorkItem
from synthetic.x12_writer import build_835, build_835_multi

log = logging.getLogger(__name__)

PLAN_PAID = "paid"
PLAN_PARTIAL = "partial"
PLAN_DENIED_AGAIN = "denied_again"
PLAN_REVERSED = "reversed"
PLAN_VALUES: tuple[str, ...] = (PLAN_PAID, PLAN_PARTIAL, PLAN_DENIED_AGAIN, PLAN_REVERSED)

CONTRACTUAL_RATE = Decimal("0.20")  # 'paid': up to 20% CO-45 contractual, the rest paid
PARTIAL_RATE = Decimal("0.50")  # 'partial': half the dollar value paid, CO-45 for the rest
RECEIVED_LAG_DAYS = 14
CENT = Decimal("0.01")
ZERO = Decimal("0.00")
DEFAULT_REASON: dict[str, Any] = {"level": "claim", "group_code": "CO", "carc": "16", "rarcs": []}
FOLLOWUP_SENDER_ID = "FOLLOWUP835"


def _dec(value: Any) -> Decimal:
    return Decimal(str(value if value is not None else 0)).quantize(CENT)


def _share(amount: Decimal, part: Decimal, whole: Decimal) -> Decimal:
    """`amount * part / whole` rounded DOWN to the cent, so line shares never add up to more than `amount`."""
    if whole <= 0:
        return ZERO
    return (amount * part / whole).quantize(CENT, rounding=ROUND_DOWN)


def _split_provider_name(name: str | None) -> tuple[str, str]:
    """'Priya Thornquist, MD' -> ('Thornquist', 'Priya'); unknown shapes degrade gracefully."""
    if not name:
        return "", ""
    core = name.split(",")[0].strip()
    parts = core.split()
    if len(parts) >= 2:
        return " ".join(parts[1:]), parts[0]
    return core, ""


def _payer_dict(payer: Payer) -> dict[str, Any]:
    address = payer.address if isinstance(payer.address, dict) else {}
    return {
        "name": payer.name,
        "payer_id": payer.payer_id,
        "tax_id": str(address.get("tax_id") or f"{payer.id:09d}"),
        "address": {k: address[k] for k in ("line1", "city", "state", "zip") if address.get(k)},
        "contact_name": "PROVIDER SERVICES",
        "phone": address.get("phone"),
    }


def _payee_dict(clinic: Clinic) -> dict[str, Any]:
    return {"name": clinic.name, "npi": clinic.npi or "", "tax_id": clinic.tax_id or ""}


def _line_dicts(claim: Claim) -> list[dict[str, Any]]:
    lines = sorted(claim.lines, key=lambda line: line.line_number)
    return [
        {
            "procedure": line.procedure_code,
            "modifiers": list(line.modifiers or []),
            "charge": _dec(line.charge_amount),
            "paid": ZERO,
            "units": _dec(line.units or 1),
            "service_date": line.service_date or claim.service_date_from,
            "line_item_control_number": line.line_item_control_number,
            "allowed": None,
            "adjustments": [],
            "rarcs": [],
        }
        for line in lines
    ]


def _primary_reason(work_item: WorkItem) -> dict[str, Any]:
    reasons = [r for r in (work_item.all_reasons or []) if isinstance(r, dict)]
    return reasons[0] if reasons else dict(DEFAULT_REASON)


def _pay_lines(
    lines: list[dict[str, Any]], charge_total: Decimal, paid_total: Decimal, *, never_below: bool
) -> None:
    """Pay `paid_total` across the lines in proportion to their charges; the rest of each charge is CO-45.

    Cent rounding goes in the payee's favour when `never_below` (the lines add up to at least
    `paid_total`) and against it otherwise (at most `paid_total`).
    """
    for line in lines:
        if never_below:
            contractual = _share(charge_total - paid_total, line["charge"], charge_total)
            paid = (line["charge"] - contractual).quantize(CENT)
        else:
            paid = _share(paid_total, line["charge"], charge_total)
            contractual = (line["charge"] - paid).quantize(CENT)
        line["allowed"], line["paid"] = paid, paid
        line["adjustments"] = [("CO", "45", contractual)] if contractual > 0 else []


def build_followup_claim(work_item: WorkItem, claim: Claim, plan: str, payment_date: date) -> dict[str, Any]:
    """The x12_writer claim dict for one work item under one outcome plan."""
    if plan not in PLAN_VALUES:
        raise ValueError(f"unknown outcome plan for work item {work_item.id}")
    patient = claim.patient
    lines = _line_dicts(claim)
    charge_total = sum((line["charge"] for line in lines), Decimal("0")).quantize(CENT)
    value = _dec(work_item.dollar_value)
    rendering_last, rendering_first = _split_provider_name(claim.rendering_provider_name)
    out: dict[str, Any] = {
        "patient_control_number": work_item.submitted_claim_control_number or claim.claim_control_number,
        "status_code": "1",
        "charge": charge_total,
        "paid": ZERO,
        "patient_responsibility": ZERO,
        "filing_indicator": "12",
        "payer_claim_control_number": f"FU{claim.payer.payer_id}{payment_date:%y%m%d}{work_item.id:06d}",
        "facility_type_code": claim.place_of_service or "11",
        "frequency_code": claim.frequency_code or "1",
        "patient_last_name": patient.last_name if patient else "",
        "patient_first_name": patient.first_name if patient else "",
        "member_id": patient.member_id if patient else None,
        "rendering_provider": {
            "last_name": rendering_last,
            "first_name": rendering_first,
            "npi": claim.rendering_provider_npi,
        },
        "received_date": payment_date - timedelta(days=RECEIVED_LAG_DAYS),
        "statement_from": claim.service_date_from or (lines[0]["service_date"] if lines else None),
        "statement_to": claim.service_date_to
        or claim.service_date_from
        or (lines[-1]["service_date"] if lines else None),
        "adjustments": [],
        "rarcs": [],
        "lines": lines,
    }

    if plan == PLAN_PAID:
        # Up to 20% contractual, but the total paid never drops below the dollar value, so the
        # SPEC 10 rule (paid >= 95% of the dollar value) records `paid` whatever share was denied.
        budget = min(
            _share(charge_total, CONTRACTUAL_RATE, Decimal(1)), charge_total - min(value, charge_total)
        )
        _pay_lines(lines, charge_total, charge_total - max(budget, ZERO), never_below=True)
    elif plan == PLAN_PARTIAL:
        # Something, but less than 95% of the dollar value: half of it (half the charge when the
        # work item carries no usable dollar value), so the rule records `partial`.
        basis = value if 0 < value <= charge_total else charge_total
        _pay_lines(lines, charge_total, _share(basis, PARTIAL_RATE, Decimal(1)), never_below=False)
    elif plan == PLAN_DENIED_AGAIN:
        reason = _primary_reason(work_item)
        group = str(reason.get("group_code") or "CO")
        carc = str(reason.get("carc") or "16")
        rarcs = [str(r) for r in (reason.get("rarcs") or [])]
        out["status_code"] = "4"
        if str(reason.get("level", "claim")) == "line" and lines:
            referenced = reason.get("line_number")
            for index, line in enumerate(lines, start=1):
                line["adjustments"] = [(group, carc, line["charge"])]
                if referenced in (None, index):
                    line["rarcs"] = list(rarcs)
        else:
            out["adjustments"] = [(group, carc, charge_total)]
            out["rarcs"] = list(rarcs)
    else:  # PLAN_REVERSED: take back a prior payment, negative amounts throughout
        out["status_code"] = "22"
        for line in lines:
            prior_paid = (line["charge"] * (Decimal(1) - CONTRACTUAL_RATE)).quantize(CENT)
            contractual = (line["charge"] - prior_paid).quantize(CENT)
            line["charge"] = -line["charge"]
            line["paid"] = -prior_paid
            line["adjustments"] = [("CO", "45", -contractual)] if contractual else []
        out["charge"] = -charge_total

    out["paid"] = sum((line["paid"] for line in lines), Decimal("0")).quantize(CENT)
    return out


def _collect(
    session: Session,
    work_items: Iterable[WorkItem],
    outcome_plan: Mapping[int, str],
    payment_date: date,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, dict[str, Any]], dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    payers: dict[str, dict[str, Any]] = {}
    payee: dict[str, Any] = {}
    for work_item in work_items:
        plan = str(outcome_plan.get(work_item.id, PLAN_PAID))
        claim = work_item.claim if work_item.claim is not None else session.get(Claim, work_item.claim_id)
        if claim is None:
            raise ValueError(f"work item {work_item.id} has no claim")
        payer = claim.payer
        groups.setdefault(payer.payer_id, []).append(
            build_followup_claim(work_item, claim, plan, payment_date)
        )
        if payer.payer_id not in payers:
            payers[payer.payer_id] = _payer_dict(payer)
        if not payee and claim.clinic is not None:
            payee = _payee_dict(claim.clinic)
    return groups, payers, payee or {"name": "PAYEE", "npi": "", "tax_id": ""}


def _control_number(payment_date: date, index: int) -> int:
    return (payment_date.toordinal() % 1_000_000) * 100 + index


def generate_followup_files(
    session: Session,
    work_items: Iterable[WorkItem],
    outcome_plan: Mapping[int, str],
    payment_date: date,
    *,
    as_of: date | None = None,
) -> dict[str, str]:
    """One follow-up 835 per payer: {payer_id: 835 text}.

    `outcome_plan` maps work item id -> 'paid' | 'partial' | 'denied_again' | 'reversed'; a work
    item missing from the plan is paid. CLP01 is the work item's submitted_claim_control_number
    when set, else the claim's control number, so the outcome matcher can find it either way.
    """
    header_date = as_of or payment_date
    groups, payers, payee = _collect(session, work_items, outcome_plan, payment_date)
    out: dict[str, str] = {}
    for index, (payer_id, claims) in enumerate(groups.items(), start=1):
        out[payer_id] = build_835(
            payer=payers[payer_id],
            payee=payee,
            claims=claims,
            payment_date=payment_date,
            production_date=header_date,
            trace_number=f"FU{payer_id}{payment_date:%Y%m%d}",
            interchange_control_number=_control_number(payment_date, index),
            sender_id=payer_id,
            receiver_id=str(payee.get("npi") or "PAYEE"),
        )
    log.info(
        "follow-up remittances built: payers=%d claims=%d", len(out), sum(len(c) for c in groups.values())
    )
    return out


def generate_followup_835(
    session: Session,
    work_items: Iterable[WorkItem],
    outcome_plan: Mapping[int, str],
    payment_date: date | None = None,
    *,
    as_of: date | None = None,
) -> str:
    """All payers in one file: one interchange, one ST/SE transaction per payer (SPEC section 12 name).

    When payment_date is omitted it is derived from the data (latest original submission date among
    the work items' claims plus 45 days) so the result stays deterministic.
    """
    items = list(work_items)
    if payment_date is None:
        dates = [
            d
            for wi in items
            for d in (
                (wi.claim.original_submission_date if wi.claim is not None else None),
                (wi.claim.service_date_from if wi.claim is not None else None),
            )
            if d is not None
        ]
        payment_date = (max(dates) if dates else date(2026, 1, 1)) + timedelta(days=45)
    header_date = as_of or payment_date
    groups, payers, payee = _collect(session, items, outcome_plan, payment_date)
    transactions = [
        {
            "payer": payers[payer_id],
            "payee": payee,
            "claims": claims,
            "payment_date": payment_date,
            "production_date": header_date,
            "trace_number": f"FU{payer_id}{payment_date:%Y%m%d}",
        }
        for payer_id, claims in groups.items()
    ]
    return build_835_multi(
        transactions=transactions,
        interchange_control_number=_control_number(payment_date, 0),
        sender_id=FOLLOWUP_SENDER_ID,
        receiver_id=str(payee.get("npi") or "PAYEE"),
        interchange_date=header_date,
    )


__all__ = [
    "PLAN_DENIED_AGAIN",
    "PLAN_PAID",
    "PLAN_PARTIAL",
    "PLAN_REVERSED",
    "PLAN_VALUES",
    "build_followup_claim",
    "generate_followup_835",
    "generate_followup_files",
]
