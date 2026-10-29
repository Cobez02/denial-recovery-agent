"""Append-only, hash-chained audit log (milestone M8).

Every action by every person and agent is recorded here: who, what, when, which claim and why.
Rows can only be inserted. ORM listeners refuse UPDATE/DELETE on every dialect; the Postgres
migration adds a trigger as a second guard. `verify_chain` detects tampering after the fact.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import event, select, text
from sqlalchemy.orm import Session

from app.enums import ActorType
from app.models import AuditLog


class AuditImmutableError(RuntimeError):
    """Raised when code tries to update or delete an audit row."""


# Transaction-scoped advisory lock key: a web process and an rq worker appending at the same time
# would otherwise both chain on the same prev_hash and fork the chain. SQLite serialises writers.
AUDIT_CHAIN_LOCK_KEY = 815001


def _json_default(o: Any) -> Any:
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, datetime):
        return o.isoformat()
    if hasattr(o, "isoformat"):
        return o.isoformat()
    if hasattr(o, "model_dump"):
        return o.model_dump(mode="json")
    return str(o)


def canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=_json_default, ensure_ascii=False)


def compute_hash(prev_hash: str | None, row: dict[str, Any]) -> str:
    payload = (prev_hash or "") + canonical_json(row)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _row_dict(entry: AuditLog) -> dict[str, Any]:
    ts = entry.ts
    if ts is not None and ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return {
        "ts": ts.isoformat() if ts else None,
        "actor_type": entry.actor_type,
        "actor_id": entry.actor_id,
        "action": entry.action,
        "entity_type": entry.entity_type,
        "entity_id": entry.entity_id,
        "claim_id": entry.claim_id,
        "reason": entry.reason,
        "details": entry.details or {},
    }


def _last_hash(session: Session) -> str | None:
    stmt = select(AuditLog.hash).order_by(AuditLog.id.desc()).limit(1)
    return session.execute(stmt).scalar_one_or_none()


def record(
    session: Session,
    *,
    actor_type: ActorType | str,
    actor_id: str,
    action: str,
    entity_type: str | None = None,
    entity_id: int | None = None,
    claim_id: int | None = None,
    reason: str | None = None,
    details: dict[str, Any] | None = None,
) -> AuditLog:
    """Append one audit event. Details must be identifiers and counts, never free-text PHI."""
    session.flush()  # make sure earlier rows in this transaction are visible to _last_hash
    bind = session.get_bind()
    if bind is not None and bind.dialect.name == "postgresql":
        session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": AUDIT_CHAIN_LOCK_KEY})
    entry = AuditLog(
        ts=datetime.now(UTC),
        actor_type=str(actor_type),
        actor_id=actor_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        claim_id=claim_id,
        reason=reason,
        details=json.loads(canonical_json(details or {})),
        prev_hash=_last_hash(session),
        hash="",
    )
    entry.hash = compute_hash(entry.prev_hash, _row_dict(entry))
    session.add(entry)
    session.flush()
    return entry


def verify_chain(session: Session) -> int | None:
    """Return the id of the first broken row, or None when the whole chain verifies."""
    prev: str | None = None
    for entry in session.execute(select(AuditLog).order_by(AuditLog.id)).scalars():
        if entry.prev_hash != prev:
            return entry.id
        if compute_hash(entry.prev_hash, _row_dict(entry)) != entry.hash:
            return entry.id
        prev = entry.hash
    return None


@event.listens_for(AuditLog, "before_update")
def _forbid_update(mapper, connection, target) -> None:  # pragma: no cover - guard
    raise AuditImmutableError("audit_log rows are append-only (update refused)")


@event.listens_for(AuditLog, "before_delete")
def _forbid_delete(mapper, connection, target) -> None:  # pragma: no cover - guard
    raise AuditImmutableError("audit_log rows are append-only (delete refused)")


__all__ = ["record", "verify_chain", "compute_hash", "canonical_json", "AuditImmutableError"]
