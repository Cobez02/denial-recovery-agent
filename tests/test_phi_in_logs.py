"""PHI in logs (plan testing table; docs/SPEC.md section 15).

With the application's logging configured at DEBUG and every record captured, a full run over the
committed synthetic batch (import, pipeline, a deliberately failing import and two tracebacks logged
with ``exc_info``) must leave zero synthetic identifiers in the log output, and the failed batch's
error and audit event must name positions only. The test fails, never skips, on a finding, and it
cannot pass vacuously: it demands more than fifty captured records.
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import select

from app.enums import ImportStatus
from app.ingest.parse_835 import parse_835
from app.ingest.remit_import import import_835
from app.ingest.x12 import X12Error
from app.logging_config import PHIMaskingFilter, configure_logging
from app.models import AuditLog
from tests import helpers

MIN_RECORDS = 50
MEMBER_ID_SHAPE = re.compile(r"\b[A-Z]{1,4}\d{6,}\b")


def _malformed_835(row: dict[str, str]) -> str:
    """An 835 that fails in the envelope reader and quotes a patient on every element."""
    return (
        f"ISA*00*{row['patient_first_name']} {row['patient_last_name']}*DOB {row['patient_dob']}"
        f"*MEMBER {row['member_id']}*{row['patient_address_line1']}~GS*HP*X*Y~ST*835*0001~"
    )


def test_no_identifier_reaches_the_logs(session, settings, clinic, caplog):
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_filters = {handler: list(handler.filters) for handler in saved_handlers}
    # caplog first so it restores the root level it found; configure_logging then attaches the
    # masking filter to the capture handler (and every other root handler) exactly as the app does.
    caplog.set_level(logging.DEBUG)
    configure_logging("DEBUG")
    if not any(isinstance(f, PHIMaskingFilter) for f in caplog.handler.filters):
        caplog.handler.addFilter(PHIMaskingFilter())
    row = helpers.read_claim_rows()[0]
    try:
        helpers.load_fixture_batch(session, settings, clinic)
        created = helpers.run_pipeline(session)
        assert len(created) == len(helpers.load_labels())

        # A failing import: the batch fails and its error names positions, never values.
        batch = import_835(session, _malformed_835(row), file_name="malformed.835", created_by="tests")
        session.commit()
        assert batch.status == str(ImportStatus.FAILED) and batch.error
        assert batch.error.startswith("parse error")

        # Tracebacks logged with exc_info by application loggers are folded to frames + class name.
        try:
            parse_835(_malformed_835(row))
        except X12Error:
            logging.getLogger("app.ingest.remit_import").exception("835 import failed for batch %s", batch.id)
        try:
            raise ValueError(
                f"rejected row for {row['patient_first_name']} {row['patient_last_name']} "
                f"born {row['patient_dob']} member {row['member_id']}"
            )
        except ValueError:
            logging.getLogger("app.ingest.claims_import").exception(
                "claims import failed for batch %s", batch.id
            )
    finally:
        for handler in list(root.handlers):
            if handler not in saved_handlers:
                root.removeHandler(handler)
        for handler in saved_handlers:
            if handler not in root.handlers:
                root.addHandler(handler)
            handler.filters = saved_filters[handler]

    records = list(caplog.records)
    assert len(records) > MIN_RECORDS, len(records)
    assert {record.name.split(".")[0] for record in records} >= {"app"}

    identifiers = helpers.identifiers_for_batch()

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

    # The two exc_info records were folded: frames and the class name, no exception message.
    folded = [
        r
        for r in records
        if r.name in ("app.ingest.remit_import", "app.ingest.claims_import")
        and "import failed" in r.getMessage()
    ]
    assert len(folded) == 2
    for record in folded:
        assert record.exc_info is None and record.exc_text is None
        message = record.getMessage()
        assert 'File "' in message and "test_phi_in_logs.py" in message
        assert message.rstrip().endswith(("X12Error", "ValueError"))
        assert "rejected row" not in message and "DOB" not in message
    assert "rejected row" not in caplog.text
    assert "Traceback (most recent call last)" not in caplog.text
