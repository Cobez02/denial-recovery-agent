"""`denial-agent` command line (milestone M1 operations).

Subcommands: init-db, create-clinic, create-user, import-835, import-claims, import-notes,
import-documents, process, generate-synthetic, verify-audit, list-payers, set-payer, purge-clinic,
serve.

Output policy: stdout carries ids, counts, statuses and file names only. Passwords are never
printed. The TOTP provisioning URI (which embeds the shared secret) is printed exactly once, on
purpose, so the operator can enrol an authenticator app; the ASCII QR code is the same data.
An unexpected exception ends the command with exit code 2, its class name and the traceback
frames only: exception messages can quote imported data and are never written to stderr or logs.

`purge-clinic <id> --confirm` (pilot termination) removes every uploaded file under DATA_DIR/uploads
and every export package under EXPORT_DIR, exports the clinic's rows to one zip, deletes the rows
(remittances stay as the payment ledger: matched CLPs are unlinked, and every CLP, matched or not,
loses the patient name and member id the payer echoed back), records the destruction with the file
names and, on SQLite, runs VACUUM so the deleted rows' pages are rewritten rather than left free in
the file; the zip is the hand-over copy and is the one PHI-bearing file left behind on purpose.

Exit codes: 0 success; 1 a user-facing error, a failed import batch, a pipeline run that failed or
skipped denials (`partial`: stderr names the skipped remittance claim ids), a broken audit chain or
a purge whose VACUUM failed; 2 an unexpected exception; 130 interrupted.
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import sys
import zipfile
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.sql import Select

from app import audit
from app.config import PROJECT_ROOT, Settings, get_settings, is_placeholder_secret
from app.db import get_engine, init_db, session_scope
from app.enums import ActorType, UserRole
from app.jobs.runner import format_frames
from app.jobs.tasks import PIPELINE_FAILED, PIPELINE_PARTIAL
from app.logging_config import configure_logging
from app.models import (
    Claim,
    ClaimLine,
    Clinic,
    ModelCall,
    NoteExcerpt,
    Outcome,
    Patient,
    PayerDocument,
    Proposal,
    RemittanceClaim,
    RemittanceLine,
    ReviewAction,
    User,
    WorkItem,
)

# Named explicitly: `python -m app.cli` would otherwise log as `__main__`.
log = logging.getLogger("app.cli")

ISSUER_NAME = "Denial Recovery Agent"
MIN_PASSWORD_LENGTH = 12
CLI_ACTOR = "cli"
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_UNEXPECTED = 2
EXIT_INTERRUPTED = 130
PURGE_TABLES: tuple[str, ...] = (
    "patients",
    "claims",
    "claim_lines",
    "note_excerpts",
    "payer_documents",
    "work_items",
    "proposals",
    "actions",
    "outcomes",
    "remittance_claims_unmatched",
)
# Files the app writes under DATA_DIR / EXPORT_DIR; purge-clinic removes them (see purge_files_on_disk).
UPLOADS_DIR_NAME = "uploads"
EXPORT_PACKAGE_GLOB = "*.zip"
HANDOVER_NOTE = (
    "the export zip is the hand-over copy for the clinic: it holds the purged rows in full; "
    "delete it once the clinic has received it"
)
# The refusal app.main.create_app makes for the same key, as a user-facing error.
PLACEHOLDER_SECRET_ERROR = (
    "SECRET_KEY is missing or a placeholder; session cookies would be forgeable. Put a random value "
    'in .env (python -c "import secrets; print(secrets.token_urlsafe(48))") and start again.'
)


class CliError(Exception):
    """A user-facing error: printed to stderr, exit code 1."""


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _emit(payload: Any, *, compact: bool = False) -> None:
    if compact:
        print(json.dumps(payload, default=_json_default, sort_keys=True))
    else:
        print(json.dumps(payload, default=_json_default, sort_keys=True, indent=2))


def _cli_actor() -> str:
    try:
        user = getpass.getuser()
    except Exception:  # no login name available (containers, services)
        return CLI_ACTOR
    return f"{CLI_ACTOR}:{user}"[:80] if user else CLI_ACTOR


def _configure_logging_once() -> None:
    if not logging.getLogger().handlers:
        configure_logging(get_settings().log_level)


def _existing_file(raw: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_file():
        raise CliError(f"file not found: {path}")
    return path.resolve()


def _task_exit_code(result: dict[str, Any]) -> int:
    """Non-zero when the import batch failed or the pipeline failed or skipped denials (the JSON says which).

    A `partial` run rolled some denials back to their savepoints and a `failed` run processed none:
    either way denials stay pending, so the exit code is 1 and stderr says how many and which
    remittance claims (ids only), for scripted runs that never read the JSON.
    """
    code = EXIT_ERROR if result.get("status") == "failed" else EXIT_OK
    pipeline_status = result.get("pipeline")
    if pipeline_status in (PIPELINE_PARTIAL, PIPELINE_FAILED):
        skipped = [int(rc_id) for rc_id in result.get("failed_remittance_claim_ids") or []]
        failures = int(result.get("pipeline_failures") or len(skipped))
        what = (
            f"{failures} denial(s) skipped (remittance claim ids {skipped})"
            if pipeline_status == PIPELINE_PARTIAL
            else "no denial was processed"
        )
        print(
            f"error: pipeline {pipeline_status} ({result.get('pipeline_error') or 'no exception'}): {what}; "
            "the pending denials wait for `denial-agent process`",
            file=sys.stderr,
        )
        code = EXIT_ERROR
    return code


def _resolve_clinic_id(session: Session, explicit: int | None) -> int:
    """--clinic-id, else CLINIC_ID from settings, else the only clinic in the database."""
    clinic_id = explicit if explicit is not None else get_settings().clinic_id
    if clinic_id is None:
        ids = list(session.execute(select(Clinic.id).order_by(Clinic.id)).scalars())
        if len(ids) == 1:
            clinic_id = ids[0]
        elif not ids:
            raise CliError("no clinic exists yet; run `denial-agent create-clinic --name ...` first")
        else:
            raise CliError("several clinics exist; pass --clinic-id or set CLINIC_ID")
    if session.get(Clinic, clinic_id) is None:
        raise CliError(f"clinic {clinic_id} not found")
    return int(clinic_id)


def alembic_config() -> Any:
    """Alembic Config pointing at this repository's alembic.ini and migrations, from any cwd."""
    from alembic.config import Config

    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", (PROJECT_ROOT / "app" / "migrations").as_posix())
    return cfg


