"""PHI in logs (plan testing table; docs/SPEC.md section 15).

With the application's logging configured at DEBUG and every record captured, a full run over the
committed synthetic batch (import, pipeline, a deliberately failing import and two tracebacks logged
with ``exc_info``) must leave zero synthetic identifiers in the log output, and the failed batch's
error and audit event must name positions only. The masking filter is defence in depth, not the
first line: it records every substitution it makes (``app.logging_config.MASK_HITS``) and no
``app.*`` logger may have needed one. The test fails, never skips, on a finding, and it cannot pass
vacuously: it demands more than fifty captured records and first proves, on a probe record, that
the hit tracking and the masking work.

The two tests at the bottom cover the web process: the ``app.main`` catch-all handler and the
``uvicorn.error`` record uvicorn writes for the same unhandled exception (``denial-agent serve``
hands uvicorn ``log_config=None`` so that record reaches the masked root handler).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import select

from app.enums import ImportStatus
from app.ingest.parse_835 import parse_835
from app.ingest.remit_import import import_835
from app.ingest.x12 import X12Error
from app.logging_config import MASK_HITS, PHIMaskingFilter, configure_logging, reset_mask_hits
from app.models import AuditLog
from tests import helpers

MIN_RECORDS = 50
MEMBER_ID_SHAPE = re.compile(r"\b[A-Z]{1,4}\d{6,}\b")
PROBE_LOGGER = "tests.probe"  # the deliberate exc_info and masking probes: never an app logger
SECRET = "member id MBR-SECRET-1 for Jane Synthetic DOB 1980-01-02"
UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "uvicorn.asgi")

# Every app.* logger is held to zero masking substitutions (app.playbooks logs days to the deadline,
# never the date): the filter is the backstop, not the plan. Nothing may be added here to make the
# test pass; the logging call is what gets fixed.
KNOWN_MASK_OFFENDERS: frozenset[tuple[str, str]] = frozenset()


def _malformed_835(row: dict[str, str]) -> str:
    """An 835 that fails in the envelope reader and quotes a patient on every element."""
    return (
        f"ISA*00*{row['patient_first_name']} {row['patient_last_name']}*DOB {row['patient_dob']}"
        f"*MEMBER {row['member_id']}*{row['patient_address_line1']}~GS*HP*X*Y~ST*835*0001~"
    )


@contextmanager
def app_logging(level: str, caplog) -> Iterator[None]:
    """``configure_logging`` exactly as the app does, leaving the root logger as it was found.

    The capture handler pytest attached to the root logger gets the masking filter like every other
    foreign root handler, so the captured records are the ones the real handlers write.
    """
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_filters = {handler: list(handler.filters) for handler in saved_handlers}
    saved_level = root.level
    caplog.set_level(logging.DEBUG)
    configure_logging(level)
    if not any(isinstance(f, PHIMaskingFilter) for f in caplog.handler.filters):
        caplog.handler.addFilter(PHIMaskingFilter())
    try:
        yield
    finally:
        for handler in list(root.handlers):
            if handler not in saved_handlers:
                root.removeHandler(handler)
        for handler in saved_handlers:
            if handler not in root.handlers:
                root.addHandler(handler)
            handler.filters = saved_filters[handler]
        root.setLevel(saved_level)


@contextmanager
def _uvicorn_loggers_restored() -> Iterator[None]:
    saved = {
        name: (list(lg.handlers), lg.propagate, lg.level)
        for name, lg in ((n, logging.getLogger(n)) for n in UVICORN_LOGGERS)
    }
    try:
        yield
    finally:
        for name, (handlers, propagate, level) in saved.items():
            logger = logging.getLogger(name)
            logger.handlers = handlers
            logger.propagate = propagate
            logger.setLevel(level)


def _app_mask_hits() -> list[tuple[str, str]]:
    return sorted({(name, label) for name, label in MASK_HITS if name == "app" or name.startswith("app.")})


def test_no_identifier_reaches_the_logs(session, settings, clinic, caplog):
    row = helpers.read_claim_rows()[0]
    identifiers = helpers.identifiers_for_batch()
    member_id = next(value for kind, value in identifiers if kind == helpers.KIND_MEMBER_ID)
    probe = logging.getLogger(PROBE_LOGGER)
    with app_logging("DEBUG", caplog):
        reset_mask_hits()
        # Control: the filter masks a member id and records the hit, so the assertions below bite.
        probe.info("masking probe for member id %s", member_id)
        assert (PROBE_LOGGER, "[ID]") in MASK_HITS

        helpers.load_fixture_batch(session, settings, clinic)
        created = helpers.run_pipeline(session)
        assert len(created) == len(helpers.load_labels())

        # A failing import: the batch fails and its error names positions, never values.
        batch = import_835(session, _malformed_835(row), file_name="malformed.835", created_by="tests")
        session.commit()
        assert batch.status == str(ImportStatus.FAILED) and batch.error
        assert batch.error.startswith("parse error")

        # Tracebacks logged with exc_info are folded to frames + class name by the masking filter.
        try:
            parse_835(_malformed_835(row))
        except X12Error:
            probe.exception("835 import failed for batch %s", batch.id)
        try:
            raise ValueError(
                f"rejected row for {row['patient_first_name']} {row['patient_last_name']} "
                f"born {row['patient_dob']} member {row['member_id']}"
            )
        except ValueError:
            probe.exception("claims import failed for batch %s", batch.id)
        hits = list(MASK_HITS)

    records = list(caplog.records)
    assert len(records) > MIN_RECORDS, len(records)
    assert {record.name.split(".")[0] for record in records} >= {"app"}

    # The failed batch and its audit event name positions and counts, never the values on the line.
    assert not helpers.scan_text_for_identifiers(batch.error, identifiers)
    failed = session.execute(select(AuditLog).where(AuditLog.action == "import_failed")).scalars().all()
    assert len(failed) == 1 and failed[0].entity_id == batch.id
    assert not helpers.scan_values(
        [("reason", failed[0].reason), ("details", failed[0].details)], identifiers
    )

    messages = [record.getMessage() for record in records]
    findings = helpers.scan_text_for_identifiers("\n".join(messages), identifiers)
    findings += helpers.scan_text_for_identifiers(caplog.text, identifiers)
    if findings:
        where = [
            (record.name, kind, preview)
            for record, message in zip(records, messages, strict=True)
            for kind, preview in helpers.scan_text_for_identifiers(message, identifiers)
        ]
        raise AssertionError(f"{len(findings)} identifier(s) in log output: {where[:10] or findings[:10]}")

    # No member id survives, by value and by shape (the masking filter rewrites id-like tokens).
    member_ids = [value for kind, value in identifiers if kind == helpers.KIND_MEMBER_ID]
    assert member_ids and not any(value in caplog.text for value in member_ids)
    prefixes = sorted({re.match(r"[A-Z]+", value).group(0) for value in member_ids})
    assert not re.search(r"\b(?:" + "|".join(prefixes) + r")\d{9}\b", caplog.text)
    assert not MEMBER_ID_SHAPE.search(caplog.text), MEMBER_ID_SHAPE.findall(caplog.text)[:5]

    # The primary control: no application logger wrote anything the masking filter had to rewrite
    # (the filter is the backstop). Only the probe logger may show hits.
    unexpected = [hit for hit in _app_mask_hits() if hit not in KNOWN_MASK_OFFENDERS]
    assert not unexpected, f"app loggers needed the masking filter: {unexpected}"
    assert all(name == PROBE_LOGGER or name.startswith("app") for name, _label in hits), sorted(set(hits))[
        :10
    ]

    # The two exc_info records were folded: frames and the class name, no exception message.
    folded = [r for r in records if r.name == PROBE_LOGGER and "import failed" in r.getMessage()]
    assert len(folded) == 2
    for record in folded:
        assert record.exc_info is None and record.exc_text is None
        message = record.getMessage()
        assert 'File "' in message and "test_phi_in_logs.py" in message
        assert message.rstrip().endswith(("X12Error", "ValueError"))
        assert "rejected row" not in message and "DOB" not in message
    assert "rejected row" not in caplog.text
    assert "Traceback (most recent call last)" not in caplog.text


def test_route_exception_is_logged_as_class_name_and_frames_only(app, caplog):
    """The app's catch-all handler logs an unhandled route error with exc_info; the masked root
    handler folds it to the request line, the frames and the class name (the message can quote
    request or row data) and the response never echoes it either."""
    from fastapi.testclient import TestClient

    def boom() -> None:
        raise RuntimeError(SECRET)

    app.add_api_route("/__boom", boom)
    with app_logging("INFO", caplog), TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/__boom")
    assert response.status_code == 500 and response.json() == {"error": "internal error"}
    assert "MBR-SECRET-1" not in response.text

    records = [r for r in caplog.records if r.name == "app.main"]
    assert len(records) == 1 and records[0].exc_info is None and records[0].exc_text is None
    message = records[0].getMessage()
    assert "unhandled error on GET /__boom: RuntimeError" in message
    assert "test_phi_in_logs.py" in message and message.rstrip().endswith("RuntimeError")
    for secret_part in ("MBR-SECRET-1", "Jane", "Synthetic", "1980-01-02"):
        assert secret_part not in caplog.text, secret_part


def test_serve_logging_folds_uvicorn_tracebacks(settings, caplog):
    """`denial-agent serve` passes ``log_config=None`` (tests/test_cli.py): uvicorn's loggers then own
    no handlers and propagate to the masked root handler, so the "Exception in ASGI application"
    record uvicorn writes after the catch-all handler re-raises carries frames and the exception
    class, never the message."""
    import uvicorn

    with _uvicorn_loggers_restored(), app_logging("INFO", caplog):
        uvicorn.Config("app.main:app", log_level=settings.log_level.lower(), log_config=None)
        for name in UVICORN_LOGGERS:
            logger = logging.getLogger(name)
            assert logger.handlers == [] and logger.propagate, name
        try:
            raise RuntimeError(SECRET)
        except RuntimeError:
            logging.getLogger("uvicorn.error").error("Exception in ASGI application", exc_info=True)

    records = [r for r in caplog.records if r.name == "uvicorn.error"]
    assert len(records) == 1 and records[0].exc_info is None
    message = records[0].getMessage()
    assert message.startswith("Exception in ASGI application") and message.rstrip().endswith("RuntimeError")
    assert "test_phi_in_logs.py" in message
    for secret_part in ("MBR-SECRET-1", "Jane", "Synthetic", "1980-01-02"):
        assert secret_part not in caplog.text, secret_part
