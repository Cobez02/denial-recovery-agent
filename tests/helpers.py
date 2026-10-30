"""Helpers shared by the cross-module tests (pipeline, classifier set, redaction, PHI in logs, injection).

Everything here runs against the committed synthetic fixture set ``synthetic/fixtures/labeled_set_v2``
(``generate(seed=20261028, n_claims=200)``, the chiropractic profile) with the stub model client;
nothing is real data.

- :func:`load_fixture_batch` imports claims, notes, payer documents and every ``remit_*.835`` from a
  directory, in that order, committing after each file.
- :func:`run_pipeline` wraps ``app.pipeline.process_pending`` with a commit.
- :func:`identifiers_for_batch` lists every patient identifier the fixture carries (names in several
  spellings, member ids, dates of birth in several spellings, addresses, account numbers) and
  :func:`scan_text_for_identifiers` finds any of them in a text. These two are deliberately
  independent of ``app.redact`` so the redaction and PHI-in-logs tests do not trust the code under test.
- :func:`fixture_run` gives a module-scoped fixture one private database with the batch imported and
  processed, so read-only tests share a single run instead of repeating the import per function.
- :func:`http_run` gives a module-scoped fixture one private database, the FastAPI app and a
  TestClient logged in through the real password + TOTP flow, with every model payload and every
  log record captured for the whole run (the end-to-end synthetic test drives the loop over HTTP).
"""

from __future__ import annotations

import csv
import json
import logging
import re
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Claim, ImportBatch, WorkItem
from app.schemas import CLAIM_EXPORT_COLUMNS, RenderedPrompt

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = PROJECT_ROOT / "synthetic" / "fixtures" / "labeled_set_v2"

CLAIMS_FILE = "claims_export.csv"
NOTES_FILE = "notes.csv"
DOCUMENTS_FILE = "payer_documents.csv"
LABELS_FILE = "labels.json"
MANIFEST_FILE = "manifest.json"
REMIT_GLOB = "remit_*.835"

# The review pages: the hidden CSRF field every form carries, one queue row, the HTMX header that
# stands in for the token on same-origin requests (see app/auth/deps.py).
CSRF_RE = re.compile(r'name="_csrf" value="([^"]+)"')
QUEUE_ROW_RE = re.compile(r'<tr class="queue-row">\s*<td><a href="/items/(\d+)"')
HX_HEADERS: dict[str, str] = {"HX-Request": "true"}
REVIEWER_EMAIL = "reviewer@example.test"
REVIEWER_PASSWORD = "correct horse battery staple 42!"

# Identifier kinds reported by identifiers_for_batch / scan_text_for_identifiers.
KIND_NAME = "PATIENT_NAME"
KIND_MEMBER_ID = "MEMBER_ID"
KIND_DOB = "DOB"
KIND_ADDRESS = "ADDRESS"
KIND_ACCOUNT = "ACCOUNT"
# Names and addresses are matched as whole words; ids and dates as plain substrings.
WORD_BOUNDED_KINDS: frozenset[str] = frozenset({KIND_NAME, KIND_ADDRESS})
MIN_IDENTIFIER_LENGTH = 3
PREVIEW_VISIBLE_CHARS = 2
PREVIEW_MAX_LENGTH = 32
MIN_PHRASE_LENGTH = 12

MONTH_NAMES: tuple[str, ...] = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

_WS = re.compile(r"\s+")
_bounded_cache: dict[str, re.Pattern[str]] = {}


# --------------------------------------------------------------------------------------
# Fixture files
# --------------------------------------------------------------------------------------


def load_labels(batch_dir: str | Path = FIXTURE_DIR) -> list[dict[str, Any]]:
    """The generator's ground truth: one dict per denied claim (see synthetic/generate.py)."""
    return json.loads((Path(batch_dir) / LABELS_FILE).read_text(encoding="utf-8"))


def load_manifest(batch_dir: str | Path = FIXTURE_DIR) -> dict[str, Any]:
    return json.loads((Path(batch_dir) / MANIFEST_FILE).read_text(encoding="utf-8"))


def batch_as_of(batch_dir: str | Path = FIXTURE_DIR) -> date:
    """The batch's "today" (``as_of`` in manifest.json): the date the pipeline runs with in tests."""
    return date.fromisoformat(load_manifest(batch_dir)["as_of"])


