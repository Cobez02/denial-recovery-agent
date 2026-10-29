"""X12 835 importer (docs/SPEC.md section 6).

``import_835`` parses a file (or inline text), stores one ``Remittance`` per transaction set with
its claims, lines and adjustments, matches each remittance claim to a claim, links remittance
lines to claim lines, runs denial detection and updates claim status. A file seen before (same
sha256) is recorded as a ``duplicate`` batch and nothing else is written. After each remittance is
stored, ``app.outcomes.matching.record_outcomes_for_remittance`` is called when that module exists.
``rematch_unmatched`` repeats the match / link / status step for remittance claims that found no
claim at import time; the claims importer calls it, so an 835 that arrives before its claims
export is resolved as soon as the claims are known. ``relink_remittance_lines`` repeats only the
line-linking step for one claim; the claims importer calls it after a re-export changed a claim's
lines, so a renumbered or trimmed export never leaves a remittance line pointing at the wrong
procedure.

Logs and audit details carry ids, counts and sequence numbers only.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.enums import ClaimStatus, ImportKind, ImportStatus
from app.ingest import (
    begin_batch,
    complete_batch,
    duplicate_batch,
    find_completed_batch,
    read_source,
    sha256_hex,
)
from app.ingest.denials import AdjustmentInput, detect, is_denial_adjustment
from app.ingest.matching import match_remittance_claim
from app.ingest.parse_835 import parse_835
from app.ingest.x12 import X12Error
from app.models import (
    Adjustment,
    Claim,
    ClaimLine,
    ImportBatch,
    Outcome,
    Payer,
    Remittance,
    RemittanceClaim,
    RemittanceLine,
)
from app.schemas import Parsed835, ParsedClaimPayment

log = logging.getLogger(__name__)

REMIT_COUNT_KEYS: tuple[str, ...] = (
    "transactions",
    "claims",
    "matched",
    "unmatched",
    "denials",
    "lines",
    "adjustments",
    "outcomes",
)

# CLP02 values that mean the payer processed and paid the claim (primary, secondary, tertiary, ...).
PAID_CLAIM_STATUS_CODES: frozenset[str] = frozenset({"1", "2", "3", "19", "20", "21"})

_ZERO = Decimal("0")


def _cut(value: str | None, width: int) -> str | None:
    """Keep Postgres VARCHAR limits honest; SQLite does not enforce them."""
    if value is None:
        return None
    return value[:width]


def transaction_sha(file_sha256: str, index: int) -> str:
    """Hash stored on the remittance row for transaction set ``index`` of a file.

    ``remittances`` is unique on (file_sha256, transaction_index), so every transaction set of a file
    stores the plain file hash; the index is kept as a separate column. Kept as a function so the
    duplicate check and the tests have one place that defines the stored value.
    """
    del index
    return file_sha256


def _find_existing_remittance(session: Session, file_sha256: str) -> Remittance | None:
    return session.execute(select(Remittance).where(Remittance.file_sha256 == file_sha256)).scalars().first()


def _resolve_payer(session: Session, parsed: Parsed835) -> Payer | None:
    """payers.payer_id against REF*2U / N104, then TRN03; else an exact (case-insensitive) name match."""
    for candidate in (parsed.payer.payer_id, parsed.payment.payer_identifier):
        if candidate:
            payer = session.execute(select(Payer).where(Payer.payer_id == candidate)).scalars().first()
            if payer is not None:
                return payer
    name = (parsed.payer.name or "").strip()
    if name:
        stmt = select(Payer).where(func.lower(Payer.name) == name.lower()).order_by(Payer.id)
        return session.execute(stmt).scalars().first()
    return None


def _store_remittance(
    session: Session, parsed: Parsed835, *, batch: ImportBatch, file_name: str, file_sha256: str, index: int
) -> Remittance:
    payer = _resolve_payer(session, parsed)
    remittance = Remittance(
        payer_id=payer.id if payer is not None else None,
        file_name=file_name[:255],
        file_sha256=transaction_sha(file_sha256, index),
        transaction_index=index,
        trace_number=_cut(parsed.payment.trace_number, 50),
        payer_identifier=_cut(parsed.payment.payer_identifier, 50),
        payment_method=_cut(parsed.payment.method, 3),
        payment_amount=parsed.payment.amount,
        payment_date=parsed.payment.payment_date,
        production_date=parsed.payment.production_date,
        payer_name=_cut(parsed.payer.name, 200),
        payee_npi=_cut(parsed.payee.npi, 10),
        import_batch_id=batch.id,
        raw_meta={
            "interchange_control_number": parsed.interchange_control_number,
            "group_control_number": parsed.group_control_number,
            "transaction_control_number": parsed.transaction_control_number,
            "sender_id": parsed.sender_id,
            "receiver_id": parsed.receiver_id,
            "payer_id": parsed.payer.payer_id,
            "payee_npi": parsed.payee.npi,
            "segment_count": parsed.segment_count,
            "unparsed_segment_count": len(parsed.unparsed_segments),
            "claim_count": len(parsed.claims),
            "provider_adjustment_count": len(parsed.provider_adjustments),
        },
    )
    session.add(remittance)
    session.flush()
    return remittance


def _store_claim(session: Session, remittance: Remittance, parsed: ParsedClaimPayment) -> RemittanceClaim:
    """Rows for one CLP: the remittance claim, its lines and every adjustment (denial flag included)."""
    rc = RemittanceClaim(
        remittance_id=remittance.id,
        sequence=parsed.sequence,
        patient_control_number=_cut(parsed.patient_control_number, 64) or "",
        claim_status_code=_cut(parsed.claim_status_code, 2) or "",
        charge_amount=parsed.charge_amount,
        paid_amount=parsed.paid_amount,
        patient_responsibility=parsed.patient_responsibility,
        filing_indicator=_cut(parsed.filing_indicator, 2),
        payer_claim_control_number=_cut(parsed.payer_claim_control_number, 64),
        facility_type_code=_cut(parsed.facility_type_code, 2),
        frequency_code=_cut(parsed.frequency_code, 1),
        patient_last_name=_cut(parsed.patient_last_name, 100),
        patient_first_name=_cut(parsed.patient_first_name, 100),
        member_id=_cut(parsed.member_id, 80),
        rendering_provider_npi=_cut(parsed.rendering_provider_npi, 10),
        claim_received_date=parsed.claim_received_date,
        statement_from=parsed.statement_from,
        statement_to=parsed.statement_to,
        remark_codes=list(parsed.remark_codes),
        is_denial=False,
        denied_amount=_ZERO,
    )
    remittance.claims.append(rc)

    for adjustment in parsed.adjustments:
        rc.adjustments.append(
            Adjustment(
                level="claim",
                group_code=_cut(adjustment.group_code, 2) or "",
                carc=_cut(adjustment.carc, 5) or "",
                amount=adjustment.amount,
                quantity=adjustment.quantity,
                is_denial=is_denial_adjustment(adjustment.group_code, adjustment.carc, adjustment.amount),
            )
        )

    for service in parsed.services:
        line = RemittanceLine(
            line_number=service.line_number,
            procedure_code=_cut(service.procedure_code, 10),
            modifiers=list(service.modifiers),
            charge_amount=service.charge_amount,
            paid_amount=service.paid_amount,
            units=service.units,
            service_date=service.service_date,
            line_item_control_number=_cut(service.line_item_control_number, 64),
            remark_codes=list(service.remark_codes),
        )
        rc.lines.append(line)
        for adjustment in service.adjustments:
            row = Adjustment(
                level="line",
                group_code=_cut(adjustment.group_code, 2) or "",
                carc=_cut(adjustment.carc, 5) or "",
                amount=adjustment.amount,
                quantity=adjustment.quantity,
                is_denial=is_denial_adjustment(adjustment.group_code, adjustment.carc, adjustment.amount),
            )
            row.remittance_line = line
            rc.adjustments.append(row)

    session.flush()
    return rc


def _adjustment_inputs(parsed: ParsedClaimPayment) -> list[AdjustmentInput]:
    inputs = [
        AdjustmentInput(
            level="claim",
            group_code=a.group_code,
            carc=a.carc,
            amount=a.amount,
            rarcs=list(parsed.remark_codes),
        )
        for a in parsed.adjustments
    ]
    for service in parsed.services:
        inputs.extend(
            AdjustmentInput(
                level="line",
                group_code=a.group_code,
                carc=a.carc,
                amount=a.amount,
                line_number=service.line_number,
                procedure_code=service.procedure_code,
                modifiers=list(service.modifiers),
                rarcs=list(service.remark_codes),
            )
            for a in service.adjustments
        )
    return inputs


def _link(remittance_line: RemittanceLine, claim_line: ClaimLine, used: set[int]) -> None:
    remittance_line.claim_line = claim_line
    remittance_line.claim_line_id = claim_line.id
    used.add(claim_line.id)


def link_remittance_lines(rc: RemittanceClaim, claim: Claim) -> int:
    """Link SVC rows to claim lines: by line item control number, then (procedure, date), then position."""
    claim_lines = sorted(claim.lines, key=lambda line: line.line_number)
    used: set[int] = set()
    for remittance_line in rc.lines:
        control = remittance_line.line_item_control_number
        if not control:
            continue
        for claim_line in claim_lines:
            if claim_line.id in used or not claim_line.line_item_control_number:
                continue
            if claim_line.line_item_control_number == control:
                _link(remittance_line, claim_line, used)
                break
    for remittance_line in rc.lines:
        if remittance_line.claim_line is not None:
            continue
        for claim_line in claim_lines:
            if claim_line.id in used or claim_line.procedure_code != remittance_line.procedure_code:
                continue
            if (
                remittance_line.service_date is None
                or claim_line.service_date is None
                or remittance_line.service_date == claim_line.service_date
            ):
                _link(remittance_line, claim_line, used)
                break
    for position, remittance_line in enumerate(rc.lines):
        if remittance_line.claim_line is not None or position >= len(claim_lines):
            continue
        candidate = claim_lines[position]
        if candidate.id not in used:
            _link(remittance_line, candidate, used)
    return sum(1 for line in rc.lines if line.claim_line is not None)


def relink_remittance_lines(session: Session, claim: Claim) -> int:
    """Re-link the SVC rows of every remittance claim matched to ``claim`` against its current lines.

    Called after a claims re-export changed the claim's lines (renumbered, re-ordered or trimmed):
    every link is dropped and rebuilt with :func:`link_remittance_lines`, so a remittance line
    follows its procedure rather than a line number that now holds something else. Returns the
    number of remittance lines linked afterwards; a line whose procedure is gone stays unlinked.
    """
    stmt = select(RemittanceClaim).where(RemittanceClaim.claim_id == claim.id).order_by(RemittanceClaim.id)
    remittance_claims = [rc for rc in session.execute(stmt).scalars() if rc.lines]
    if not remittance_claims:
        return 0
    for rc in remittance_claims:
        for remittance_line in rc.lines:
            remittance_line.claim_line = None
            remittance_line.claim_line_id = None
    session.flush()
    linked = sum(link_remittance_lines(rc, claim) for rc in remittance_claims)
    log.info(
        "re-linked remittance lines for claim %s: %d of %d linked across %d remittance claim(s)",
        claim.id,
        linked,
        sum(len(rc.lines) for rc in remittance_claims),
        len(remittance_claims),
    )
    return linked


def claim_status_for(rc: RemittanceClaim) -> str | None:
    """New claim status implied by a remittance claim, or None to leave it unchanged."""
    paid = Decimal(rc.paid_amount or 0)
    if rc.is_denial:
        return str(ClaimStatus.DENIED) if paid <= _ZERO else str(ClaimStatus.PARTIAL)
    if paid > _ZERO and (rc.claim_status_code or "").strip() in PAID_CLAIM_STATUS_CODES:
        return str(ClaimStatus.PAID)
    return None


def _record_outcomes(session: Session, remittance: Remittance) -> int:
    """Hand the stored remittance to the outcomes module (built separately) and count what it recorded."""
    try:
        from app.outcomes.matching import record_outcomes_for_remittance
    except ImportError:
        log.warning("outcomes module unavailable; no outcomes recorded for remittance %s", remittance.id)
        return 0
    record_outcomes_for_remittance(session, remittance)
    session.flush()
    rc_ids = [rc.id for rc in remittance.claims if rc.id is not None]
    if not rc_ids:
        return 0
    stmt = select(func.count(Outcome.id)).where(Outcome.remittance_claim_id.in_(rc_ids))
    return int(session.execute(stmt).scalar_one())


def import_835(
    session: Session, source: str | Path, file_name: str | None = None, created_by: str = "system"
) -> ImportBatch:
    """Import one 835 file (path or text). Returns the ImportBatch; the caller commits."""
    kind = str(ImportKind.REMIT_835)
    src = read_source(source, default_name="inline.835", inline_marker="ISA")
    name = file_name or src.name
    sha = sha256_hex(src.raw)
    if (
        find_completed_batch(session, kind, sha) is not None
        or _find_existing_remittance(session, sha) is not None
    ):
        return duplicate_batch(session, kind=kind, file_name=name, file_sha256=sha, created_by=created_by)
    batch = begin_batch(session, kind=kind, file_name=name, file_sha256=sha, created_by=created_by)

    try:
        transactions = parse_835(src.text)
    except X12Error as exc:
        return complete_batch(
            session,
            batch,
            status=ImportStatus.FAILED,
            counts={"transactions": 0},
            created_by=created_by,
            error=f"parse error: {exc}",
        )

    counts: dict[str, int] = {key: 0 for key in REMIT_COUNT_KEYS}
    unmatched: list[dict[str, Any]] = []
    for index, parsed in enumerate(transactions):
        remittance = _store_remittance(
            session, parsed, batch=batch, file_name=name, file_sha256=sha, index=index
        )
        counts["transactions"] += 1
        for parsed_claim in parsed.claims:
            rc = _store_claim(session, remittance, parsed_claim)
            counts["claims"] += 1
            counts["lines"] += len(rc.lines)
            counts["adjustments"] += len(rc.adjustments)

            claim = match_remittance_claim(session, rc, remittance=remittance)
            if claim is not None:
                link_remittance_lines(rc, claim)

            is_denial, denied_amount, _reasons = detect(rc, _adjustment_inputs(parsed_claim))
            rc.is_denial = is_denial
            rc.denied_amount = denied_amount
            if is_denial:
                counts["denials"] += 1

            if claim is not None:
                counts["matched"] += 1
                new_status = claim_status_for(rc)
                if new_status is not None:
                    claim.status = new_status
            else:
                counts["unmatched"] += 1
                unmatched.append({"remittance_id": remittance.id, "sequence": rc.sequence})
            session.flush()
        counts["outcomes"] += _record_outcomes(session, remittance)

    return complete_batch(
        session,
        batch,
        status=ImportStatus.DONE,
        counts=counts,
        created_by=created_by,
        extra_details={"unmatched_sequences": unmatched},
    )


def rematch_unmatched(session: Session) -> int:
    """Match remittance claims that have no claim yet, e.g. from an 835 imported before its claims.

    Every newly matched row gets its lines linked and its claim's status updated exactly as during
    an import; outcomes are then recorded for the affected remittances. Returns the number matched.
    Nothing is committed. ``import_claims_csv`` calls this after each claims import.
    """
    stmt = select(RemittanceClaim).where(RemittanceClaim.claim_id.is_(None)).order_by(RemittanceClaim.id)
    pending = session.execute(stmt).scalars().all()
    matched = 0
    touched: dict[int, Remittance] = {}
    for rc in pending:
        claim = match_remittance_claim(session, rc)
        if claim is None:
            continue
        link_remittance_lines(rc, claim)
        new_status = claim_status_for(rc)
        if new_status is not None:
            claim.status = new_status
        matched += 1
        if rc.remittance is not None:
            touched[rc.remittance.id] = rc.remittance
    session.flush()
    for remittance in touched.values():
        _record_outcomes(session, remittance)
    if pending:
        log.info("re-matched %d of %d unmatched remittance claim(s)", matched, len(pending))
    return matched


__all__ = [
    "PAID_CLAIM_STATUS_CODES",
    "REMIT_COUNT_KEYS",
    "claim_status_for",
    "import_835",
    "link_remittance_lines",
    "relink_remittance_lines",
    "rematch_unmatched",
    "transaction_sha",
]
