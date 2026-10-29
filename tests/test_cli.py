"""`denial-agent` CLI: audit verification, user creation, clinic purge, migrations, import wiring."""

from __future__ import annotations

import io
import json
import logging
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, inspect, select, text

from app import audit
from app.cli import main
from app.models import (
    AuditLog,
    Claim,
    ClaimLine,
    Clinic,
    ModelCall,
    NoteExcerpt,
    Outcome,
    Patient,
    Payer,
    PayerDocument,
    Proposal,
    Remittance,
    RemittanceClaim,
    RemittanceLine,
    ReviewAction,
    User,
    WorkItem,
)

BASE32 = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")
PASSWORD = "correct horse battery staple 42!"
CLINIC_TABLES = (
    "patients",
    "claims",
    "claim_lines",
    "note_excerpts",
    "payer_documents",
    "work_items",
    "proposals",
    "actions",
    "outcomes",
)


# ---- verify-audit ---------------------------------------------------------------------------


def test_verify_audit_exit_codes(engine, session, capsys):
    first = audit.record(session, actor_type="system", actor_id="tests", action="first")
    audit.record(session, actor_type="system", actor_id="tests", action="second")
    session.commit()

    assert main(["verify-audit"]) == 0
    assert capsys.readouterr().out.strip() == "OK"

    session.execute(text("UPDATE audit_log SET action = 'tampered' WHERE id = :id"), {"id": first.id})
    session.commit()
    assert main(["verify-audit"]) == 1
    out = capsys.readouterr().out
    assert str(first.id) in out and "OK" not in out


# ---- create-user ----------------------------------------------------------------------------


def test_create_user_with_password_stdin(engine, session, monkeypatch, capsys):
    import pyotp
    from argon2 import PasswordHasher

    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    assert main(["create-user", "--email", "ops@example.test", "--role", "admin", "--password-stdin"]) == 0
    out = capsys.readouterr().out
    assert PASSWORD not in out
    assert "otpauth://totp/" in out and "Denial%20Recovery%20Agent" in out

    user = session.execute(select(User).where(User.email == "ops@example.test")).scalar_one()
    assert user.password_hash.startswith("$argon2")
    assert PasswordHasher().verify(user.password_hash, PASSWORD)
    assert len(user.totp_secret) == 32 and set(user.totp_secret) <= BASE32
    assert f"secret={user.totp_secret}" in out
    assert pyotp.TOTP(user.totp_secret).verify(pyotp.TOTP(user.totp_secret).now())
    assert user.role == "admin" and user.is_active is True


