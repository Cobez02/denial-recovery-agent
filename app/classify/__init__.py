"""Denial classification (milestone M4): rules first, the model only for ambiguous cases.

``classify(ctx, guard_call)`` is the single entry point used by the pipeline (docs/SPEC.md
section 7). The rules engine always runs. The model is consulted, through the guard, only when
``guard_call`` is given and the rules are not confident (below ``CLASSIFY_MODEL_THRESHOLD``),
landed on ``other``, or a payer document is attached. The more confident of the two answers
wins; when both ran the result carries ``source=rules+model``, the winning rule id and the
model's rationale, and ``injection_detected`` is true if either side flagged it. A model that
cannot answer leaves the rules result in place. Payer documents are untrusted: when one is
flagged, or the model reports ``injection_detected``, the model may never override the rules
category (``app.classify.model_classifier.merge_results``).
"""

from __future__ import annotations

from app.classify.codes import all_codes, carc_text, describe, enrich_reasons, rarc_text
from app.classify.engine import classify_reason, classify_rules, load_rules
from app.classify.model_classifier import (
    CLASSIFY_SCHEMA,
    PROMPT_NAME,
    GuardCall,
    build_variables,
    classify_with_model,
    merge_results,
    query_model,
)
from app.config import get_settings
from app.enums import DenialCategory
from app.schemas import ClassificationResult, DenialContext


def should_consult_model(ctx: DenialContext, rules: ClassificationResult, threshold: float) -> bool:
    """Model consultation rule: low rules confidence, category ``other``, or any payer document."""
    return rules.confidence < threshold or rules.category == DenialCategory.OTHER or bool(ctx.documents)


def classify(
    ctx: DenialContext,
    guard_call: GuardCall | None = None,
    *,
    threshold: float | None = None,
) -> ClassificationResult:
    """Classify one denied claim: rules first, then the model when it is worth asking.

    ``guard_call(prompt_name, variables, schema=None) -> dict | str`` is the guarded model path
    (``app.llm.guard.make_guard_call``); ``None`` means rules only. ``threshold`` defaults to
    ``settings.classify_model_threshold``.
    """
    rules = classify_rules(ctx)
    if guard_call is None:
        return rules
    if threshold is None:
        threshold = get_settings().classify_model_threshold
    if not should_consult_model(ctx, rules, threshold):
        return rules
    return classify_with_model(ctx, guard_call, rules_result=rules)


__all__ = [
    "CLASSIFY_SCHEMA",
    "PROMPT_NAME",
    "GuardCall",
    "all_codes",
    "build_variables",
    "carc_text",
    "classify",
    "classify_reason",
    "classify_rules",
    "classify_with_model",
    "describe",
    "enrich_reasons",
    "load_rules",
    "merge_results",
    "query_model",
    "rarc_text",
    "should_consult_model",
]