def run_migrations(revision: str = "head") -> None:
    """Programmatic `alembic upgrade <revision>` against DATABASE_URL."""
    from alembic import command

    command.upgrade(alembic_config(), revision)


# --------------------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------------------


def cmd_init_db(args: argparse.Namespace) -> int:
    settings = get_settings()
    settings.data_path.mkdir(parents=True, exist_ok=True)
    if args.migrate:
        run_migrations("head")
        print("database migrated to head")
    else:
        init_db()
        print("database tables created")
    return 0


def cmd_create_clinic(args: argparse.Namespace) -> int:
    with session_scope() as session:
        clinic = Clinic(name=args.name.strip(), npi=args.npi, tax_id=args.tax_id, pm_system=args.pm_system)
        session.add(clinic)
        session.flush()
        audit.record(
            session,
            actor_type=ActorType.USER,
            actor_id=_cli_actor(),
            action="clinic_created",
            entity_type="clinic",
            entity_id=clinic.id,
            details={"source": "cli"},
        )
        clinic_id = clinic.id
    _emit({"clinic_id": clinic_id, "name": args.name.strip()}, compact=True)
    return 0


def _read_password(args: argparse.Namespace) -> str:
    if args.password_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass("Password: ")
        if password != getpass.getpass("Repeat password: "):
            raise CliError("passwords do not match")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise CliError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    return password


