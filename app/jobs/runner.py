"""Job runner: `inline` (synchronous, the default locally and in tests) or `rq` (Redis worker).

Tasks are addressed by dotted path (for example "app.jobs.tasks.import_835_file") so the same call
works for both backends. Nothing about a job's arguments is ever logged: a failed inline job logs
the task path and the exception class name, plus (at DEBUG) the traceback frames without the
exception message, because a message can quote the input that caused it.
"""

from __future__ import annotations

import importlib
import logging
import traceback
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.config import Settings, get_settings

log = logging.getLogger(__name__)

QUEUE_NAME = "default"

STATUS_QUEUED = "queued"
STATUS_FINISHED = "finished"
STATUS_FAILED = "failed"


@dataclass
class JobHandle:
    id: str
    status: str  # queued | finished | failed (rq may also report started/deferred/...)
    result: Any = None
    error: str | None = None  # exception class name only, never a message


def format_frames(exc: BaseException) -> str:
    """The traceback's frames (file, line, function, source line) without the exception message.

    Log formatters write `exc_info` tracebacks after the PHI-masking filter has run, and an
    exception message may quote the data that caused it (a CSV cell, a note), so only this form
    of a traceback may be logged.
    """
    return "".join(traceback.format_tb(exc.__traceback__)).rstrip()


def resolve_task(task_path: str) -> Callable[..., Any]:
    """Import "package.module.function" and return the callable."""
    module_path, sep, name = task_path.rpartition(".")
    if not sep or not module_path or not name:
        raise ValueError("task path must be a dotted path to a function")
    module = importlib.import_module(module_path)
    func = getattr(module, name, None)
    if func is None or not callable(func):
        raise ValueError(f"task {task_path!r} is not a callable")
    return func


def enqueue(task_path: str, *args: Any, **kwargs: Any) -> JobHandle:
    """Run `task_path(*args, **kwargs)` now (inline) or queue it on Redis (rq)."""
    settings = get_settings()
    if settings.job_backend == "rq":
        return _enqueue_rq(settings, task_path, *args, **kwargs)
    return _run_inline(task_path, *args, **kwargs)


def _run_inline(task_path: str, *args: Any, **kwargs: Any) -> JobHandle:
    job_id = uuid.uuid4().hex
    try:
        func = resolve_task(task_path)
        result = func(*args, **kwargs)
    except Exception as exc:
        log.warning("inline job %s (%s) failed: %s", job_id, task_path, type(exc).__name__)
        log.debug("inline job %s traceback (frames only):\n%s", job_id, format_frames(exc))
        return JobHandle(id=job_id, status=STATUS_FAILED, error=type(exc).__name__)
    log.info("inline job %s (%s) finished", job_id, task_path)
    return JobHandle(id=job_id, status=STATUS_FINISHED, result=result)


def _enqueue_rq(settings: Settings, task_path: str, *args: Any, **kwargs: Any) -> JobHandle:
    import redis
    import rq

    connection = redis.from_url(settings.redis_url)
    queue = rq.Queue(QUEUE_NAME, connection=connection)
    job = queue.enqueue(task_path, *args, **kwargs)
    log.info("queued job %s (%s)", job.id, task_path)
    return JobHandle(id=str(job.id), status=STATUS_QUEUED)


def fetch_job(job_id: str) -> JobHandle | None:
    """Current state of an rq job (None for the inline backend or an unknown id)."""
    settings = get_settings()
    if settings.job_backend != "rq":
        return None
    import redis
    import rq
    from rq.exceptions import NoSuchJobError

    connection = redis.from_url(settings.redis_url)
    try:
        job = rq.job.Job.fetch(job_id, connection=connection)
    except NoSuchJobError:
        return None
    status = job.get_status()
    status_name = getattr(status, "value", str(status))
    if status_name == STATUS_FINISHED:
        return JobHandle(id=job_id, status=STATUS_FINISHED, result=job.return_value())
    if status_name == STATUS_FAILED:
        # rq stores the traceback; keep the exception class name only.
        exc_info = (job.exc_info or "").strip()
        error = exc_info.splitlines()[-1].split(":")[0] if exc_info else "Exception"
        return JobHandle(id=job_id, status=STATUS_FAILED, error=error)
    return JobHandle(id=job_id, status=status_name)


__all__ = [
    "JobHandle",
    "QUEUE_NAME",
    "STATUS_FAILED",
    "STATUS_FINISHED",
    "STATUS_QUEUED",
    "enqueue",
    "fetch_job",
    "format_frames",
    "resolve_task",
]
