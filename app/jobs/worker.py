"""RQ worker entry point for ``JOB_BACKEND=rq``: ``python -m app.jobs.worker [--burst] [--queue NAME]``.

A plain ``rq worker`` process would (1) never call :func:`app.logging_config.configure_logging`, so
nothing masks its log records; (2) log every failed job's full traceback, exception message
included, through handlers of its own; (3) store that same text in Redis as the job's failure; and
(4) log each job's call string with its arguments. An exception message can quote the data that
caused it (a CSV cell, a bound SQL parameter), so this entry point configures the application
logging first (rq's loggers then propagate to the masked root handler instead of installing
handlers of their own), attaches the masking filter to rq's loggers as well, and runs a worker that
logs and stores only the exception class name and the traceback frames, never the message.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import traceback
from collections.abc import Sequence
from types import TracebackType
from typing import Any

from rq.exceptions import DeserializationError
from rq.job_lifecycle import call_exception_handlers
from rq.worker import SimpleWorker, Worker

from app.config import Settings, get_settings
from app.jobs.runner import QUEUE_NAME
from app.logging_config import PHIMaskingFilter, configure_logging

log = logging.getLogger(__name__)

RQ_LOGGER_NAMES: tuple[str, ...] = (
    "rq",
    "rq.job",
    "rq.queue",
    "rq.registry",
    "rq.scripts",
    "rq.webhook",
    "rq.worker",
)
TRACEBACK_HEADER = "Traceback (most recent call last):"
EXIT_WRONG_BACKEND = 2

ExcInfo = tuple[type[BaseException] | None, BaseException | None, TracebackType | None]


def format_failure(exc_info: ExcInfo) -> str:
    """Traceback frames plus the exception class name; never the exception message.

    This is the only form of a job failure that may be logged or stored (see app.jobs.runner).
    """
    exc_type, _exc, tb = exc_info
    frames = "".join(traceback.format_tb(tb)) if tb is not None else ""
    name = getattr(exc_type, "__name__", str(exc_type))
    return f"{frames}{name}".rstrip()


def safe_exception_text(text: str) -> str:
    """Strip the exception message from a traceback string rq composed itself.

    rq's own failure strings for a killed or stopped work horse carry no traceback and are kept
    as they are; a formatted traceback is reduced to the exception class name on its last line.
    """
    if TRACEBACK_HEADER not in text:
        return text
    lines = [line for line in text.rstrip().splitlines() if line.strip()]
    return lines[-1].split(":", 1)[0].strip() if lines else ""


def install_rq_masking() -> None:
    """Attach the PHI-masking filter to every rq logger (and any handler already on one)."""
    names = set(RQ_LOGGER_NAMES)
    names.update(n for n in logging.root.manager.loggerDict if n == "rq" or n.startswith("rq."))
    for name in sorted(names):
        logger = logging.getLogger(name)
        if not any(isinstance(f, PHIMaskingFilter) for f in logger.filters):
            logger.addFilter(PHIMaskingFilter())
        for handler in logger.handlers:
            if not any(isinstance(f, PHIMaskingFilter) for f in handler.filters):
                handler.addFilter(PHIMaskingFilter())


class _MaskedFailureMixin:
    """Overrides rq's two failure hooks so neither the log nor Redis sees an exception message.

    ``handle_exception`` runs first for a job that raised (rq logs the full traceback there by
    default, before any registered exception handler); ``handle_job_failure`` then persists the
    ``exc_string`` it is given. The frames-and-class text built in the first hook is handed to the
    second by job id.
    """

    log: logging.Logger
    name: str
    _exc_handlers: list[Any]

    def _safe_failures(self) -> dict[str, str]:
        failures = self.__dict__.get("_masked_failures")
        if failures is None:
            failures = self.__dict__["_masked_failures"] = {}
        return failures

    def handle_exception(self, job: Any, *exc_info: Any) -> None:
        safe = format_failure(exc_info)  # type: ignore[arg-type]
        self._safe_failures()[job.id] = safe
        try:
            func_name = job.func_name
        except DeserializationError:
            func_name = "<DeserializationError>"
        self.log.error("Worker %s: job %s (%s) failed:\n%s", self.name, job.id, func_name, safe)
        call_exception_handlers(self._exc_handlers, job, *exc_info)

    def handle_job_failure(
        self,
        job: Any,
        queue: Any,
        started_job_registry: Any = None,
        exc_string: str = "",
        execution: Any = None,
    ) -> Any:
        safe = self._safe_failures().pop(job.id, None)
        if safe is None:
            safe = safe_exception_text(exc_string)
        return super().handle_job_failure(  # type: ignore[misc]
            job,
            queue,
            started_job_registry=started_job_registry,
            exc_string=safe,
            execution=execution,
        )


class MaskedWorker(_MaskedFailureMixin, Worker):
    """Forking rq worker (Linux, the Docker image) that logs failures as frames + class name."""


class MaskedSimpleWorker(_MaskedFailureMixin, SimpleWorker):
    """The same, running jobs in the worker process itself (no fork: Windows and tests)."""


def worker_class() -> type[MaskedWorker | MaskedSimpleWorker]:
    """The forking rq worker where ``os.fork`` exists, the in-process one elsewhere."""
    return MaskedWorker if hasattr(os, "fork") else MaskedSimpleWorker


def build_worker(
    settings: Settings,
    queue_names: Sequence[str] | None = None,
    *,
    connection: Any = None,
    prepare_for_work: bool = True,
) -> Any:
    """An rq worker on ``settings.redis_url`` that never logs job arguments or exception messages.

    ``prepare_for_work=False`` builds it without issuing a Redis command (tests).
    """
    import redis
    import rq

    if connection is None:
        connection = redis.from_url(settings.redis_url)
    names = list(queue_names) if queue_names else [QUEUE_NAME]
    queues = [rq.Queue(name, connection=connection) for name in names]
    return worker_class()(
        queues,
        connection=connection,
        log_job_description=False,
        prepare_for_work=prepare_for_work,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.jobs.worker",
        description="Run one RQ worker for the Denial Recovery Agent (JOB_BACKEND=rq).",
    )
    parser.add_argument(
        "--queue",
        action="append",
        dest="queues",
        default=None,
        help=f"queue to listen on (repeatable; default: {QUEUE_NAME})",
    )
    parser.add_argument("--burst", action="store_true", help="exit when every queue is empty")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level)
    install_rq_masking()
    if settings.job_backend != "rq":
        log.error("JOB_BACKEND is %s; the worker serves the rq backend only", settings.job_backend)
        return EXIT_WRONG_BACKEND
    worker = build_worker(settings, args.queues)
    log.info("rq worker %s listening on %s", worker.name, ",".join(q.name for q in worker.queues))
    worker.work(burst=args.burst, logging_level=settings.log_level.upper(), with_scheduler=False)
    return 0


__all__ = [
    "EXIT_WRONG_BACKEND",
    "RQ_LOGGER_NAMES",
    "MaskedSimpleWorker",
    "MaskedWorker",
    "build_parser",
    "build_worker",
    "format_failure",
    "install_rq_masking",
    "main",
    "safe_exception_text",
    "worker_class",
]


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
