"""`denial-agent` CLI: audit verification, user creation, clinic purge, migrations, import wiring."""

from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import sys
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine, func, inspect, select, text

from app import audit, pipeline
from app.cli import main
from app.config import PROJECT_ROOT
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
# CLPs that matched no claim carry the payer's spelling of a patient: exported, counted and cleared too.
UNMATCHED_TABLE = "remittance_claims_unmatched"
EXPORT_MEMBERS = {f"{t}.json" for t in (*CLINIC_TABLES, UNMATCHED_TABLE)} | {"manifest.json"}


def _expected_counts(unmatched: int = 0) -> dict[str, int]:
    """What `_seed_clinic_data` leaves: one row per clinic table plus the unmatched CLPs given."""
    return {**dict.fromkeys(CLINIC_TABLES, 1), UNMATCHED_TABLE: unmatched}


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


def _seed_files_on_disk(settings) -> dict[str, Path]:
    """What the app leaves on disk: uploads under DATA_DIR/uploads/<kind>/ (app/review/routes.py),
    export packages and an earlier hand-over zip under EXPORT_DIR (app/review/export.py, purge), plus
    a stray text file that is not the app's own artefact."""
    uploads = settings.data_path / "uploads"
    claims_upload = uploads / "claims" / "0123456789abcdef_claims_export.csv"
    remit_upload = uploads / "835" / "fedcba9876543210_remit_p.835"
    for path, text_ in (
        (claims_upload, "claim_control_number,patient_first_name,patient_last_name\nCLM-P1,Pat,Synthetic\n"),
        (remit_upload, "ISA*00*          *00*          *ZZ*SYNTHETIC      *ZZ*CLINIC         ~"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text_, encoding="utf-8")
    exports = settings.export_path
    package = exports / "work_item_1_CLM-P1_v1_20261001T000000Z.zip"
    old_handover = exports / "handed-over" / "clinic_1_export_20260101T000000Z.zip"
    for path in (package, old_handover):
        path.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("summary.json", "{}")
    stray = exports / "README.txt"
    stray.write_text("not an export package", encoding="utf-8")
    return {
        "claims_upload": claims_upload,
        "remit_upload": remit_upload,
        "package": package,
        "old_handover": old_handover,
        "stray": stray,
    }


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


def test_purge_clinic_without_confirm_only_reports(engine, session, clinic, settings, capsys):
    _seed_clinic_data(session, clinic)
    files = _seed_files_on_disk(settings)
    before = _counts(session)

    assert main(["purge-clinic", "--clinic-id", str(clinic.id)]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report["deleted"] is False
    assert report["counts"] == _expected_counts()
    assert report["files_on_disk"] == {"uploads": 2, "exports": 2}
    assert all(path.exists() for path in files.values())
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
    assert report["deleted"] is True and report["vacuumed"] is True
    export_file = Path(report["export_file"])
    assert export_file.exists()
    assert export_file.parent == settings.export_path
    with zipfile.ZipFile(export_file) as zf:
        assert set(zf.namelist()) == EXPORT_MEMBERS
        claims = json.loads(zf.read("claims.json"))
        assert len(claims) == 1
        assert claims[0]["claim_control_number"] == "CLM-P1" and claims[0]["total_charge"] == "120.00"
        manifest = json.loads(zf.read("manifest.json"))
        assert manifest["clinic_id"] == clinic.id
        assert manifest["tables"] == _expected_counts()

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


def test_purge_clinic_removes_uploads_and_export_packages(engine, session, clinic, settings, capsys):
    """Database rows are not the only PHI copies: every uploaded source file and every export
    package on disk goes too, the destruction record lists them, and the one file left behind is
    the hand-over zip the command just wrote."""
    _seed_clinic_data(session, clinic)
    files = _seed_files_on_disk(settings)

    assert main(["purge-clinic", str(clinic.id), "--confirm", "--reason", "pilot ended"]) == 0
    out, err = capsys.readouterr()
    report = json.loads(out)
    assert report["deleted"] is True and report["export_file_kept"] is True
    export_file = Path(report["export_file"])
    assert export_file.exists() and export_file.parent == settings.export_path
    assert str(export_file) in err and "hand-over copy kept" in err

    for name in ("claims_upload", "remit_upload", "package", "old_handover"):
        assert not files[name].exists(), name
    assert files["stray"].exists()  # only the app's own artefacts (uploads, zip packages) are removed
    assert list(settings.export_path.rglob("*.zip")) == [export_file]
    assert [p for p in (settings.data_path / "uploads").rglob("*") if p.is_file()] == []

    expected_files = {
        "uploads": ["835/fedcba9876543210_remit_p.835", "claims/0123456789abcdef_claims_export.csv"],
        "exports": [
            "handed-over/clinic_1_export_20260101T000000Z.zip",
            "work_item_1_CLM-P1_v1_20261001T000000Z.zip",
        ],
    }
    assert report["files_deleted"] == expected_files
    session.expire_all()
    assert {t: _counts(session)[t] for t in CLINIC_TABLES} == dict.fromkeys(CLINIC_TABLES, 0)
    event = session.execute(select(AuditLog).where(AuditLog.action == "clinic_purged")).scalar_one()
    assert event.reason == "pilot ended"
    assert event.details["files_deleted"] == {"uploads": 2, "exports": 2}
    assert event.details["deleted_files"] == expected_files
    assert event.details["export_file"] == export_file.name and event.details["export_file_kept"] is True
    assert audit.verify_chain(session) is None


def test_purge_clinic_refuses_to_report_destruction_when_a_file_survives(
    engine, session, clinic, settings, capsys, monkeypatch
):
    """A file that cannot be removed stops the purge before any row is deleted: exit 1, no
    hand-over zip, no `clinic_purged` event, and the error names the file (never its content)."""
    _seed_clinic_data(session, clinic)
    files = _seed_files_on_disk(settings)
    before = _counts(session)
    stuck = files["remit_upload"]
    real_unlink = Path.unlink

    def unlink(self: Path, *args, **kwargs):
        if self == stuck:
            raise PermissionError(13, "The process cannot access the file")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    assert main(["purge-clinic", str(clinic.id), "--confirm"]) == 1
    out, err = capsys.readouterr()
    assert out == ""
    assert "could not delete" in err and stuck.name in err and "PermissionError" in err
    assert "cannot access" not in err  # the OS message is not repeated either, only the class name
    assert stuck.exists()
    session.expire_all()
    assert _counts(session) == before
    assert list(settings.export_path.glob("clinic_*_export_*.zip")) == []
    purged = session.execute(
        select(func.count(AuditLog.id)).where(AuditLog.action == "clinic_purged")
    ).scalar_one()
    assert purged == 0
    assert audit.verify_chain(session) is None


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


def test_purge_clinic_clears_the_payer_spelling_on_unmatched_remittance_claims(
    engine, session, clinic, settings, capsys
):
    """A CLP that matched no claim still carries the patient name and member id the 835 named. In
    the one-clinic pilot it is the clinic's row: counted before the purge, handed over in the zip,
    and stripped of those identifiers (the ledger entry stays) before the destruction is recorded."""
    ids = _seed_clinic_data(session, clinic)
    unmatched = RemittanceClaim(
        remittance_id=ids["remittance_id"],
        claim_id=None,
        sequence=2,
        patient_control_number="UNKNOWN-0002",
        claim_status_code="4",
        charge_amount=Decimal("60.00"),
        paid_amount=Decimal("0"),
        patient_first_name="Zebulon",
        patient_last_name="Quixotic",
        member_id="MBR-UNMATCHED-2",
        is_denial=True,
        denied_amount=Decimal("60.00"),
    )
    session.add(unmatched)
    session.commit()
    unmatched_id = unmatched.id

    assert main(["purge-clinic", str(clinic.id)]) == 0
    assert json.loads(capsys.readouterr().out)["counts"] == _expected_counts(unmatched=1)

    assert main(["purge-clinic", str(clinic.id), "--confirm", "--reason", "pilot ended"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["deleted"] is True
    assert report["counts"]["remittance_claims_unmatched_cleared"] == 1
    assert report["counts"]["remittance_claims_unlinked"] == 1  # the matched CLP is not counted twice
    with zipfile.ZipFile(report["export_file"]) as zf:
        rows = json.loads(zf.read(f"{UNMATCHED_TABLE}.json"))
        assert [(r["id"], r["patient_first_name"], r["patient_last_name"], r["member_id"]) for r in rows] == [
            (unmatched_id, "Zebulon", "Quixotic", "MBR-UNMATCHED-2")
        ]
        assert json.loads(zf.read("manifest.json"))["tables"][UNMATCHED_TABLE] == 1

    session.expire_all()
    rc = session.get(RemittanceClaim, unmatched_id)
    assert rc is not None and rc.claim_id is None
    assert (rc.patient_first_name, rc.patient_last_name, rc.member_id) == (None, None, None)
    assert (rc.charge_amount, rc.claim_status_code, rc.is_denial) == (Decimal("60.00"), "4", True)
    assert all(
        row.patient_first_name is None and row.patient_last_name is None and row.member_id is None
        for row in session.execute(select(RemittanceClaim)).scalars()
    )
    event = session.execute(select(AuditLog).where(AuditLog.action == "clinic_purged")).scalar_one()
    assert event.details["counts"]["remittance_claims_unmatched_cleared"] == 1
    assert event.details["counts"]["remittance_claims_unlinked"] == 1
    assert audit.verify_chain(session) is None


def test_purge_clinic_leaves_no_deleted_identifier_in_the_sqlite_file(
    engine, session, clinic, settings, capsys
):
    """A DELETE only frees pages: without VACUUM the names, member ids and addresses stay readable
    in the database file. The purge vacuums the SQLite file once the transaction committed and
    reports it, so `strings` on the file finds nothing the destruction record says is gone."""
    _seed_clinic_data(session, clinic)
    for i in range(40):
        session.add(
            Patient(
                clinic_id=clinic.id,
                token=f"PT-VAN{i:04d}",
                account_number=f"ACC-VAN-{i:04d}",
                first_name="Zebulon",
                last_name="Vanishington",
                dob=date(1980, 1, 2),
                member_id=f"MBRVAN{i:07d}",
                address_line1="12 Vanishing Lane",
            )
        )
    session.commit()
    assert settings.is_sqlite
    db_file = Path(settings.database_url.removeprefix("sqlite:///"))
    before = db_file.read_bytes()
    # Control: the seeded identifiers are in the file, so the assertions below bite.
    assert before.count(b"Vanishington") >= 40 and before.count(b"MBRVAN") >= 40
    assert b"12 Vanishing Lane" in before

    assert main(["purge-clinic", str(clinic.id), "--confirm"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["deleted"] is True and report["vacuumed"] is True and "vacuum_error" not in report
    after = db_file.read_bytes()
    for needle in (b"Vanishington", b"Zebulon", b"12 Vanishing Lane", b"MBRVAN"):
        assert needle not in after, needle
    assert len(after) < len(before)
    session.expire_all()
    assert _counts(session)["patients"] == 0
    assert session.execute(select(AuditLog).where(AuditLog.action == "clinic_purged")).scalar_one()


def test_purge_clinic_reports_a_failed_vacuum_as_an_error(
    engine, session, clinic, settings, capsys, monkeypatch
):
    """The rows are deleted and recorded even when the file rewrite fails afterwards: the report says
    `vacuumed: false`, stderr names the error class (never its message) and the exit code is 1."""
    from app import cli

    _seed_clinic_data(session, clinic)

    def broken(settings_):
        raise RuntimeError("database is locked: C:/somewhere/private.db")

    monkeypatch.setattr(cli, "reclaim_deleted_pages", broken)
    assert main(["purge-clinic", str(clinic.id), "--confirm"]) == 1
    out, err = capsys.readouterr()
    report = json.loads(out)
    assert (
        report["deleted"] is True and report["vacuumed"] is False and report["vacuum_error"] == "RuntimeError"
    )
    assert "VACUUM failed with RuntimeError" in err and "private.db" not in err
    session.expire_all()
    assert _counts(session)["patients"] == 0
    assert session.execute(select(AuditLog).where(AuditLog.action == "clinic_purged")).scalar_one()


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
    def broken(session, **kwargs):
        raise RuntimeError("not for the operator")

    monkeypatch.setattr(pipeline, "process_pending", broken)
    assert main(["process"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["pipeline"] == "failed" and report["pipeline_error"] == "RuntimeError"
    assert "not for the operator" not in json.dumps(report)


def test_process_exit_code_reflects_a_partial_pipeline_run(engine, monkeypatch, capsys):
    """Some denials skipped (rolled back to their savepoints) is not success: exit 1 and the skipped
    remittance claim ids on stderr, so a scripted `process` or import cannot read `partial` as done."""
    from types import SimpleNamespace

    def partial(session, **kwargs):
        return pipeline.PipelineRun(
            [SimpleNamespace(id=11)],
            [pipeline.PipelineFailure(remittance_claim_id=7, claim_id=3, error="RuntimeError")],
        )

    monkeypatch.setattr(pipeline, "process_pending", partial)
    assert main(["process"]) == 1
    out, err = capsys.readouterr()
    report = json.loads(out)
    assert report["pipeline"] == "partial" and report["work_items_created"] == 1
    assert report["pipeline_failures"] == 1 and report["failed_remittance_claim_ids"] == [7]
    assert "pipeline partial (RuntimeError)" in err and "1 denial(s) skipped" in err and "[7]" in err
    assert "denial-agent process" in err


def test_process_exit_code_ok(engine, capsys):
    assert main(["process"]) == 0
    report = json.loads(capsys.readouterr().out)
    # "unavailable" is what the task reports when app.pipeline cannot be imported: never acceptable.
    assert report["work_items_created"] == 0 and report["pipeline"] == "ok"


# ---- serve ---------------------------------------------------------------------------------


def test_serve_keeps_uvicorn_on_the_masked_root_logging(settings, monkeypatch):
    import uvicorn

    calls: dict = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: calls.update(app=app, **kwargs))
    assert main(["serve", "--host", "127.0.0.1", "--port", "8123"]) == 0
    assert calls["app"] == "app.main:app" and (calls["host"], calls["port"]) == ("127.0.0.1", 8123)
    assert calls["reload"] is False and calls["log_level"] == "info"
    # uvicorn's default dictConfig gives `uvicorn` and `uvicorn.access` handlers of their own with
    # propagate=False, outside the PHI-masking filter; None keeps its loggers on the root handler
    # (tests/test_phi_in_logs.py proves the folding).
    assert calls["log_config"] is None


def test_serve_refuses_a_placeholder_secret_key_with_a_readable_error(settings, monkeypatch, capsys):
    """Outside the test environment a placeholder SECRET_KEY stops `serve` before uvicorn imports the
    app: exit 1 and one sentence naming SECRET_KEY and .env, instead of the bare `RuntimeError` plus
    frames the generic handler prints when create_app refuses inside uvicorn's import."""
    import uvicorn

    from app.config import reset_settings_cache

    calls: list = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("SECRET_KEY", "change-me")
    reset_settings_cache()
    try:
        assert main(["serve", "--host", "127.0.0.1", "--port", "8124"]) == 1
    finally:
        reset_settings_cache()
    out, err = capsys.readouterr()
    assert calls == [] and out == ""
    assert err.startswith("error: ") and "SECRET_KEY" in err and ".env" in err
    assert "RuntimeError" not in err and 'File "' not in err


# ---- init-db, create-clinic, imports, generate-synthetic ------------------------------------


def test_init_db_migrate_on_fresh_database(settings):
    assert main(["init-db", "--migrate"]) == 0
    engine = create_engine(settings.database_url)
    try:
        tables = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert {"alembic_version", "work_items", "audit_log", "claims"} <= tables


def test_init_db_migrate_prints_only_its_own_line(settings, tmp_path):
    """In a real process (not under caplog) the CLI's logging quiets alembic: one line on stdout,
    nothing on stderr."""
    env = dict(os.environ, DATABASE_URL=f"sqlite:///{(tmp_path / 'sub.db').as_posix()}")
    proc = subprocess.run(
        [sys.executable, "-m", "app.cli", "init-db", "--migrate"],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.splitlines() == ["database migrated to head"]
    assert proc.stderr.strip() == "", proc.stderr


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
    out = tmp_path / "syn"
    assert main(["generate-synthetic", "--out", str(out), "--seed", "7", "--claims", "20"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "claims_export.csv" in payload["files"]
    assert (out / "claims_export.csv").exists()


# ---- list-payers / set-payer ----------------------------------------------------------------


def _seed_payer(session, payer_id: str = "SYN-P1") -> int:
    payer = Payer(name="Synthetic Plan", payer_id=payer_id)
    session.add(payer)
    session.commit()
    return payer.id


def test_list_payers_prints_windows(engine, session, capsys):
    _seed_payer(session, "SYN-P2")
    _seed_payer(session, "SYN-P1")
    assert main(["list-payers"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["count"] == 2
    assert [row["payer_id"] for row in payload["payers"]] == ["SYN-P1", "SYN-P2"]
    first = payload["payers"][0]
    assert first["name"] == "Synthetic Plan"
    assert (first["timely_filing_days"], first["corrected_claim_days"], first["appeal_days"]) == (90, 90, 180)


def test_set_payer_updates_windows_and_audits(engine, session, capsys):
    payer_id = _seed_payer(session)
    argv = ["set-payer", "--payer-id", "SYN-P1", "--timely-filing-days", "365", "--appeal-days", "60"]
    assert main(argv) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["payer_id"] == "SYN-P1" and payload["id"] == payer_id
    assert (payload["timely_filing_days"], payload["corrected_claim_days"], payload["appeal_days"]) == (
        365,
        90,
        60,
    )

    session.expire_all()
    stored = session.get(Payer, payer_id)
    assert (stored.timely_filing_days, stored.corrected_claim_days, stored.appeal_days) == (365, 90, 60)
    events = session.execute(select(AuditLog).where(AuditLog.action == "payer_windows_set")).scalars().all()
    assert len(events) == 1
    event = events[0]
    assert event.entity_type == "payer" and event.entity_id == payer_id
    assert event.actor_type == "user" and event.actor_id.startswith("cli")
    assert event.details["payer_id"] == "SYN-P1"
    assert event.details["before"] == {"timely_filing_days": 90, "appeal_days": 180}
    assert event.details["after"] == {"timely_filing_days": 365, "appeal_days": 60}
    assert audit.verify_chain(session) is None


def test_set_payer_rejects_unknown_payer_missing_and_bad_windows(engine, session, capsys):
    payer_id = _seed_payer(session)
    assert main(["set-payer", "--payer-id", "NOPE", "--appeal-days", "60"]) == 1
    assert "unknown payer" in capsys.readouterr().err
    assert main(["set-payer", "--payer-id", "SYN-P1"]) == 1
    assert "no window given" in capsys.readouterr().err
    assert main(["set-payer", "--payer-id", "SYN-P1", "--appeal-days", "0"]) == 1
    assert "between" in capsys.readouterr().err
    assert main(["set-payer", "--payer-id", "SYN-P1", "--timely-filing-days", "99999"]) == 1
    assert "between" in capsys.readouterr().err

    session.expire_all()
    stored = session.get(Payer, payer_id)
    assert (stored.timely_filing_days, stored.corrected_claim_days, stored.appeal_days) == (90, 90, 180)
    assert (
        session.execute(
            select(func.count(AuditLog.id)).where(AuditLog.action == "payer_windows_set")
        ).scalar_one()
        == 0
    )
