"""Background tasks: file imports and pipeline processing.

Each task opens its own database sessions (jobs run in a worker process or inline). An import
task commits the import first, then runs the pipeline over every denied, matched remittance claim
that has no work item yet in a second transaction, so a pipeline failure never discards a finished
import. The pipeline itself handles each claim in its own savepoint: a claim that fails (a model
transport error, a bug in one playbook) is rolled back and audited while the others complete, and
the result lists it under `failed_remittance_claim_ids` with `pipeline: "partial"` (or `"failed"`
when nothing could be processed, or when the run itself raised) and the first exception class
name as `pipeline_error`; `denial-agent process` picks the failed denials up later. Results are
plain dicts (ids, statuses and counts) so they serialise through rq. The ingest and pipeline
modules are imported inside the tasks to keep the worker's import cheap.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app.db import session_scope
from app.jobs.runner import format_frames
from app.models import ImportBatch

log = logging.getLogger(__name__)

DEFAULT_ACTOR = "system"

PIPELINE_OK = "ok"
PIPELINE_PARTIAL = "partial"
PIPELINE_FAILED = "failed"


def _batch_result(batch: ImportBatch) -> dict[str, Any]:
    return {
        "batch_id": batch.id,
        "kind": batch.kind,
        "file_name": batch.file_name,
        "status": batch.status,
        "counts": dict(batch.counts or {}),
        "error": batch.error,
    }


def _run_pipeline() -> dict[str, Any]:
    """Create work items for every pending denial in its own transaction.

    Result keys: `pipeline` ("ok" | "partial" | "failed"), `work_items_created`, `work_item_ids`,
    `pipeline_failures` (count), `failed_remittance_claim_ids` and, when anything failed,
    `pipeline_error` (the first exception class name, never a message). The pipeline rolls a failing
    claim back to its own savepoint and continues, so the committed transaction holds every claim
    that succeeded; `partial` means some did, `failed` means none did or the run itself raised. The
    failed denials stay pending for the next run.
    """
    from app.pipeline import process_pending as pipeline_process_pending

    try:
        with session_scope() as session:
            run = pipeline_process_pending(session)
            work_item_ids = [wi.id for wi in run]
            failures = list(getattr(run, "failed", []))
            failed_ids = [failure.remittance_claim_id for failure in failures]
            first_error = failures[0].error if failures else None
    except Exception as exc:
        log.warning("pipeline failed (%s); pending denials stay queued for `process`", type(exc).__name__)
        log.debug("pipeline traceback (frames only):\n%s", format_frames(exc))
        return {
            "pipeline": PIPELINE_FAILED,
            "pipeline_error": type(exc).__name__,
            "work_items_created": 0,
            "work_item_ids": [],
            "pipeline_failures": 0,
            "failed_remittance_claim_ids": [],
        }
    if not failures:
        status = PIPELINE_OK
    elif work_item_ids:
        status = PIPELINE_PARTIAL
    else:
        status = PIPELINE_FAILED
    result: dict[str, Any] = {
        "pipeline": status,
        "work_items_created": len(work_item_ids),
        "work_item_ids": work_item_ids,
        "pipeline_failures": len(failures),
        "failed_remittance_claim_ids": failed_ids,
    }
    if first_error is not None:
        result["pipeline_error"] = first_error
        log.warning(
            "pipeline skipped %d remittance claim(s) (first error %s); they stay queued for `process`",
            len(failures),
            first_error,
        )
    return result


def _finish(result: dict[str, Any], kind: str) -> dict[str, Any]:
    result.update(_run_pipeline())
    log.info(
        "%s import batch %s finished with status %s (pipeline %s, %d work item(s))",
        kind,
        result["batch_id"],
        result["status"],
        result["pipeline"],
        result["work_items_created"],
    )
    return result


def import_835_file(
    path: str, file_name: str | None = None, created_by: str = DEFAULT_ACTOR
) -> dict[str, Any]:
    """Import one X12 835 file (committed on its own), then process the denials it carries."""
    source = Path(path)
    with session_scope() as session:
        from app.ingest.remit_import import import_835

        batch = import_835(session, source, file_name=file_name or source.name, created_by=created_by)
        result = _batch_result(batch)
    return _finish(result, "835")


def import_claims_file(path: str, clinic_id: int, created_by: str = DEFAULT_ACTOR) -> dict[str, Any]:
    """Import a claim-export CSV for a clinic, then process any now-matchable denials."""
    source = Path(path)
    with session_scope() as session:
        from app.ingest.claims_import import import_claims_csv

        batch = import_claims_csv(session, source, clinic_id=clinic_id, created_by=created_by)
        result = _batch_result(batch)
    return _finish(result, "claims")


def import_notes_file(path: str, clinic_id: int, created_by: str = DEFAULT_ACTOR) -> dict[str, Any]:
    """Import notes.csv (quoted note excerpts) for a clinic."""
    source = Path(path)
    with session_scope() as session:
        from app.ingest.claims_import import import_notes_csv

        batch = import_notes_csv(session, source, clinic_id=clinic_id, created_by=created_by)
        result = _batch_result(batch)
    return _finish(result, "notes")


def import_documents_file(path: str, clinic_id: int, created_by: str = DEFAULT_ACTOR) -> dict[str, Any]:
    """Import payer_documents.csv (letters / portal text; untrusted data) for a clinic."""
    source = Path(path)
    with session_scope() as session:
        from app.ingest.claims_import import import_payer_documents_csv

        batch = import_payer_documents_csv(session, source, clinic_id=clinic_id, created_by=created_by)
        result = _batch_result(batch)
    return _finish(result, "documents")


def process_pending(created_by: str = DEFAULT_ACTOR) -> dict[str, Any]:
    """Run the pipeline over every denied, matched remittance claim without a work item."""
    result = _run_pipeline()
    result["requested_by"] = created_by
    log.info(
        "process_pending: pipeline %s, %d work item(s)", result["pipeline"], result["work_items_created"]
    )
    return result


__all__ = [
    "PIPELINE_FAILED",
    "PIPELINE_OK",
    "PIPELINE_PARTIAL",
    "import_835_file",
    "import_claims_file",
    "import_documents_file",
    "import_notes_file",
    "process_pending",
]