def _create_user(session: Session, email: str, password: str, role: str) -> User:
    """Use app.auth.service.create_user when it exists; otherwise the same contract inline."""
    try:
        from app.auth.service import create_user as service_create_user
    except ImportError:
        service_create_user = None
    if service_create_user is not None:
        return service_create_user(session, email=email, password=password, role=role)

    import pyotp
    from argon2 import PasswordHasher

    existing = session.execute(select(User).where(User.email == email)).scalar_one_or_none()
    if existing is not None:
        raise CliError("a user with that email already exists")
    user = User(
        email=email,
        password_hash=PasswordHasher().hash(password),
        totp_secret=pyotp.random_base32(),
        role=role,
        is_active=True,
        failed_logins=0,
    )
    session.add(user)
    session.flush()
    audit.record(
        session,
        actor_type=ActorType.SYSTEM,
        actor_id=CLI_ACTOR,
        action="user_created",
        entity_type="user",
        entity_id=user.id,
        details={"role": role, "source": "cli"},
    )
    return user


def _print_qr(uri: str) -> None:
    try:
        import qrcode
    except ImportError:  # pragma: no cover - dependency is pinned
        print("(qrcode is not installed; enter the URI above in your authenticator app)")
        return
    qr = qrcode.QRCode(border=1)
    qr.add_data(uri)
    qr.make(fit=True)
    try:
        qr.print_ascii(invert=True)
    except UnicodeEncodeError:
        print("(this terminal cannot draw the QR code; enter the URI above in your authenticator app)")


def cmd_create_user(args: argparse.Namespace) -> int:
    import pyotp

    email = args.email.strip()
    if "@" not in email:
        raise CliError("--email must be an email address")
    password = _read_password(args)
    try:
        with session_scope() as session:
            user = _create_user(session, email, password, args.role)
            session.flush()
            user_id, secret = user.id, user.totp_secret
    except IntegrityError as exc:
        raise CliError("a user with that email already exists") from exc
    except ValueError as exc:
        raise CliError(str(exc)) from exc
    uri = pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=ISSUER_NAME)
    print(f"created user id={user_id} role={args.role}")
    print("TOTP provisioning URI (scan the QR code or paste this into an authenticator app):")
    print(uri)
    _print_qr(uri)
    return 0


def cmd_import_835(args: argparse.Namespace) -> int:
    from app.jobs import tasks

    path = _existing_file(args.file)
    result = tasks.import_835_file(str(path), file_name=path.name, created_by=_cli_actor())
    _emit(result)
    return _task_exit_code(result)


def _import_clinic_file(args: argparse.Namespace, task: Callable[..., dict[str, Any]]) -> int:
    path = _existing_file(args.file)
    with session_scope() as session:
        clinic_id = _resolve_clinic_id(session, args.clinic_id)
    result = task(str(path), clinic_id=clinic_id, created_by=_cli_actor())
    _emit(result)
    return _task_exit_code(result)


def cmd_import_claims(args: argparse.Namespace) -> int:
    from app.jobs import tasks

    return _import_clinic_file(args, tasks.import_claims_file)


def cmd_import_notes(args: argparse.Namespace) -> int:
    from app.jobs import tasks

    return _import_clinic_file(args, tasks.import_notes_file)


def cmd_import_documents(args: argparse.Namespace) -> int:
    from app.jobs import tasks

    return _import_clinic_file(args, tasks.import_documents_file)


def cmd_process(args: argparse.Namespace) -> int:
    from app.jobs import tasks

    result = tasks.process_pending(created_by=_cli_actor())
    _emit(result)
    return _task_exit_code(result)


