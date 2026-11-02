"""Marking what comes from a pseudonymized batch (docs/SPEC.md section 16).

`denial-agent anonymize` writes one extra, last column into the pseudonymized claim-export CSV:
:data:`IMPORT_COLUMN`, with :data:`IMPORT_VALUE` on every row. The claims importer records that
column on every claim of the import (``Claim.extra[EXTRA_KEY] = True``; a later import of the same
claim without the column keeps the mark), so the export (``app/review/export.py``) knows a claim came
from a pseudonymized batch whatever the file was called when it was imported, and without a schema
change. anonymize also names every file it writes ``pseudonymized_run<N>_<kind>_<n>.<ext>``; a
claims import whose file name matches :data:`FILE_RE` (an upload keeps it after its hash prefix)
still marks its claims, for batches imported before the column existed. The package then says so
wherever a person looks: the zip name starts with ``PSEUDONYMIZED_``, the letter and the clinic
question open with :data:`BANNER`, ``summary.json`` carries ``"pseudonymized": true`` and the
corrected claim CSV a :data:`CSV_COLUMN` column. ``denial-agent deanonymize`` removes every one of
them.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

FILE_PREFIX = "pseudonymized_"
FILE_RE = re.compile(r"(?:^|[_\-.])pseudonymized_run\d+_", re.IGNORECASE)
IMPORT_COLUMN = "pseudonymized_batch"
IMPORT_VALUE = "yes"
EXTRA_KEY = "pseudonymized"
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


def is_pseudonymized_header(columns: Iterable[str]) -> bool:
    """True for a claim-export header that carries the marker column anonymize writes."""
    return any(column.strip().casefold() == IMPORT_COLUMN for column in columns)


def is_marked_claim(extra: Mapping[str, Any] | None) -> bool:
    """True when the importer recorded the marker column on the claim (``Claim.extra``)."""
    return bool((extra or {}).get(EXTRA_KEY))


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
    "EXTRA_KEY",
    "FILE_PREFIX",
    "IMPORT_COLUMN",
    "IMPORT_VALUE",
    "SUMMARY_KEY",
    "ZIP_PREFIX",
    "batch_file_name",
    "is_marked_claim",
    "is_pseudonymized_file",
    "is_pseudonymized_header",
    "strip_banner",
]
