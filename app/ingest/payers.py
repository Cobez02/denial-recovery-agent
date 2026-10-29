"""Payer windows (docs/SPEC.md section 2): timely filing, corrected claim and appeal days per payer.

The claims importer creates a payer with the settings defaults the first time it sees a payer id
and never changes the windows afterwards, so an operator's values survive every later import. This
module is the one place that sets them: ``set_payer_windows`` (audited, ids and day counts only) and
``list_payers`` for the ``denial-agent set-payer`` / ``list-payers`` commands and any future importer
of a payers file. Deadlines and the timely-filing verdict read the stored windows at proposal time.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.enums import ActorType
from app.models import Payer

WINDOW_FIELDS: tuple[str, ...] = ("timely_filing_days", "corrected_claim_days", "appeal_days")
MIN_WINDOW_DAYS = 1
MAX_WINDOW_DAYS = 3650


class UnknownPayerError(LookupError):
    """No payer row carries the given payer id (the claims import creates payer rows)."""


def get_payer(session: Session, payer_id: str) -> Payer | None:
    """The payer row for an X12 payer identifier, or None."""
    key = (payer_id or "").strip()
    if not key:
        return None
    return session.execute(select(Payer).where(Payer.payer_id == key)).scalars().first()


def list_payers(session: Session) -> list[dict[str, Any]]:
    """Every payer with its windows, ordered by payer id (plain dicts: ids, names and day counts)."""
    rows = session.execute(select(Payer).order_by(Payer.payer_id)).scalars()
    return [
        {
            "id": payer.id,
            "payer_id": payer.payer_id,
            "name": payer.name,
            "timely_filing_days": payer.timely_filing_days,
            "corrected_claim_days": payer.corrected_claim_days,
            "appeal_days": payer.appeal_days,
        }
        for payer in rows
    ]


def _validated_days(field: str, value: int | None) -> int | None:
    if value is None:
        return None
    try:
        days = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be a whole number of days") from exc
    if not MIN_WINDOW_DAYS <= days <= MAX_WINDOW_DAYS:
        raise ValueError(f"{field} must be between {MIN_WINDOW_DAYS} and {MAX_WINDOW_DAYS} days")
    return days


def set_payer_windows(
    session: Session,
    payer_id: str,
    *,
    timely_filing_days: int | None = None,
    corrected_claim_days: int | None = None,
    appeal_days: int | None = None,
    actor_id: str = "system",
    actor_type: ActorType | str = ActorType.USER,
) -> Payer:
    """Set one or more windows on an existing payer; a window left ``None`` is unchanged.

    Records a ``payer_windows_set`` audit event with the payer id and the before/after day counts.
    Raises ``UnknownPayerError`` for an unknown payer id and ``ValueError`` for a window outside
    1..3650 days or when no window was given. Nothing is committed; the caller commits.
    """
    payer = get_payer(session, payer_id)
    if payer is None:
        raise UnknownPayerError(f"unknown payer id: {(payer_id or '').strip()}")
    requested = {
        "timely_filing_days": _validated_days("timely_filing_days", timely_filing_days),
        "corrected_claim_days": _validated_days("corrected_claim_days", corrected_claim_days),
        "appeal_days": _validated_days("appeal_days", appeal_days),
    }
    changes = {field: days for field, days in requested.items() if days is not None}
    if not changes:
        raise ValueError("no window given: set at least one of " + ", ".join(WINDOW_FIELDS))
    before = {field: getattr(payer, field) for field in changes}
    for field, days in changes.items():
        setattr(payer, field, days)
    session.flush()
    audit.record(
        session,
        actor_type=actor_type,
        actor_id=actor_id,
        action="payer_windows_set",
        entity_type="payer",
        entity_id=payer.id,
        details={"payer_id": payer.payer_id, "before": before, "after": changes},
    )
    return payer


__all__ = [
    "MAX_WINDOW_DAYS",
    "MIN_WINDOW_DAYS",
    "WINDOW_FIELDS",
    "UnknownPayerError",
    "get_payer",
    "list_payers",
    "set_payer_windows",
]