def cmd_generate_synthetic(args: argparse.Namespace) -> int:
    try:
        from synthetic.generate import generate
    except ImportError as exc:
        raise CliError(f"synthetic generator is not available ({type(exc).__name__})") from exc
    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    generate(seed=args.seed, n_claims=args.claims, out_dir=out_dir)
    files = sorted(p.name for p in out_dir.iterdir() if p.is_file())
    _emit({"out_dir": str(out_dir), "seed": args.seed, "claims": args.claims, "files": files})
    return 0


def cmd_verify_audit(args: argparse.Namespace) -> int:
    with session_scope() as session:
        broken = audit.verify_chain(session)
    if broken is None:
        print("OK")
        return 0
    print(f"audit chain broken at row {broken}")
    return 1


# ---- payer windows (docs/SPEC.md section 2) ------------------------------------------------


def cmd_list_payers(args: argparse.Namespace) -> int:
    """Print every payer with its timely-filing, corrected-claim and appeal windows (ids, names, days)."""
    from app.ingest.payers import list_payers

    with session_scope() as session:
        rows = list_payers(session)
    _emit({"payers": rows, "count": len(rows)})
    return 0


def cmd_set_payer(args: argparse.Namespace) -> int:
    """Set one or more windows of an existing payer; app.ingest.payers records the audit event.

    The claims importer creates payers with the settings defaults and never overwrites windows set
    here. Unknown payer ids, windows outside 1..3650 days and a call without any window are
    user-facing errors (exit 1); nothing is changed then.
    """
    from app.ingest.payers import UnknownPayerError, set_payer_windows

    try:
        with session_scope() as session:
            payer = set_payer_windows(
                session,
                args.payer_id,
                timely_filing_days=args.timely_filing_days,
                corrected_claim_days=args.corrected_claim_days,
                appeal_days=args.appeal_days,
                actor_id=_cli_actor(),
                actor_type=ActorType.USER,
            )
            result = {
                "id": payer.id,
                "payer_id": payer.payer_id,
                "name": payer.name,
                "timely_filing_days": payer.timely_filing_days,
                "corrected_claim_days": payer.corrected_claim_days,
                "appeal_days": payer.appeal_days,
            }
    except UnknownPayerError as exc:
        raise CliError(f"{exc} (payers are created by import-claims; see list-payers)") from exc
    except ValueError as exc:
        raise CliError(str(exc)) from exc
    _emit(result, compact=True)
    return 0


# ---- purge-clinic -------------------------------------------------------------------------


def _clinic_selections(clinic_id: int) -> dict[str, Select[Any]]:
    """Row selections for everything that belongs to a clinic, keyed by table name.

    ``remittance_claims_unmatched`` are the CLPs that matched no claim: they carry the payer's
    spelling of a patient (name, member id) and have no clinic column, and the pilot serves one
    clinic, so they are its rows (``files_on_disk`` reasons the same way about uploads).
    """
    claim_ids = select(Claim.id).where(Claim.clinic_id == clinic_id)
    work_item_ids = select(WorkItem.id).where(WorkItem.claim_id.in_(claim_ids))
    rc_ids = select(RemittanceClaim.id).where(RemittanceClaim.claim_id.in_(claim_ids))
    return {
        "patients": select(Patient).where(Patient.clinic_id == clinic_id),
        "claims": select(Claim).where(Claim.clinic_id == clinic_id),
        "claim_lines": select(ClaimLine).where(ClaimLine.claim_id.in_(claim_ids)),
        "note_excerpts": select(NoteExcerpt).where(NoteExcerpt.claim_id.in_(claim_ids)),
        "payer_documents": select(PayerDocument).where(
            or_(PayerDocument.claim_id.in_(claim_ids), PayerDocument.remittance_claim_id.in_(rc_ids))
        ),
        "work_items": select(WorkItem).where(WorkItem.id.in_(work_item_ids)),
        "proposals": select(Proposal).where(Proposal.work_item_id.in_(work_item_ids)),
        "actions": select(ReviewAction).where(ReviewAction.work_item_id.in_(work_item_ids)),
        "outcomes": select(Outcome).where(Outcome.work_item_id.in_(work_item_ids)),
        "remittance_claims_unmatched": select(RemittanceClaim).where(RemittanceClaim.claim_id.is_(None)),
    }