AS_OF: date = batch_as_of()


def read_claim_rows(batch_dir: str | Path = FIXTURE_DIR) -> list[dict[str, str]]:
    """Rows of claims_export.csv; the header must carry every column of the claim-export contract."""
    with open(Path(batch_dir) / CLAIMS_FILE, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = [column for column in CLAIM_EXPORT_COLUMNS if column not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"claims export lacks columns: {', '.join(missing)}")
        return list(reader)


def remit_files(batch_dir: str | Path = FIXTURE_DIR) -> list[Path]:
    return sorted(Path(batch_dir).glob(REMIT_GLOB))


# --------------------------------------------------------------------------------------
# Import and processing
# --------------------------------------------------------------------------------------


def batch_summary(batch: ImportBatch) -> dict[str, Any]:
    """The batch's counts plus its ``status`` and ``batch_id`` (count keys never use those names)."""
    return {"batch_id": batch.id, "status": str(batch.status), **dict(batch.counts or {})}


def load_fixture_batch(
    session: Session,
    settings: Any,
    clinic: Any,
    batch_dir: str | Path = FIXTURE_DIR,
    *,
    created_by: str = "tests",
) -> dict[str, dict[str, Any]]:
    """Import claims_export.csv, notes.csv, payer_documents.csv, then every remit_*.835, in that order.

    Commits after each file. Returns ``{kind_or_file: counts}`` where the keys are ``claims``,
    ``notes``, ``payer_documents`` and each remittance file name, and every value is the batch's
    counts plus ``status`` and ``batch_id`` (see :func:`batch_summary`).
    """
    from app.ingest.claims_import import import_claims_csv, import_notes_csv, import_payer_documents_csv
    from app.ingest.remit_import import import_835

    directory = Path(batch_dir)
    clinic_id = int(clinic.id if clinic is not None else settings.clinic_id)
    results: dict[str, dict[str, Any]] = {}
    for kind, file_name, importer in (
        ("claims", CLAIMS_FILE, import_claims_csv),
        ("notes", NOTES_FILE, import_notes_csv),
        ("payer_documents", DOCUMENTS_FILE, import_payer_documents_csv),
    ):
        batch = importer(session, directory / file_name, clinic_id=clinic_id, created_by=created_by)
        session.commit()
        results[kind] = batch_summary(batch)
    for remit in remit_files(directory):
        batch = import_835(session, remit, file_name=remit.name, created_by=created_by)
        session.commit()
        results[remit.name] = batch_summary(batch)
    return results


def run_pipeline(session: Session, today: date = AS_OF, **kwargs: Any) -> list[WorkItem]:
    """``app.pipeline.process_pending`` followed by a commit; returns the work items it created."""
    from app.pipeline import process_pending

    created = process_pending(session, today=today, **kwargs)
    session.commit()
    return created


def work_items_by_ccn(session: Session) -> dict[str, WorkItem]:
    """Every work item keyed by its claim's control number (ordered by work item id)."""
    stmt = (
        select(WorkItem, Claim.claim_control_number)
        .join(Claim, WorkItem.claim_id == Claim.id)
        .order_by(WorkItem.id)
    )
    return {ccn: work_item for work_item, ccn in session.execute(stmt)}


def labels_by_ccn(labels: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {label["claim_control_number"]: label for label in labels}


# --------------------------------------------------------------------------------------
# One shared run for a whole test module
# --------------------------------------------------------------------------------------


@dataclass
class FixtureRun:
    """A fixture batch imported and processed in a private database (see :func:`fixture_run`)."""

    session: Session
    settings: Any
    clinic: Any
    batch_dir: Path
    today: date
    imports: dict[str, dict[str, Any]]
    created: list[WorkItem]
    labels: list[dict[str, Any]]
    label_by_ccn: dict[str, dict[str, Any]]
    by_ccn: dict[str, WorkItem]
    payloads: list[RenderedPrompt]


@contextmanager
def fixture_run(
    tmp_dir: str | Path,
    *,
    batch_dir: str | Path = FIXTURE_DIR,
    capture_payloads: bool = False,
    process: bool = True,
) -> Iterator[FixtureRun]:
    """Import (and process) the batch once in a database of its own, for module-scoped fixtures.

    Wires the environment, settings, engine, session and clinic exactly like the ``settings`` /
    ``engine`` / ``session`` / ``clinic`` fixtures in tests/conftest.py, then runs
    :func:`load_fixture_batch` and :func:`run_pipeline`. With ``capture_payloads`` every
    ``RenderedPrompt`` the stub client received during the run is kept in ``payloads`` (the patch
    is undone before the run is handed out). Tests in a module that uses this must not also use
    the function-scoped conftest fixtures: their teardown disposes the shared engine.
    """
    from app.config import get_settings, reset_settings_cache
    from app.db import configure_engine, dispose_engine, init_db, make_engine, session_factory
    from app.llm import clients
    from app.models import Clinic
    from tests.conftest import set_test_env  # the one definition of the test environment

    set_test_env(Path(tmp_dir))
    reset_settings_cache()
    dispose_engine()
    settings = get_settings()
    engine = make_engine(settings.database_url)
    configure_engine(engine)
    init_db(engine)
    session = session_factory()()
    payloads: list[RenderedPrompt] = []
    try:
        clinic = Clinic(
            name="Test Chiropractic Clinic", npi="1234567893", tax_id="12-3456789", pm_system="TestPM"
        )
        session.add(clinic)
        session.commit()
        settings.clinic_id = clinic.id
        today = batch_as_of(batch_dir)
        with pytest.MonkeyPatch.context() as monkeypatch:
            if capture_payloads:
                original = clients.StubClient.complete

                def _capture(self: Any, rendered: RenderedPrompt) -> Any:
                    payloads.append(rendered)
                    return original(self, rendered)

                monkeypatch.setattr(clients.StubClient, "complete", _capture)
            imports = load_fixture_batch(session, settings, clinic, batch_dir)
            created = run_pipeline(session, today=today) if process else []
        labels = load_labels(batch_dir)
        yield FixtureRun(
            session=session,
            settings=settings,
            clinic=clinic,
            batch_dir=Path(batch_dir),
            today=today,
            imports=imports,
            created=created,
            labels=labels,
            label_by_ccn=labels_by_ccn(labels),
            by_ccn=work_items_by_ccn(session),
            payloads=payloads,
        )
    finally:
        session.rollback()
        session.close()
        dispose_engine()
        engine.dispose()
        reset_settings_cache()


# --------------------------------------------------------------------------------------
# One shared HTTP run for a whole test module
# --------------------------------------------------------------------------------------


class LogCapture(logging.Handler):
    """Keeps every record that reaches the root logger, as the application's own handlers see it.

    The app's ``PHIMaskingFilter`` is attached to this handler too (``configure_logging`` does that
    for every foreign root handler), so the records are the masked ones the real handlers write.
    """

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    @property
    def messages(self) -> list[str]:
        return [record.getMessage() for record in self.records]


@dataclass
class HttpRun:
    """A private database, the app and a logged-in client for a whole module (see :func:`http_run`)."""

    session: Session
    settings: Any
    clinic: Any
    reviewer: Any
    reviewer_password: str
    app: Any
    client: Any
    batch_dir: Path
    today: date
    labels: list[dict[str, Any]]
    label_by_ccn: dict[str, dict[str, Any]]
    payloads: list[RenderedPrompt] = field(default_factory=list)
    logs: LogCapture = field(default_factory=LogCapture)


def csrf_token(client: Any) -> str:
    """The session's CSRF token, read from the queue page exactly as a browser would."""
    page = client.get("/queue")
    assert page.status_code == 200, page.status_code
    match = CSRF_RE.search(page.text)
    assert match is not None, "the queue page carries no CSRF field"
    return match.group(1)


def queue_item_ids(html: str) -> list[int]:
    """Work item ids in the order the queue table lists them."""
    return [int(x) for x in QUEUE_ROW_RE.findall(html)]


@contextmanager
def http_run(
    tmp_dir: str | Path,
    *,
    batch_dir: str | Path = FIXTURE_DIR,
    reviewer_email: str = REVIEWER_EMAIL,
    reviewer_password: str = REVIEWER_PASSWORD,
) -> Iterator[HttpRun]:
    """A database of its own, one clinic, one reviewer and a TestClient logged in through MFA.

    Wires the environment, settings, engine and session like the conftest fixtures, creates the
    reviewer through ``app.auth.service`` and logs the client in with POST /login and POST
    /login/mfa (a ``login_success`` audit event, as in production). Nothing is imported: the
    caller drives the loop over HTTP. For the whole run every ``RenderedPrompt`` the stub client
    receives lands in ``payloads`` and every log record the root logger receives (``app`` loggers
    at DEBUG, the rest at the configured level) in ``logs``; both patches are undone on exit.
    Tests in a module that uses this must not also use the function-scoped conftest fixtures.
    """
    import pyotp
    from fastapi.testclient import TestClient

    from app.auth.service import create_user
    from app.config import get_settings, reset_settings_cache
    from app.db import configure_engine, dispose_engine, init_db, make_engine, session_factory
    from app.llm import clients
    from app.logging_config import PHIMaskingFilter
    from app.models import Clinic
    from tests.conftest import set_test_env  # the one definition of the test environment

    set_test_env(Path(tmp_dir))
    # app.main builds the application at import and refuses a placeholder SECRET_KEY outside
    # APP_ENV=test, so it is imported only once the test environment is in place (this module
    # may be the first one in a run to import it).
    from app.main import create_app

    reset_settings_cache()
    dispose_engine()
    settings = get_settings()
    engine = make_engine(settings.database_url)
    configure_engine(engine)
    init_db(engine)
    session = session_factory()()

    root = logging.getLogger()
    app_logger = logging.getLogger("app")
    handlers_before = list(root.handlers)
    root_level, app_level = root.level, app_logger.level
    capture = LogCapture()
    capture.addFilter(PHIMaskingFilter())
    root.addHandler(capture)
    monkeypatch = pytest.MonkeyPatch()
    payloads: list[RenderedPrompt] = []
    client: Any = None
    try:
        original = clients.StubClient.complete

        def _capture(self: Any, rendered: RenderedPrompt) -> Any:
            payloads.append(rendered)
            return original(self, rendered)

        monkeypatch.setattr(clients.StubClient, "complete", _capture)

        clinic = Clinic(
            name="Test Chiropractic Clinic", npi="1234567893", tax_id="12-3456789", pm_system="TestPM"
        )
        session.add(clinic)
        session.commit()
        settings.clinic_id = clinic.id
        reviewer = create_user(session, email=reviewer_email, password=reviewer_password, role="reviewer")
        session.commit()

        app = create_app()  # configures logging: root at LOG_LEVEL, masking filter on every handler
        app_logger.setLevel(logging.DEBUG)
        client = TestClient(app, base_url="http://testserver", follow_redirects=False)
        response = client.post("/login", data={"email": reviewer.email, "password": reviewer_password})
        assert response.status_code in (200, 302, 303), response.status_code
        response = client.post("/login/mfa", data={"code": pyotp.TOTP(reviewer.totp_secret).now()})
        assert response.status_code in (200, 302, 303), response.status_code
        assert client.get("/queue").status_code == 200, "login through password + TOTP failed"

        labels = load_labels(batch_dir)
        yield HttpRun(
            session=session,
            settings=settings,
            clinic=clinic,
            reviewer=reviewer,
            reviewer_password=reviewer_password,
            app=app,
            client=client,
            batch_dir=Path(batch_dir),
            today=batch_as_of(batch_dir),
            labels=labels,
            label_by_ccn=labels_by_ccn(labels),
            payloads=payloads,
            logs=capture,
        )
    finally:
        monkeypatch.undo()
        if client is not None:
            client.close()
        session.rollback()
        session.close()
        dispose_engine()
        engine.dispose()
        reset_settings_cache()
        # Leave the root logger as it was found: drop the capture handler and the stderr handler
        # configure_logging added during this run; never re-add a handler pytest already removed.
        for handler in list(root.handlers):
            if handler is capture or (
                handler not in handlers_before and getattr(handler, "_denial_agent_handler", False)
            ):
                root.removeHandler(handler)
        root.setLevel(root_level)
        app_logger.setLevel(app_level)


# --------------------------------------------------------------------------------------
# Identifiers and scanning
# --------------------------------------------------------------------------------------


def payer_letterheads(profile_name: str | None = None) -> set[str]:
    """The synthetic payers' own city/state/zip lines as the letter templates print them (lower case).

    Some synthetic payers are located in towns synthetic patients also live in, so a whole-batch
    address scan of a payload would otherwise report a payer's letterhead as a patient address. The
    default profile is the one named in the fixture manifest.
    """
    from synthetic.profile import load_profile

    letterheads: set[str] = set()
    for payer in load_profile(profile_name or load_manifest()["profile"]).payers:
        address = payer.address or {}
        city, state, zip_code = address.get("city", ""), address.get("state", ""), address.get("zip", "")
        if city and zip_code:
            letterheads.add(f"{city}, {state} {zip_code}".lower())
            letterheads.add(f"{city} {state} {zip_code}".lower())
    return letterheads


def dob_spellings(dob: date) -> list[str]:
    """YYYY-MM-DD, MM/DD/YYYY, M/D/YYYY, MMDDYYYY and 'Month D, YYYY'."""
    return [
        dob.isoformat(),
        f"{dob.month:02d}/{dob.day:02d}/{dob.year}",
        f"{dob.month}/{dob.day}/{dob.year}",
        f"{dob.month:02d}{dob.day:02d}{dob.year}",
        f"{MONTH_NAMES[dob.month - 1]} {dob.day}, {dob.year}",
    ]


def identifiers_for_batch(batch_dir: str | Path = FIXTURE_DIR) -> list[tuple[str, str]]:
    """Every (kind, value) a patient in claims_export.csv could be recognised by.

    Names: first, last, 'First Last' and 'Last, First'. Member id. Date of birth in five spellings.
    Address line 1 and the city + state + zip combinations. Account number. Values shorter than
    three characters are dropped; duplicates (case-insensitive) are kept once, in first-seen order.
    """
    identifiers: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, value: str | None) -> None:
        text = _WS.sub(" ", (value or "").strip())
        if len(text) < MIN_IDENTIFIER_LENGTH:
            return
        key = (kind, text.lower())
        if key in seen:
            return
        seen.add(key)
        identifiers.append((kind, text))

    for row in read_claim_rows(batch_dir):
        first = (row.get("patient_first_name") or "").strip()
        last = (row.get("patient_last_name") or "").strip()
        for variant in (first, last, f"{first} {last}", f"{last}, {first}"):
            add(KIND_NAME, variant)
        add(KIND_MEMBER_ID, row.get("member_id"))
        dob_text = (row.get("patient_dob") or "").strip()
        if dob_text:
            for spelling in dob_spellings(date.fromisoformat(dob_text)):
                add(KIND_DOB, spelling)
        add(KIND_ADDRESS, row.get("patient_address_line1"))
        city = (row.get("patient_city") or "").strip()
        state = (row.get("patient_state") or "").strip()
        zip_code = (row.get("patient_zip") or "").strip()
        if city and zip_code:
            if state:
                add(KIND_ADDRESS, f"{city}, {state} {zip_code}")
                add(KIND_ADDRESS, f"{city} {state} {zip_code}")
            else:
                add(KIND_ADDRESS, f"{city} {zip_code}")
        add(KIND_ACCOUNT, row.get("patient_account_number"))
    return identifiers


def mask_preview(value: str) -> str:
    """First two characters, the rest masked, capped in length: never a whole identifier in a report."""
    visible = value[:PREVIEW_VISIBLE_CHARS]
    return (visible + "*" * max(len(value) - PREVIEW_VISIBLE_CHARS, 0))[:PREVIEW_MAX_LENGTH]


def _bounded(needle: str) -> re.Pattern[str]:
    pattern = _bounded_cache.get(needle)
    if pattern is None:
        parts = [re.escape(part) for part in needle.split(" ")]
        pattern = re.compile(r"(?<![a-z0-9])" + r"\s+".join(parts) + r"(?![a-z0-9])")
        _bounded_cache[needle] = pattern
    return pattern


def scan_text_for_identifiers(
    text: str | None, identifiers: Sequence[tuple[str, str]]
) -> list[tuple[str, str]]:
    """(kind, masked preview) for every identifier present in ``text``.

    Case-insensitive; whitespace runs are collapsed first. Names and addresses must match as whole
    words (so 'Orla' never matches 'Orlando'); ids and dates match as substrings. Values shorter
    than three characters are skipped. Each identifier is reported once.
    """
    if not text:
        return []
    lowered = _WS.sub(" ", text).lower()
    findings: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for kind, value in identifiers:
        needle = _WS.sub(" ", value.strip()).lower()
        if len(needle) < MIN_IDENTIFIER_LENGTH or needle not in lowered:
            continue
        if kind in WORD_BOUNDED_KINDS and _bounded(needle).search(lowered) is None:
            continue
        key = (kind, needle)
        if key in seen:
            continue
        seen.add(key)
        findings.append((kind, mask_preview(value)))
    return findings


def scan_values(
    values: Iterable[tuple[str, Any]], identifiers: Sequence[tuple[str, str]]
) -> list[tuple[str, str, str]]:
    """Scan several (label, value) pairs; non-strings are JSON-encoded first. Returns
    (label, kind, preview) triples."""
    out: list[tuple[str, str, str]] = []
    for label, value in values:
        text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
        for kind, preview in scan_text_for_identifiers(text, identifiers):
            out.append((label, kind, preview))
    return out


# --------------------------------------------------------------------------------------
# Planted instructions
# --------------------------------------------------------------------------------------


def planted_phrases_by_style() -> dict[str, list[str]]:
    """``{style: phrases}`` for every style of planted instruction the synthetic generator uses.

    Taken from ``synthetic.generate.INJECTION_PAYLOADS`` so the injection tests and the generator
    never drift apart. Every payload line is kept whole and also split at sentence punctuation so
    a partially quoted instruction is caught too; pieces shorter than twelve characters are dropped.
    """
    from synthetic.generate import INJECTION_PAYLOADS

    by_style: dict[str, list[str]] = {}
    for style, payload in INJECTION_PAYLOADS:
        phrases = by_style.setdefault(style, [])
        for line in payload.splitlines():
            candidates = [line.strip(), *re.split(r"(?<=[.;:])\s+", line.strip())]
            for candidate in candidates:
                piece = candidate.strip()
                if len(piece) >= MIN_PHRASE_LENGTH and piece not in phrases:
                    phrases.append(piece)
    return by_style


def planted_phrases() -> list[str]:
    """Every sentence the synthetic generator plants in payer letters, plus its signatures
    (``INJECTION_SIGNATURES`` first, then the phrases of every style in generator order)."""
    from synthetic.generate import INJECTION_SIGNATURES

    phrases: list[str] = list(INJECTION_SIGNATURES)
    for style_phrases in planted_phrases_by_style().values():
        for piece in style_phrases:
            if piece not in phrases:
                phrases.append(piece)
    return phrases


def find_planted_phrases(text: str | None, phrases: Sequence[str]) -> list[str]:
    """The planted phrases that occur in ``text`` (case-insensitive substring match)."""
    if not text:
        return []
    lowered = text.lower()
    return [phrase for phrase in phrases if phrase.lower() in lowered]


__all__ = [
    "AS_OF",
    "CLAIMS_FILE",
    "CSRF_RE",
    "DOCUMENTS_FILE",
    "FIXTURE_DIR",
    "FixtureRun",
    "HX_HEADERS",
    "HttpRun",
    "KIND_ACCOUNT",
    "KIND_ADDRESS",
    "KIND_DOB",
    "KIND_MEMBER_ID",
    "KIND_NAME",
    "LABELS_FILE",
    "LogCapture",
    "MANIFEST_FILE",
    "NOTES_FILE",
    "QUEUE_ROW_RE",
    "REVIEWER_EMAIL",
    "REVIEWER_PASSWORD",
    "WORD_BOUNDED_KINDS",
    "batch_as_of",
    "batch_summary",
    "csrf_token",
    "dob_spellings",
    "find_planted_phrases",
    "fixture_run",
    "http_run",
    "identifiers_for_batch",
    "labels_by_ccn",
    "load_fixture_batch",
    "load_labels",
    "load_manifest",
    "mask_preview",
    "payer_letterheads",
    "planted_phrases",
    "planted_phrases_by_style",
    "queue_item_ids",
    "read_claim_rows",
    "remit_files",
    "run_pipeline",
    "scan_text_for_identifiers",
    "scan_values",
    "work_items_by_ccn",
]
