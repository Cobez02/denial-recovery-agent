"""Job runner (inline | rq). Tasks live in app.jobs.tasks and are addressed by dotted path."""

from app.jobs.runner import JobHandle, enqueue, fetch_job

__all__ = ["JobHandle", "enqueue", "fetch_job"]