def _count(session: Session, stmt: Select[Any]) -> int:
    return int(session.execute(select(func.count()).select_from(stmt.subquery())).scalar_one())


def clinic_counts(session: Session, clinic_id: int) -> dict[str, int]:
    return {name: _count(session, stmt) for name, stmt in _clinic_selections(clinic_id).items()}


def _row_to_dict(obj: Any) -> dict[str, Any]:
    return {attr.key: getattr(obj, attr.key) for attr in sa_inspect(obj).mapper.column_attrs}


def export_clinic(session: Session, clinic: Clinic, export_dir: Path) -> Path:
    """Write every row that belongs to the clinic as JSON files inside one zip (data returned)."""
    export_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    target = export_dir / f"clinic_{clinic.id}_export_{stamp}.zip"
    manifest: dict[str, Any] = {
        "clinic_id": clinic.id,
        "clinic_name": clinic.name,
        "exported_at": datetime.now(UTC).isoformat(),
        "tables": {},
    }
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, stmt in _clinic_selections(clinic.id).items():
            rows = [_row_to_dict(obj) for obj in session.execute(stmt).scalars()]
            manifest["tables"][name] = len(rows)
            zf.writestr(f"{name}.json", json.dumps(rows, default=_json_default, indent=2, sort_keys=True))
        zf.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
    return target


def delete_clinic_rows(session: Session, clinic_id: int) -> dict[str, int]:
    """Delete the clinic's rows in dependency order; remittances are unlinked, never deleted.

    Every remittance claim, matched or not, loses the patient name and member id the payer echoed
    back (the matched ones are unlinked too); the unmatched ones are counted first, before the
    matched ones join them under ``claim_id IS NULL``.
    """
    claim_ids = select(Claim.id).where(Claim.clinic_id == clinic_id)
    line_ids = select(ClaimLine.id).where(ClaimLine.claim_id.in_(claim_ids))
    work_item_ids = select(WorkItem.id).where(WorkItem.claim_id.in_(claim_ids))
    rc_ids = select(RemittanceClaim.id).where(RemittanceClaim.claim_id.in_(claim_ids))
    deleted: dict[str, int] = {}

    def run(name: str, stmt: Any) -> None:
        deleted[name] = int(session.execute(stmt).rowcount or 0)

    run(
        "model_calls_unlinked",
        update(ModelCall).where(ModelCall.work_item_id.in_(work_item_ids)).values(work_item_id=None),
    )
    run("outcomes", delete(Outcome).where(Outcome.work_item_id.in_(work_item_ids)))
    run("actions", delete(ReviewAction).where(ReviewAction.work_item_id.in_(work_item_ids)))
    run("proposals", delete(Proposal).where(Proposal.work_item_id.in_(work_item_ids)))
    run("work_items", delete(WorkItem).where(WorkItem.id.in_(work_item_ids)))
    run(
        "payer_documents",
        delete(PayerDocument).where(
            or_(PayerDocument.claim_id.in_(claim_ids), PayerDocument.remittance_claim_id.in_(rc_ids))
        ),
    )
    run("note_excerpts", delete(NoteExcerpt).where(NoteExcerpt.claim_id.in_(claim_ids)))
    run(
        "remittance_lines_unlinked",
        update(RemittanceLine).where(RemittanceLine.claim_line_id.in_(line_ids)).values(claim_line_id=None),
    )
    # Drop the patient identifiers the payer echoed back in the CLPs (unmatched ones first, so their
    # count is their own), then unlink the matched CLPs.
    payer_spelling: dict[str, Any] = {
        "patient_first_name": None,
        "patient_last_name": None,
        "member_id": None,
    }
    run(
        "remittance_claims_unmatched_cleared",
        update(RemittanceClaim).where(RemittanceClaim.claim_id.is_(None)).values(**payer_spelling),
    )
    run(
        "remittance_claims_unlinked",
        update(RemittanceClaim)
        .where(RemittanceClaim.claim_id.in_(claim_ids))
        .values(claim_id=None, match_method=None, **payer_spelling),
    )
    run("claim_lines", delete(ClaimLine).where(ClaimLine.claim_id.in_(claim_ids)))
    session.execute(update(Claim).where(Claim.clinic_id == clinic_id).values(original_claim_id=None))
    run("claims", delete(Claim).where(Claim.clinic_id == clinic_id))
    run("patients", delete(Patient).where(Patient.clinic_id == clinic_id))
    session.flush()
    return deleted


