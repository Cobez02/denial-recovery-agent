"""`denial-agent` command line (milestone M1 operations).

Subcommands: init-db, create-clinic, create-user, import-835, import-claims, import-notes,
import-documents, process, generate-synthetic, verify-audit, purge-clinic, serve.

Output policy: stdout carries ids, counts, statuses and file names only. Passwords are never
printed. The TOTP provisioning URI (which embeds the shared secret) is printed exactly once, on
purpose, so the operator can enrol an authenticator app; the ASCII QR code is the same data.
An unexpected exception ends the command with exit code 2, its class name and the traceback
frames only: exception messages can quote imported data and are never written to stderr or logs.

Exit codes: 0 success; 1 a user-facing error, a failed import batch, a failed pipeline run or a
broken audit chain; 2 an unexpected exception; 130 interrupted.
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
from app.config import PROJECT_ROOT, get_settings
from app.db import init_db, session_scope
from app.enums import ActorType, UserRole
from app.jobs.runner import format_frames
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

log = logging.getLogger(__name__)

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
    """Non-zero when the import batch failed or the pipeline run failed (the JSON says which)."""
    if result.get("status") == "failed" or result.get("pipeline") == "failed":
        return EXIT_ERROR
    return EXIT_OK


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


# ---- purge-clinic -------------------------------------------------------------------------


def _clinic_selections(clinic_id: int) -> dict[str, Select[Any]]:
    """Row selections for everything that belongs to a clinic, keyed by table name."""
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
    """Delete the clinic's rows in dependency order; remittances are unlinked, never deleted."""
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
    # Unlink the CLPs and drop the patient identifiers the payer echoed back in them.
    run(
        "remittance_claims_unlinked",
        update(RemittanceClaim)
        .where(RemittanceClaim.claim_id.in_(claim_ids))
        .values(
            claim_id=None, match_method=None, patient_first_name=None, patient_last_name=None, member_id=None
        ),
    )
    run("claim_lines", delete(ClaimLine).where(ClaimLine.claim_id.in_(claim_ids)))
    session.execute(update(Claim).where(Claim.clinic_id == clinic_id).values(original_claim_id=None))
    run("claims", delete(Claim).where(Claim.clinic_id == clinic_id))
    run("patients", delete(Patient).where(Patient.clinic_id == clinic_id))
    session.flush()
    return deleted


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
            _emit(
                {
                    "clinic_id": clinic.id,
                    "counts": counts,
                    "deleted": False,
                    "hint": "re-run with --confirm to export these rows to a zip and then delete them",
                }
            )
            return 0
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
            details={"counts": deleted, "export_file": export_file.name},
        )
        clinic_id = clinic.id
    log.info("clinic %s purged: %s", clinic_id, {k: v for k, v in deleted.items()})
    _emit({"clinic_id": clinic_id, "counts": deleted, "deleted": True, "export_file": str(export_file)})
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=settings.log_level.lower(),
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

    p = sub.add_parser("purge-clinic", help="export then delete a clinic's data (pilot termination)")
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
