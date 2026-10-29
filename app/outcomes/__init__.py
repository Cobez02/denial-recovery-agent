"""Outcomes (M8): match later 835s to submitted fixes and compute dashboard metrics."""

from app.outcomes.matching import record_outcome, record_outcomes_for_remittance
from app.outcomes.metrics import category_agreement, compute_dashboard

__all__ = ["record_outcome", "record_outcomes_for_remittance", "compute_dashboard", "category_agreement"]