def files_on_disk(settings: Settings) -> dict[str, list[Path]]:
    """The PHI-bearing files the app itself wrote: uploads (claims CSVs, notes, letters, 835s) under
    DATA_DIR/uploads and export packages (corrected claims, appeal letters, earlier hand-over zips)
    under EXPORT_DIR. The pilot serves one clinic, so every one of them belongs to it."""
    uploads_dir = settings.data_path / UPLOADS_DIR_NAME
    export_dir = settings.export_path
    uploads = sorted(p for p in uploads_dir.rglob("*") if p.is_file()) if uploads_dir.is_dir() else []
    exports = (
        sorted(p for p in export_dir.rglob(EXPORT_PACKAGE_GLOB) if p.is_file()) if export_dir.is_dir() else []
    )
    return {"uploads": uploads, "exports": exports}


def purge_files_on_disk(settings: Settings) -> dict[str, list[str]]:
    """Delete every file :func:`files_on_disk` lists; returns their names per directory.

    Raises CliError naming the first file that could not be removed: the purge then stops before
    any row is deleted and nothing is recorded as destroyed.
    """
    removed: dict[str, list[str]] = {"uploads": [], "exports": []}
    bases = {"uploads": settings.data_path / UPLOADS_DIR_NAME, "exports": settings.export_path}
    for kind, paths in files_on_disk(settings).items():
        for path in paths:
            try:
                path.unlink()
            except OSError as exc:
                raise CliError(
                    f"could not delete {path} ({type(exc).__name__}); nothing was purged from the "
                    "database and no destruction record was written - fix the file and re-run"
                ) from exc
            removed[kind].append(path.relative_to(bases[kind]).as_posix())
    return removed


def reclaim_deleted_pages(settings: Settings) -> bool:
    """On SQLite, VACUUM the database so the pages the purge freed are rewritten, not kept.

    A DELETE only marks pages free: their bytes (names, member ids, addresses) stay readable in the
    file until VACUUM rebuilds it, and VACUUM must run outside any transaction, so this is called
    after the purge committed. Returns False when the database is not SQLite (on PostgreSQL the DBA
    runs VACUUM FULL after a purge; see scripts/env-notes.md).
    """
    if not settings.is_sqlite:
        return False
    with get_engine().connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.exec_driver_sql("VACUUM")
    return True