def test_create_user_rejects_short_password(engine, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    assert main(["create-user", "--email", "short@example.test", "--password-stdin"]) == 1
    assert "at least" in capsys.readouterr().err


def test_create_user_duplicate_email_fails_cleanly(engine, session, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    assert main(["create-user", "--email", "dup@example.test", "--password-stdin"]) == 0
    capsys.readouterr()
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    assert main(["create-user", "--email", "dup@example.test", "--password-stdin"]) == 1
    assert "error:" in capsys.readouterr().err
    assert (
        session.execute(select(func.count(User.id)).where(User.email == "dup@example.test")).scalar_one() == 1
    )


# ---- purge-clinic ---------------------------------------------------------------------------


def _seed_clinic_data(session, clinic) -> dict[str, int]:
    payer = Payer(name="Synthetic Plan", payer_id="SYN-P1")
    session.add(payer)
    session.flush()
    patient = Patient(
        clinic_id=clinic.id,
        token="PT-PURGE1",
        account_number="ACC-P1",
        first_name="Pat",
        last_name="Synthetic",
    )
    session.add(patient)
    session.flush()
    claim = Claim(
        clinic_id=clinic.id,
        payer_id=payer.id,
        patient_id=patient.id,
        claim_control_number="CLM-P1",
        total_charge=Decimal("120.00"),
        status="denied",
    )
    session.add(claim)
    session.flush()
    line = ClaimLine(
        claim_id=claim.id, line_number=1, procedure_code="99213", charge_amount=Decimal("120.00")
    )
    session.add(line)
    session.flush()
    session.add(NoteExcerpt(claim_id=claim.id, text="Synthetic note excerpt", author="Dr. Synthetic"))
    session.add(PayerDocument(claim_id=claim.id, document_type="letter", text="Synthetic payer letter"))
    remittance = Remittance(
        payer_id=payer.id,
        file_name="remit_p.835",
        file_sha256="p" * 64,
        payment_amount=Decimal("0"),
        payment_date=date(2026, 9, 1),
    )
    session.add(remittance)
    session.flush()
    rc = RemittanceClaim(
        remittance=remittance,
        claim=claim,
        sequence=1,
        patient_control_number="CLM-P1",
        claim_status_code="4",
        charge_amount=Decimal("120.00"),
        paid_amount=Decimal("0"),
        patient_first_name="Pat",
        patient_last_name="Synthetic",
        member_id="MBR-P1",
        is_denial=True,
        denied_amount=Decimal("120.00"),
        match_method="exact_ccn",
    )
    session.add(rc)
    session.flush()
    session.add(
        RemittanceLine(
            remittance_claim_id=rc.id,
            claim_line_id=line.id,
            line_number=1,
            procedure_code="99213",
            charge_amount=Decimal("120.00"),
            paid_amount=Decimal("0"),
        )
    )
    work_item = WorkItem(
        claim=claim,
        remittance_claim=rc,
        category="modifier",
        category_confidence=0.9,
        status="closed",
        dollar_value=Decimal("120.00"),
    )
    session.add(work_item)
    session.flush()
    proposal = Proposal(
        work_item_id=work_item.id, version=1, action_type="corrected_claim", summary="s", rationale="r"
    )
    session.add(proposal)
    session.flush()
    session.add(
        ReviewAction(work_item_id=work_item.id, proposal_id=proposal.id, action="approve", review_seconds=60)
    )
    session.add(
        Outcome(
            work_item_id=work_item.id, remittance_claim_id=rc.id, result="paid", paid_amount=Decimal("120.00")
        )
    )
    session.add(
        ModelCall(
            prompt_name="classify_denial",
            prompt_version="classify_denial.v1@deadbeef",
            model_id="stub",
            purpose="classify",
            work_item_id=work_item.id,
        )
    )
    session.commit()
    return {"remittance_id": remittance.id, "rc_id": rc.id, "work_item_id": work_item.id}


def _counts(session) -> dict[str, int]:
    models = {
        "patients": Patient,
        "claims": Claim,
        "claim_lines": ClaimLine,
        "note_excerpts": NoteExcerpt,
        "payer_documents": PayerDocument,
        "work_items": WorkItem,
        "proposals": Proposal,
        "actions": ReviewAction,
        "outcomes": Outcome,
        "remittances": Remittance,
        "remittance_claims": RemittanceClaim,
        "remittance_lines": RemittanceLine,
        "model_calls": ModelCall,
    }
    return {
        name: session.execute(select(func.count(model.id))).scalar_one() for name, model in models.items()
    }


def test_purge_clinic_without_confirm_only_reports(engine, session, clinic, capsys):
    _seed_clinic_data(session, clinic)
    before = _counts(session)

    assert main(["purge-clinic", "--clinic-id", str(clinic.id)]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["deleted"] is False
    assert report["counts"] == dict.fromkeys(CLINIC_TABLES, 1)
    session.expire_all()
    assert _counts(session) == before
    purged = session.execute(
        select(func.count(AuditLog.id)).where(AuditLog.action == "clinic_purged")
    ).scalar_one()
    assert purged == 0


def test_purge_clinic_confirm_exports_then_deletes(engine, session, clinic, settings, capsys):
    ids = _seed_clinic_data(session, clinic)

    assert main(["purge-clinic", "--clinic-id", str(clinic.id), "--confirm", "--reason", "pilot ended"]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["deleted"] is True
    export_file = Path(report["export_file"])
    assert export_file.exists()
    assert export_file.parent == settings.export_path
    with zipfile.ZipFile(export_file) as zf:
        names = set(zf.namelist())
        assert {f"{t}.json" for t in CLINIC_TABLES} | {"manifest.json"} == names
        claims = json.loads(zf.read("claims.json"))
        assert len(claims) == 1
        assert claims[0]["claim_control_number"] == "CLM-P1" and claims[0]["total_charge"] == "120.00"
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["clinic_id"] == clinic.id
        assert manifest["tables"] == dict.fromkeys(CLINIC_TABLES, 1)

    session.expire_all()
    after = _counts(session)
    assert {t: after[t] for t in CLINIC_TABLES} == dict.fromkeys(CLINIC_TABLES, 0)
    assert (
        after["remittances"],
        after["remittance_claims"],
        after["remittance_lines"],
        after["model_calls"],
    ) == (1, 1, 1, 1)
    rc = session.get(RemittanceClaim, ids["rc_id"])
    assert rc.claim_id is None and rc.match_method is None
    assert rc.patient_first_name is None and rc.patient_last_name is None and rc.member_id is None
    assert session.execute(select(RemittanceLine)).scalar_one().claim_line_id is None
    assert session.execute(select(ModelCall)).scalar_one().work_item_id is None
    assert session.get(Clinic, clinic.id) is not None

    event = session.execute(select(AuditLog).where(AuditLog.action == "clinic_purged")).scalar_one()
    assert (event.entity_type, event.entity_id, event.reason) == ("clinic", clinic.id, "pilot ended")
    assert event.details["export_file"] == export_file.name
    assert event.details["counts"]["claims"] == 1 and event.details["counts"]["patients"] == 1
    assert event.details["counts"]["remittance_claims_unlinked"] == 1
    assert audit.verify_chain(session) is None
    assert main(["verify-audit"]) == 0


def test_purge_unknown_clinic(engine, capsys):
    assert main(["purge-clinic", "--clinic-id", "999"]) == 1
    assert "not found" in capsys.readouterr().err
    assert main(["purge-clinic", "999"]) == 1
    assert "not found" in capsys.readouterr().err


def test_purge_clinic_positional_id_as_in_spec(engine, session, clinic, capsys):
    """docs/SPEC.md writes the command as `denial-agent purge-clinic <id>`."""
    _seed_clinic_data(session, clinic)
    assert main(["purge-clinic", str(clinic.id)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["clinic_id"] == clinic.id and report["deleted"] is False
    assert report["counts"]["claims"] == 1

    assert main(["purge-clinic"]) == 1
    assert "clinic id" in capsys.readouterr().err
    session.expire_all()
    assert _counts(session)["claims"] == 1  # nothing deleted without --confirm


def test_purge_clinic_positional_confirm_deletes(engine, session, clinic, capsys):
    _seed_clinic_data(session, clinic)
    assert main(["purge-clinic", str(clinic.id), "--confirm"]) == 0
    assert json.loads(capsys.readouterr().out)["deleted"] is True
    session.expire_all()
    assert _counts(session)["claims"] == 0 and _counts(session)["patients"] == 0


# ---- unexpected errors and exit codes ----------------------------------------------------------


def test_unexpected_exception_prints_class_name_and_frames_only(engine, monkeypatch, capsys, caplog):
    from app.jobs import tasks

    secret = "RuntimeError: member id MBR-SECRET-1 for Jane Synthetic"

    def explode(created_by="system"):
        raise RuntimeError(secret)

    monkeypatch.setattr(tasks, "process_pending", explode)
    with caplog.at_level(logging.DEBUG):
        assert main(["process"]) == 2
    captured = capsys.readouterr()
    assert "process failed with RuntimeError" in captured.err
    assert "explode" in captured.err  # the frame that raised, so the operator can locate it
    assert "Jane Synthetic" not in captured.err and "MBR-SECRET-1" not in captured.err
    assert "Jane Synthetic" not in captured.out
    assert "Jane Synthetic" not in caplog.text and "MBR-SECRET-1" not in caplog.text


def test_import_exit_code_reflects_failed_batch(engine, session, clinic, tmp_path, capsys):
    bad = tmp_path / "claims_export.csv"
    bad.write_text("claim_control_number,foo\nX,1\n", encoding="utf-8")
    assert main(["import-claims", str(bad), "--clinic-id", str(clinic.id)]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "failed" and report["kind"] == "claims"
    assert report["error"] and "missing columns" in report["error"]


def test_process_exit_code_reflects_pipeline_failure(engine, monkeypatch, capsys):
    pipeline = pytest.importorskip("app.pipeline")

    def broken(session, **kwargs):
        raise RuntimeError("not for the operator")

    monkeypatch.setattr(pipeline, "process_pending", broken)
    assert main(["process"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["pipeline"] == "failed" and report["pipeline_error"] == "RuntimeError"
    assert "not for the operator" not in json.dumps(report)


def test_process_exit_code_ok(engine, capsys):
    assert main(["process"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["work_items_created"] == 0 and report["pipeline"] in ("ok", "unavailable")


# ---- init-db, create-clinic, imports, generate-synthetic ------------------------------------


def test_init_db_migrate_on_fresh_database(settings):
    assert main(["init-db", "--migrate"]) == 0
    engine = create_engine(settings.database_url)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert {"alembic_version", "work_items", "audit_log", "claims"} <= tables


def test_init_db_create_all(settings):
    assert main(["init-db"]) == 0
    engine = create_engine(settings.database_url)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert "work_items" in tables and "alembic_version" not in tables


def test_create_clinic_prints_id(engine, session, capsys):
    assert main(["create-clinic", "--name", "Synthetic Pilot Clinic", "--pm-system", "synthetic"]) == 0
    payload = json.loads(capsys.readouterr().out)
    clinic = session.get(Clinic, payload["clinic_id"])
    assert clinic is not None and clinic.name == "Synthetic Pilot Clinic" and clinic.pm_system == "synthetic"
    assert (
        session.execute(
            select(func.count(AuditLog.id)).where(AuditLog.action == "clinic_created")
        ).scalar_one()
        == 1
    )


def test_import_commands_require_existing_file(engine, tmp_path, capsys):
    assert main(["import-835", str(tmp_path / "missing.835")]) == 1
    assert "file not found" in capsys.readouterr().err
    assert main(["import-claims", str(tmp_path / "missing.csv"), "--clinic-id", "1"]) == 1


def test_import_claims_requires_a_clinic(engine, tmp_path, capsys):
    csv_file = tmp_path / "claims_export.csv"
    csv_file.write_text("claim_control_number\n", encoding="utf-8")
    assert main(["import-claims", str(csv_file)]) == 1
    assert "no clinic exists" in capsys.readouterr().err


def test_generate_synthetic_writes_batch(settings, tmp_path, capsys):
    pytest.importorskip("synthetic.generate")
    out = tmp_path / "syn"
    assert main(["generate-synthetic", "--out", str(out), "--seed", "7", "--claims", "20"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "claims_export.csv" in payload["files"]
    assert (out / "claims_export.csv").exists()
