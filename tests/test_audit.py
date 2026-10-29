"""Audit log (milestone M8): append-only, hash-chained; tampering is detected."""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text

from app import audit
from app.audit.log import AuditImmutableError, _row_dict, canonical_json, compute_hash
from app.models import AuditLog


def _record(session, n: int = 3) -> list[AuditLog]:
    rows = [
        audit.record(
            session,
            actor_type="system",
            actor_id="tests",
            action=f"step_{i}",
            entity_type="work_item",
            entity_id=i + 1,
            claim_id=100 + i,
            details={"index": i, "count": 2},
        )
        for i in range(n)
    ]
    session.commit()
    return rows


def test_record_and_verify_chain_ok(session):
    rows = _record(session)
    assert rows[0].prev_hash is None
    assert rows[1].prev_hash == rows[0].hash
    assert rows[2].prev_hash == rows[1].hash
    assert all(len(r.hash) == 64 for r in rows)
    assert len({r.hash for r in rows}) == 3
    assert audit.verify_chain(session) is None


def test_recorded_hash_matches_stored_row(session):
    row = _record(session, 1)[0]
    session.expire_all()
    stored = session.get(AuditLog, row.id)
    assert compute_hash(stored.prev_hash, _row_dict(stored)) == stored.hash


def test_orm_update_raises(session):
    rows = _record(session, 1)
    rows[0].action = "tampered"
    with pytest.raises(AuditImmutableError):
        session.flush()
    session.rollback()
    session.expire_all()
    assert session.get(AuditLog, rows[0].id).action == "step_0"
    assert audit.verify_chain(session) is None


def test_orm_delete_raises(session):
    rows = _record(session, 2)
    session.delete(rows[0])
    with pytest.raises(AuditImmutableError):
        session.flush()
    session.rollback()
    session.expire_all()
    assert session.execute(select(func.count(AuditLog.id))).scalar_one() == 2
    assert audit.verify_chain(session) is None


def test_raw_sql_update_detected(session):
    rows = _record(session, 3)
    target = rows[1].id
    session.execute(
        text("UPDATE audit_log SET details = :details WHERE id = :id"),
        {"details": json.dumps({"index": 99, "count": 2}), "id": target},
    )
    session.commit()
    session.expire_all()
    assert audit.verify_chain(session) == target


def test_raw_sql_delete_detected(session):
    rows = _record(session, 3)
    session.execute(text("DELETE FROM audit_log WHERE id = :id"), {"id": rows[1].id})
    session.commit()
    session.expire_all()
    # The row after the gap carries a prev_hash that no longer matches its predecessor.
    assert audit.verify_chain(session) == rows[2].id


def test_canonical_json_handles_decimal_and_date():
    payload = {"amount": Decimal("12.50"), "date": date(2026, 10, 28), "n": 1, "nested": {"z": Decimal("0")}}
    encoded = canonical_json(payload)
    assert json.loads(encoded) == {"amount": "12.50", "date": "2026-10-28", "n": 1, "nested": {"z": "0"}}
    assert encoded == '{"amount":"12.50","date":"2026-10-28","n":1,"nested":{"z":"0"}}'


def test_details_with_decimal_and_date_serialize(session):
    # The JSON-safe form every module passes to record(): money as str, dates as ISO strings.
    details = json.loads(canonical_json({"paid_amount": Decimal("95.00"), "payment_date": date(2026, 11, 1)}))
    row = audit.record(
        session, actor_type="system", actor_id="outcomes", action="outcome_recorded", details=details
    )
    session.commit()
    session.expire_all()
    stored = session.get(AuditLog, row.id)
    assert stored.details == {"paid_amount": "95.00", "payment_date": "2026-11-01"}
    assert audit.verify_chain(session) is None


def test_details_with_raw_decimal_and_date(session):
    row = audit.record(
        session,
        actor_type="system",
        actor_id="tests",
        action="raw_types",
        details={"amount": Decimal("1.50"), "when": date(2026, 1, 2)},
    )
    session.commit()
    session.expire_all()
    assert session.get(AuditLog, row.id).details == {"amount": "1.50", "when": "2026-01-02"}
    assert audit.verify_chain(session) is None


def test_hash_changes_when_details_change():
    base = {
        "ts": "2026-10-28T12:00:00+00:00",
        "actor_type": "system",
        "actor_id": "x",
        "action": "a",
        "entity_type": None,
        "entity_id": None,
        "claim_id": None,
        "reason": None,
        "details": {"n": 1},
    }
    changed = dict(base, details={"n": 2})
    assert compute_hash(None, base) != compute_hash(None, changed)
    assert compute_hash("prev", base) != compute_hash(None, base)
    assert compute_hash(None, base) == compute_hash(None, dict(base))


def test_hash_changes_when_recorded_details_differ(session):
    a = audit.record(session, actor_type="system", actor_id="t", action="same", details={"n": 1})
    b = audit.record(session, actor_type="system", actor_id="t", action="same", details={"n": 2})
    session.commit()
    assert a.hash != b.hash
    assert b.prev_hash == a.hash