def cmd_purge_clinic(args: argparse.Namespace) -> int:
    settings = get_settings()
    clinic_id = args.clinic_id if args.clinic_id is not None else args.clinic_id_positional
    if clinic_id is None:
        raise CliError("pass the clinic id: `purge-clinic <id>` (or --clinic-id <id>)")
    with session_scope() as session:
        clinic = session.get(Clinic, clinic_id)
        if clinic is None:
            raise CliError(f"clinic {clinic_id} not found")
        counts = clinic_counts(session, clinic.id)
        if not args.confirm:
            on_disk = files_on_disk(settings)
            _emit(
                {
                    "clinic_id": clinic.id,
                    "counts": counts,
                    "files_on_disk": {kind: len(paths) for kind, paths in on_disk.items()},
                    "deleted": False,
                    "hint": (
                        "re-run with --confirm to export these rows to a zip, then delete them together "
                        "with every uploaded file and export package on disk"
                    ),
                }
            )
            return 0
        # Files first: a file that cannot be removed stops the purge before any row is touched
        # (the transaction rolls back) and before the hand-over zip is written.
        files_deleted = purge_files_on_disk(settings)
        export_file = export_clinic(session, clinic, settings.export_path)
        deleted = delete_clinic_rows(session, clinic.id)
        audit.record(
            session,
            actor_type=ActorType.USER,
            actor_id=_cli_actor(),
            action="clinic_purged",
            entity_type="clinic",
            entity_id=clinic.id,
            reason=args.reason,
            details={
                "counts": deleted,
                "files_deleted": {kind: len(names) for kind, names in files_deleted.items()},
                "deleted_files": files_deleted,
                "export_file": export_file.name,
                "export_file_kept": True,
            },
        )
        clinic_id = clinic.id
    # The transaction is committed: the file's free pages can be rewritten now (and only now).
    vacuum_error: str | None = None
    try:
        vacuumed = reclaim_deleted_pages(settings)
    except Exception as exc:  # the rows are gone and recorded; only the file rewrite is missing
        vacuumed, vacuum_error = False, type(exc).__name__
    log.info(
        "clinic %s purged: %s; files deleted: %s; vacuumed: %s",
        clinic_id,
        {k: v for k, v in deleted.items()},
        {kind: len(names) for kind, names in files_deleted.items()},
        vacuumed,
    )
    report: dict[str, Any] = {
        "clinic_id": clinic_id,
        "counts": deleted,
        "files_deleted": files_deleted,
        "deleted": True,
        "vacuumed": vacuumed,
        "export_file": str(export_file),
        "export_file_kept": True,
        "note": HANDOVER_NOTE,
    }
    if vacuum_error is not None:
        report["vacuum_error"] = vacuum_error
    _emit(report)
    print(f"hand-over copy kept: {export_file}\n({HANDOVER_NOTE})", file=sys.stderr)
    if vacuum_error is not None:
        print(
            f"error: VACUUM failed with {vacuum_error}: the rows are deleted and the destruction is "
            "recorded, but their pages are still in the database file; run `VACUUM` on it and re-check",
            file=sys.stderr,
        )
        return EXIT_ERROR
    if not vacuumed:
        print(
            "note: not SQLite, so nothing was vacuumed here; have the DBA run VACUUM FULL so the deleted "
            "rows' pages are reclaimed (scripts/env-notes.md)",
            file=sys.stderr,
        )
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    settings = get_settings()
    # The refusal create_app makes, raised here as a user-facing error: inside uvicorn's import of
    # app.main it would reach the operator as a bare `RuntimeError` plus frames (messages are never
    # printed by the generic handler), with no word about SECRET_KEY or .env.
    if settings.app_env != "test" and is_placeholder_secret(settings.secret_key):
        raise CliError(PLACEHOLDER_SECRET_ERROR)
    # log_config=None: uvicorn must not install handlers of its own (its default dictConfig gives
    # `uvicorn` and `uvicorn.access` unmasked handlers with propagate=False, so "Exception in ASGI
    # application" tracebacks would carry the exception message). Its loggers then propagate to
    # the root handler configure_logging installed, which masks and folds every record.
    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=settings.log_level.lower(),
        log_config=None,
    )
    return 0


