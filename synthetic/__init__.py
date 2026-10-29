"""Synthetic data for the Denial Recovery Agent (docs/SPEC.md section 12).

Everything in this package is invented: patients, providers, payers, addresses and identifiers come
from word lists in `synthetic.names` and the specialty profile under `synthetic/profiles/`. No real
patient data is ever used here, and the generators are fully deterministic for a given seed.
"""

from __future__ import annotations

__all__ = ["names", "profile", "x12_writer", "generate", "followup"]
