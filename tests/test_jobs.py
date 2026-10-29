"""Job runner: the inline backend runs tasks synchronously; the rq backend queues by dotted path.

The task tests at the bottom drive the real ingest and pipeline modules with a small synthetic
batch: they guard the cross-module wiring (signatures, result keys) and the rule that an import is
committed even when the pipeline run after it fails.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app import audit
from app.jobs import JobHandle, enqueue
from app.jobs.runner import format_frames, resolve_task
from app.models import ImportBatch, Remittance, WorkItem

SECRET_MESSAGE = "boom-secret-detail"


def sample_task(x, y=1):
    return {"sum": x + y}


def failing_task():
    raise RuntimeError(SECRET_MESSAGE)


def test_inline_runs_dotted_task(settings):
    assert settings.job_backend == "inline"
    handle = enqueue("tests.test_jobs.sample_task", 2, y=3)
    assert isinstance(handle, JobHandle)
    assert handle.status == "finished"
    assert handle.result == {"sum": 5}
    assert handle.error is None
    assert len(handle.id) == 32


def test_inline_failure_records_class_name_only(settings, caplog):
    with caplog.at_level(logging.WARNING, logger="app.jobs.runner"):
        handle = enqueue("tests.test_jobs.failing_task")
    assert handle.status == "failed"
    assert handle.error == "RuntimeError"
    assert handle.result is None
    assert "RuntimeError" in caplog.text
    assert "boom-secret-detail" not in caplog.text


def test_inline_failure_debug_traceback_has_frames_but_no_message(settings, caplog):
    # exc_info tracebacks bypass the PHI-masking filter (it rewrites the message only), so the
    # runner may log the frames of a failed job but never the exception message.
    with caplog.at_level(logging.DEBUG, logger="app.jobs.runner"):
        handle = enqueue("tests.test_jobs.failing_task")
    assert handle.status == "failed"
    debug = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert len(debug) == 1 and debug[0].exc_info is None
    assert "failing_task" in debug[0].getMessage()  # the frame that raised
    assert SECRET_MESSAGE not in caplog.text


def test_format_frames_omits_exception_message():
    try:
        failing_task()
    except RuntimeError as exc:
        frames = format_frames(exc)
    assert "failing_task" in frames and "test_jobs.py" in frames
    # Source lines are code (`raise RuntimeError(SECRET_MESSAGE)`), never the message's value.
    assert SECRET_MESSAGE not in frames
    assert not any(line.startswith("RuntimeError") for line in frames.splitlines())


def test_inline_unknown_task(settings):
    assert enqueue("tests.test_jobs.no_such_task").status == "failed"
    assert enqueue("no_such_module_xyz.task").error == "ModuleNotFoundError"
    with pytest.raises(ValueError):
        resolve_task("not_dotted")


def test_rq_backend_enqueues_by_dotted_path(settings, monkeypatch):
    import redis
    import rq

    calls: dict = {}

    class FakeJob:
        id = "job-123"

    class FakeQueue:
        def __init__(self, name, connection=None, **kwargs):
            calls["name"] = name
            calls["connection"] = connection

        def enqueue(self, f, *args, **kwargs):
            calls["enqueue"] = (f, args, kwargs)
            return FakeJob()

    monkeypatch.setattr(rq, "Queue", FakeQueue)
    monkeypatch.setattr(redis, "from_url", lambda url: ("fake-redis", url))
    monkeypatch.setattr(settings, "job_backend", "rq")

    handle = enqueue("app.jobs.tasks.process_pending", created_by="tests")
    assert handle.status == "queued"
    assert handle.id == "job-123"
    assert handle.result is None and handle.error is None
    assert calls["name"] == "default"
    assert calls["connection"] == ("fake-redis", settings.redis_url)
    assert calls["enqueue"] == ("app.jobs.tasks.process_pending", (), {"created_by": "tests"})


def test_process_pending_task_uses_its_own_session(engine):
    from app.jobs import tasks

    result = tasks.process_pending(created_by="tests")
    assert result["work_items_created"] == 0 and result["work_item_ids"] == []
    assert result["pipeline"] in ("ok", "unavailable")
    assert result["requested_by"] == "tests"


def test_process_pending_via_enqueue(engine):
    handle = enqueue("app.jobs.tasks.process_pending")
    assert handle.status == "finished"
    assert handle.result["work_items_created"] == 0


# ---- the tasks against the real ingest + pipeline modules -----------------------------------


@pytest.fixture()
def small_batch(tmp_path) -> Path:
    generate = pytest.importorskip("synthetic.generate").generate
    out = tmp_path / "syn"
    generate(seed=11, n_claims=12, out_dir=out)
    return out


def _count(session, model) -> int:
    return session.execute(select(func.count(model.id))).scalar_one()


def test_import_tasks_wire_ingest_and_pipeline(engine, session, clinic, small_batch):
    pytest.importorskip("app.pipeline")
    from app.jobs import tasks

    claims = tasks.import_claims_file(
        str(small_batch / "claims_export.csv"), clinic_id=clinic.id, created_by="t"
    )
    assert claims["kind"] == "claims" and claims["status"] == "done" and claims["error"] is None
    assert claims["counts"]["claims_created"] == 12
    assert claims["pipeline"] == "ok" and claims["work_items_created"] == 0  # nothing denied yet

    for name, task in (
        ("notes.csv", tasks.import_notes_file),
        ("payer_documents.csv", tasks.import_documents_file),
    ):
        result = task(str(small_batch / name), clinic_id=clinic.id, created_by="t")
        assert result["status"] == "done", name

    created_total = 0
    for remit in sorted(small_batch.glob("remit_*.835")):
        result = tasks.import_835_file(str(remit), file_name=remit.name, created_by="t")
        assert result["kind"] == "835" and result["status"] == "done" and result["file_name"] == remit.name
        assert result["counts"]["unmatched"] == 0
        assert result["pipeline"] == "ok"
        assert result["work_items_created"] == result["counts"]["denials"] == len(result["work_item_ids"])
        created_total += result["work_items_created"]
    assert created_total > 0
    assert _count(session, WorkItem) == created_total
    assert {wi.status for wi in session.execute(select(WorkItem)).scalars()} == {"proposed"}

    # Re-importing a file is a no-op and the pipeline is idempotent.
    again = tasks.import_835_file(str(sorted(small_batch.glob("remit_*.835"))[0]), created_by="t")
    assert again["status"] == "duplicate" and again["work_items_created"] == 0
    assert tasks.process_pending(created_by="t")["work_items_created"] == 0
    assert _count(session, WorkItem) == created_total
    assert audit.verify_chain(session) is None


def test_pipeline_failure_keeps_import_committed(engine, session, clinic, small_batch, monkeypatch, caplog):
    pipeline = pytest.importorskip("app.pipeline")
    from app.jobs import tasks

    tasks.import_claims_file(str(small_batch / "claims_export.csv"), clinic_id=clinic.id, created_by="t")

    def broken(session_, **kwargs):
        raise RuntimeError(SECRET_MESSAGE)

    monkeypatch.setattr(pipeline, "process_pending", broken)
    remits = sorted(small_batch.glob("remit_*.835"))
    denials = 0
    with caplog.at_level(logging.DEBUG, logger="app.jobs.tasks"):
        for remit in remits:
            result = tasks.import_835_file(str(remit), created_by="t")
            # The import itself is durable and reported; only the pipeline phase failed.
            assert result["status"] == "done" and result["counts"]["unmatched"] == 0
            assert result["pipeline"] == "failed" and result["pipeline_error"] == "RuntimeError"
            assert result["work_items_created"] == 0 and result["work_item_ids"] == []
            batch = session.get(ImportBatch, result["batch_id"])
            assert batch is not None and batch.status == "done"
            denials += result["counts"]["denials"]
    assert denials > 0
    assert _count(session, Remittance) == len(remits)
    assert _count(session, WorkItem) == 0
    assert "RuntimeError" in caplog.text and SECRET_MESSAGE not in caplog.text

    # `process` later picks the denials up once the pipeline works again.
    monkeypatch.undo()
    recovered = tasks.process_pending(created_by="t")
    assert recovered["pipeline"] == "ok"
    assert recovered["work_items_created"] == denials
    assert _count(session, WorkItem) == denials


def test_pipeline_failure_rolls_back_partial_work(engine, session, clinic, small_batch, monkeypatch):
    """A failure in the middle of a pipeline run leaves no half-built work item behind."""
    pipeline = pytest.importorskip("app.pipeline")
    from app.jobs import tasks

    tasks.import_claims_file(str(small_batch / "claims_export.csv"), clinic_id=clinic.id, created_by="t")
    # Import every 835 with the pipeline parked, so all the batch's denials are pending at once.
    monkeypatch.setattr(pipeline, "process_pending", lambda session_, **kwargs: [])
    denials = sum(
        tasks.import_835_file(str(remit), created_by="t")["counts"]["denials"]
        for remit in sorted(small_batch.glob("remit_*.835"))
    )
    assert denials >= 2
    monkeypatch.undo()

    original = pipeline.process_remittance_claim
    calls = {"n": 0}

    def flaky(session_, rc, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError(SECRET_MESSAGE)
        return original(session_, rc, **kwargs)

    monkeypatch.setattr(pipeline, "process_remittance_claim", flaky)
    result = tasks.process_pending(created_by="t")
    assert result["pipeline"] == "failed" and result["pipeline_error"] == "RuntimeError"
    assert calls["n"] == 2
    assert _count(session, WorkItem) == 0  # the first item's rows were rolled back with the failure
    assert audit.verify_chain(session) is None

    monkeypatch.undo()
    assert tasks.process_pending(created_by="t")["work_items_created"] == denials
    assert _count(session, WorkItem) == denials
