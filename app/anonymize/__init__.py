"""Pseudonymize one raw clinic batch for local work on real data (docs/SPEC.md section 16).

``anonymize_batch(in_dir, out_dir, key_path, options)`` is the entry point behind
``denial-agent anonymize``: it writes a batch the app processes exactly like the original (same
matches, same work items, categories and actions) with every patient identifier replaced, and a
KEY.json that is the only place an original value is written. A pseudonymized batch that keeps
dates of service is still a HIPAA limited data set, so it is still PHI. ``restore_packages`` (behind
``denial-agent deanonymize``) puts the real identifiers back into export packages, with the key.
"""

from app.anonymize.batch import (
    AnonymizeOptions,
    AnonymizeResult,
    ResidualFinding,
    anonymize_batch,
    check_paths,
    restore_claim_row,
)
from app.anonymize.pseudonyms import AnonymizeError, PseudonymKey, load_key
from app.anonymize.restore import RestoreResult, restore_packages

__all__ = [
    "AnonymizeError",
    "AnonymizeOptions",
    "AnonymizeResult",
    "PseudonymKey",
    "ResidualFinding",
    "RestoreResult",
    "anonymize_batch",
    "check_paths",
    "load_key",
    "restore_claim_row",
    "restore_packages",
]
