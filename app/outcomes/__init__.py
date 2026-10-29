"""Outcomes (M8): match later 835s to submitted fixes (and take-backs of closed ones), compute metrics."""

from app.outcomes.matching import (
    latest_outcome,
    record_outcome,
    record_outcomes_for_remittance,
    takeback_candidates,
)
from app.outcomes.metrics import category_agreement, compute_dashboard

__all__ = [
    "category_agreement",
    "compute_dashboard",
    "latest_outcome",
    "record_outcome",
    "record_outcomes_for_remittance",
    "takeback_candidates",
]
