"""Ingestion (milestone M3): X12 tokenizer, 835 parser, claim-export importer, remittance import,
remittance-to-claim matching and denial detection.

Entry points live in the submodules:

* ``app.ingest.parse_835.parse_835(text)``
* ``app.ingest.claims_import.import_claims_csv / import_notes_csv / import_payer_documents_csv``
* ``app.ingest.remit_import.import_835``
* ``app.ingest.matching.match_remittance_claim``
* ``app.ingest.denials.detect / denial_reasons_for``

This package module holds the pieces every importer shares: reading a path-or-text source, hashing
it, and the ``ImportBatch`` lifecycle (duplicate check, start, finish with one audit event). Logs
and batch errors carry ids, counts and row numbers only.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.enums import ActorType, ImportStatus
from app.models import ImportBatch

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SourceText:
    """A file read for import: the bytes that were hashed, their text, and a display name."""

    raw: bytes
    text: str
    name: str


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_source(source: str | Path, *, default_name: str, inline_marker: str | None = None) -> SourceText:
    """Accept a path or the file content itself.

    A ``str`` is treated as content when it contains a newline or starts with ``inline_marker``
    (``"ISA"`` for an 835); anything else is a path. Files are decoded as UTF-8 (BOM tolerated);
    undecodable bytes are replaced rather than aborting the import.
    """
    if isinstance(source, Path):
        raw = source.read_bytes()
        return SourceText(raw=raw, text=raw.decode("utf-8-sig", errors="replace"), name=source.name)
    text = source.lstrip("﻿")  # an inline string may still carry the BOM of the file it came from
    looks_inline = "\n" in text or (inline_marker is not None and text.lstrip().startswith(inline_marker))
    if looks_inline:
        return SourceText(raw=source.encode("utf-8"), text=text, name=default_name)
    path = Path(source)
    raw = path.read_bytes()
    return SourceText(raw=raw, text=raw.decode("utf-8-sig", errors="replace"), name=path.name)


def find_completed_batch(session: Session, kind: str, file_sha256: str) -> ImportBatch | None:
    """A previous successful import of the same bytes, if any (failed batches may be retried)."""
    stmt = (
        select(ImportBatch)
        .where(
            ImportBatch.kind == kind,
            ImportBatch.file_sha256 == file_sha256,
            ImportBatch.status == str(ImportStatus.DONE),
        )
        .order_by(ImportBatch.id.desc())
    )
    return session.execute(stmt).scalars().first()


def begin_batch(
    session: Session, *, kind: str, file_name: str, file_sha256: str, created_by: str
) -> ImportBatch:
    batch = ImportBatch(
        kind=kind,
        file_name=file_name[:255],
        file_sha256=file_sha256,
        status=str(ImportStatus.PENDING),
        counts={},
        created_by=created_by[:80],
    )
    session.add(batch)
    session.flush()
    return batch


def complete_batch(
    session: Session,
    batch: ImportBatch,
    *,
    status: ImportStatus | str,
    counts: dict[str, Any],
    created_by: str,
    error: str | None = None,
    extra_details: dict[str, Any] | None = None,
) -> ImportBatch:
    """Close the batch and record its single audit event (``import_completed`` or ``import_failed``)."""
    batch.status = str(status)
    batch.counts = dict(counts)
    batch.error = error
    batch.finished_at = datetime.now(UTC)
    session.flush()
    action = "import_failed" if batch.status == str(ImportStatus.FAILED) else "import_completed"
    details: dict[str, Any] = {**counts, "status": batch.status, "kind": batch.kind}
    if extra_details:
        details.update(extra_details)
    audit.record(
        session,
        actor_type=ActorType.SYSTEM,
        actor_id=created_by,
        action=action,
        entity_type="import_batch",
        entity_id=batch.id,
        reason=error,
        details=details,
    )
    log.info("import batch %s kind=%s status=%s counts=%s", batch.id, batch.kind, batch.status, dict(counts))
    return batch


def duplicate_batch(
    session: Session, *, kind: str, file_name: str, file_sha256: str, created_by: str
) -> ImportBatch:
    """Record that a file was seen before. Nothing else is written."""
    batch = begin_batch(
        session, kind=kind, file_name=file_name, file_sha256=file_sha256, created_by=created_by
    )
    return complete_batch(session, batch, status=ImportStatus.DUPLICATE, counts={}, created_by=created_by)


__all__ = [
    "SourceText",
    "begin_batch",
    "complete_batch",
    "duplicate_batch",
    "find_completed_batch",
    "read_source",
    "sha256_hex",
]
