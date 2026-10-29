"""Logging setup with a PHI-masking filter.

Policy: log records carry ids, counts and codes only. The masking filter is defence in depth -
it rewrites things that look like identifiers (dates of birth, member-id-like tokens, emails,
SSNs, phone numbers) before the record is written. tests/test_phi_in_logs.py fails the build if
any synthetic identifier appears in captured log output.
"""

from __future__ import annotations

import logging
import re
import sys
import traceback

_MASK_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[SSN]"),
    (re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "[EMAIL]"),
    (re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b"), "[PHONE]"),
    # DOB-like dates in several formats (service dates can also match; that is acceptable in logs).
    (re.compile(r"\b(?:19|20)\d{2}-\d{2}-\d{2}\b"), "[DATE]"),
    (re.compile(r"\b\d{1,2}/\d{1,2}/(?:19|20)\d{2}\b"), "[DATE]"),
    (re.compile(r"\b(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])(?:19|20)\d{2}\b"), "[DATE]"),
    # Member-id-like tokens: letters followed by 6+ digits, or 9+ digit runs.
    (re.compile(r"\b[A-Z]{1,4}\d{6,}\b"), "[ID]"),
    (re.compile(r"\b\d{9,}\b"), "[ID]"),
    (re.compile(r"(?i)\b(dob|date of birth|member id|member_id|ssn)\s*[:=]\s*\S+"), r"\1: [MASKED]"),
]


def mask_text(text: str) -> str:
    for pattern, repl in _MASK_PATTERNS:
        text = pattern.sub(repl, text)
    return text


class PHIMaskingFilter(logging.Filter):
    """Mask identifier-like substrings in the formatted message and its arguments."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover - malformed log call
            msg = str(record.msg)
        if record.exc_info:
            # The formatter appends tracebacks after filters ran, so fold them in here: frames
            # (file, line, code) and the exception class only, never the exception message, which
            # may carry whatever text the failing call was handling.
            exc_type, _exc, tb = record.exc_info
            frames = "".join(traceback.format_tb(tb)) if tb is not None else ""
            name = getattr(exc_type, "__name__", str(exc_type))
            msg = f"{msg}\n{frames}{name}".rstrip()
            record.exc_info = None
            record.exc_text = None
        record.msg = mask_text(msg)
        record.args = ()
        return True


def configure_logging(level: str | int = "INFO") -> None:
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        if getattr(h, "_denial_agent_handler", False):
            root.removeHandler(h)
        elif not any(isinstance(f, PHIMaskingFilter) for f in h.filters):
            h.addFilter(PHIMaskingFilter())  # foreign handlers (pytest caplog, uvicorn) mask too
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.addFilter(PHIMaskingFilter())
    handler._denial_agent_handler = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    # Quiet noisy libraries; the SDK never logs request bodies at INFO.
    for noisy in ("httpx", "httpcore", "anthropic", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def install_masking_filter_everywhere() -> None:
    """Attach the masking filter to every existing handler (used by tests)."""
    for logger in [logging.getLogger()] + [logging.getLogger(n) for n in logging.root.manager.loggerDict]:
        for h in getattr(logger, "handlers", []):
            if not any(isinstance(f, PHIMaskingFilter) for f in h.filters):
                h.addFilter(PHIMaskingFilter())