# --------------------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="denial-agent",
        description="Denial Recovery Agent: local MVP operations (imports, processing, users, audit).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init-db", help="create the database schema")
    p.add_argument("--migrate", action="store_true", help="run `alembic upgrade head` instead of create_all")
    p.set_defaults(func=cmd_init_db)

    p = sub.add_parser("create-clinic", help="create a clinic row and print its id")
    p.add_argument("--name", required=True)
    p.add_argument("--npi")
    p.add_argument("--tax-id", dest="tax_id")
    p.add_argument("--pm-system", dest="pm_system")
    p.set_defaults(func=cmd_create_clinic)

    p = sub.add_parser("create-user", help="create a reviewer/admin account and print its TOTP enrolment")
    p.add_argument("--email", required=True)
    p.add_argument("--role", choices=[str(r) for r in UserRole], default=str(UserRole.REVIEWER))
    p.add_argument("--password-stdin", action="store_true", help="read the password from stdin (first line)")
    p.set_defaults(func=cmd_create_user)

    p = sub.add_parser("import-835", help="import an X12 835 remittance file and process its denials")
    p.add_argument("file")
    p.set_defaults(func=cmd_import_835)

    for name, handler, help_text in (
        ("import-claims", cmd_import_claims, "import a claim-export CSV"),
        ("import-notes", cmd_import_notes, "import notes.csv (quoted note excerpts)"),
        ("import-documents", cmd_import_documents, "import payer_documents.csv (letters, portal text)"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("file")
        p.add_argument("--clinic-id", dest="clinic_id", type=int, default=None)
        p.set_defaults(func=handler)

    p = sub.add_parser("process", help="create work items for every pending denial")
    p.set_defaults(func=cmd_process)

    p = sub.add_parser("generate-synthetic", help="write a synthetic claims/835 batch")
    p.add_argument("--out", required=True)
    p.add_argument("--seed", type=int, default=20261028)
    p.add_argument("--claims", type=int, default=200)
    p.set_defaults(func=cmd_generate_synthetic)

    p = sub.add_parser("verify-audit", help="verify the audit log hash chain (exit 1 when broken)")
    p.set_defaults(func=cmd_verify_audit)

    p = sub.add_parser(
        "list-payers", help="list every payer with its filing, corrected-claim and appeal windows"
    )
    p.set_defaults(func=cmd_list_payers)

    p = sub.add_parser(
        "set-payer",
        help="set a payer's timely-filing, corrected-claim and/or appeal window in days (audited)",
    )
    p.add_argument(
        "--payer-id", dest="payer_id", required=True, help="X12 payer identifier (see list-payers)"
    )
    p.add_argument("--timely-filing-days", dest="timely_filing_days", type=int, default=None)
    p.add_argument("--corrected-claim-days", dest="corrected_claim_days", type=int, default=None)
    p.add_argument("--appeal-days", dest="appeal_days", type=int, default=None)
    p.set_defaults(func=cmd_set_payer)

    p = sub.add_parser(
        "purge-clinic",
        help=(
            "export then delete a clinic's data (pilot termination): database rows plus every "
            "uploaded file and export package on disk; the export zip is kept as the hand-over copy"
        ),
    )
    p.add_argument(
        "clinic_id_positional", metavar="CLINIC_ID", nargs="?", type=int, default=None, help="clinic id"
    )
    p.add_argument(
        "--clinic-id", dest="clinic_id", type=int, default=None, help="clinic id (alternative form)"
    )
    p.add_argument(
        "--confirm", action="store_true", help="actually export and delete; otherwise print counts"
    )
    p.add_argument("--reason", default=None, help="short reason recorded in the audit log")
    p.set_defaults(func=cmd_purge_clinic)

    p = sub.add_parser("serve", help="run the web app with uvicorn")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--reload", action="store_true")
    p.set_defaults(func=cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _configure_logging_once()
    try:
        return int(args.func(args) or EXIT_OK)
    except CliError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("aborted", file=sys.stderr)
        return EXIT_INTERRUPTED
    except Exception as exc:
        # The message may quote imported data; the class name and the frames are enough to locate it.
        name = type(exc).__name__
        log.error("%s failed: %s", args.command, name)
        print(f"error: {args.command} failed with {name}\n{format_frames(exc)}", file=sys.stderr)
        return EXIT_UNEXPECTED


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
