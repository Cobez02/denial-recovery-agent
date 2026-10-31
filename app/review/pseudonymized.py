"""Marking what comes from a pseudonymized batch (docs/SPEC.md section 16).

`denial-agent anonymize` names every file it writes ``pseudonymized_run<N>_<kind>_<n>.<ext>``. The
claims importer records that name on the import batch (an upload keeps it after its hash prefix), and
every claim points at its import batch, so the export (``app/review/export.py``) knows a claim came
from a pseudonymized batch without any new column. Its package then says so wherever a person looks:
the zip name starts with ``PSEUDONYMIZED_``, the letter and the clinic question open with
:data:`BANNER`, ``summary.json`` carries ``"pseudonymized": true`` and the corrected claim CSV a
:data:`CSV_COLUMN` column. ``denial-agent deanonymize`` removes every one of them.
"""

from __future__ import annotations

import re

FILE_PREFIX = "pseudonymized_"
FILE_RE = re.compile(r"(?:^|[_\-.])pseudonymized_run\d+_", re.IGNORECASE)
ZIP_PREFIX = "PSEUDONYMIZED_"
SUMMARY_KEY = "pseudonymized"
CSV_COLUMN = "pseudonymized_restore_before_submitting"
CSV_VALUE = "yes"
BANNER = (
    "*** PSEUDONYMIZED COPY - NOT FOR SUBMISSION. Every name, id and date of birth below is a "
    "pseudonym: restore this package with denial-agent deanonymize on the clinic's computer first. ***"
)


def batch_file_name(run: int, kind: str, number: int, extension: str) -> str:
    return f"{FILE_PREFIX}run{run}_{kind}_{number}.{extension}"


def is_pseudonymized_file(name: str | None) -> bool:
    """True for a file name anonymize wrote (also with an upload's hash prefix in front)."""
    return bool(name) and FILE_RE.search(name or "") is not None


def strip_banner(text: str) -> tuple[str, bool]:
    """``text`` without the banner line (and the blank line after it) the export put on top."""
    if not text.startswith(BANNER):
        return text, False
    rest = text[len(BANNER) :]
    for newline in ("\r\n\r\n", "\n\n", "\r\n", "\n"):
        if rest.startswith(newline):
            return rest[len(newline) :], True
    return rest, True


__all__ = [
    "BANNER",
    "CSV_COLUMN",
    "CSV_VALUE",
    "FILE_PREFIX",
    "SUMMARY_KEY",
    "ZIP_PREFIX",
    "batch_file_name",
    "is_pseudonymized_file",
    "strip_banner",
]
