"""Rules engine: CARC/RARC combinations -> denial category (docs/SPEC.md section 7).

``rules.yaml`` is an ordered list of rules. For every denial reason on the remittance claim the
engine walks the list top to bottom and the first rule whose ``match`` block fits wins; a reason
that matches nothing is ``other`` at confidence 0.30 with rule id ``unmatched``. RARC matching
tries the reason's own remark codes first; the claim-level remark codes of the remittance (a
remark after the CLP applies to the whole claim) are added when the reason's own codes did not
select a remark-specific rule, and both sets are cited in the rationale.

``classify_rules`` turns the per-reason matches into one ``ClassificationResult`` for the work
item; the selection is deterministic and documented on that function.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.classify.codes import normalize_code
from app.enums import ACTIONABLE_CATEGORIES, CATEGORY_PRIORITY, ClassificationSource, DenialCategory
from app.schemas import ClassificationResult, DenialContext, DenialReason, RemittanceContext

log = logging.getLogger(__name__)

RULES_PATH = Path(__file__).resolve().parent / "rules.yaml"

UNMATCHED_RULE_ID = "unmatched"
UNMATCHED_CONFIDENCE = 0.30
UNMATCHED_RATIONALE = "no classification rule matches this code; needs review"
NO_REASONS_RATIONALE = "no denial reasons on the remittance claim; needs review"
MIXED_CATEGORY_PENALTY = 0.10
CONFIDENCE_FLOOR = 0.05
AMOUNT_SHARE_THRESHOLD = Decimal("0.2")

_LEVELS = frozenset({"claim", "line", "any"})
_MATCH_KEYS = frozenset(
    {
        "carc",
        "rarc_any",
        "rarc_none",
        "group",
        "level",
        "claim_paid",
        "amount_positive",
        "procedure_codes",
        "modifiers_any",
        "modifiers_none",
    }
)
_RULE_KEYS = frozenset({"id", "category", "confidence", "match", "rationale"})
_PRIORITY_INDEX: dict[DenialCategory, int] = {c: i for i, c in enumerate(CATEGORY_PRIORITY)}
_CODE_RANGE = re.compile(r"^(\d{4,5})-(\d{4,5})$")


def _money(amount: Any) -> str:
    try:
        return f"{Decimal(amount or 0):.2f}"
    except (ValueError, TypeError, ArithmeticError):
        return "0.00"


def _group(value: Any) -> str:
    return str(value or "").strip().upper()


def _code_set(value: Any, *, what: str, rule_id: str) -> frozenset[str] | None:
    """Parse a code list from YAML. ``None``, ``"*"`` or a list containing ``"*"`` mean "any"."""
    if value is None or value == "*":
        return None
    if isinstance(value, str | int):
        value = [value]
    if not isinstance(value, list):
        raise ValueError(f"rules.yaml: rule {rule_id}: {what} must be a list of codes")
    codes = {normalize_code(v) for v in value}
    if "*" in codes:
        return None
    if not codes or "" in codes:
        raise ValueError(f"rules.yaml: rule {rule_id}: {what} has an empty code")
    return frozenset(codes)


def _modifier_set(value: Any, *, what: str, rule_id: str) -> frozenset[str]:
    if value is None:
        return frozenset()
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not value:
        raise ValueError(f"rules.yaml: rule {rule_id}: {what} must be a non-empty list of modifiers")
    modifiers = {str(m).strip().upper() for m in value}
    if "" in modifiers:
        raise ValueError(f"rules.yaml: rule {rule_id}: {what} has an empty modifier")
    return frozenset(modifiers)


def _procedure_spec(value: Any, *, rule_id: str) -> tuple[frozenset[str], tuple[tuple[int, int], ...]]:
    """``procedure_codes``: exact codes (``98943``, ``G0283``) and inclusive numeric ranges (``98940-98942``)."""
    if value is None:
        return frozenset(), ()
    if isinstance(value, str | int):
        value = [value]
    if not isinstance(value, list) or not value:
        raise ValueError(f"rules.yaml: rule {rule_id}: procedure_codes must be a non-empty list")
    codes: set[str] = set()
    ranges: list[tuple[int, int]] = []
    for item in value:
        text = str(item).strip().upper()
        match = _CODE_RANGE.match(text)
        if match:
            low, high = int(match.group(1)), int(match.group(2))
            if low > high:
                raise ValueError(f"rules.yaml: rule {rule_id}: procedure range {text} is reversed")
            ranges.append((low, high))
        elif text:
            codes.add(text)
        else:
            raise ValueError(f"rules.yaml: rule {rule_id}: procedure_codes has an empty code")
    return frozenset(codes), tuple(ranges)


def _procedure_in(code: str | None, codes: frozenset[str], ranges: tuple[tuple[int, int], ...]) -> bool:
    if not code:
        return False
    text = str(code).strip().upper()
    if text in codes:
        return True
    if text.isdigit():
        value = int(text)
        return any(low <= value <= high for low, high in ranges)
    return False


@dataclass(frozen=True)
class Rule:
    """One entry of rules.yaml. ``carcs``/``groups`` of ``None`` mean "any".

    ``procedure_codes``, ``modifiers_any`` and ``modifiers_none`` look at the denied line itself
    (``DenialReason.procedure_code`` / ``modifiers``), so a code the payer uses for two different
    situations (CARC 50 on a CMT line with and without modifier AT) can be told apart; a claim-level
    reason has no line and never matches a rule that names procedure codes or modifiers.
    """

    id: str
    category: DenialCategory
    confidence: float
    rationale: str
    carcs: frozenset[str] | None = None
    rarc_any: frozenset[str] = frozenset()
    rarc_none: frozenset[str] = frozenset()
    groups: frozenset[str] | None = None
    level: str = "any"
    claim_paid: bool | None = None
    amount_positive: bool | None = None
    procedure_codes: frozenset[str] = frozenset()
    procedure_ranges: tuple[tuple[int, int], ...] = ()
    modifiers_any: frozenset[str] = frozenset()
    modifiers_none: frozenset[str] = frozenset()

    def matches(
        self,
        reason: DenialReason,
        rarcs: Iterable[str],
        remittance: RemittanceContext | None = None,
    ) -> bool:
        """True when the reason (with the given effective RARCs) satisfies every match condition."""
        if self.carcs is not None and normalize_code(reason.carc) not in self.carcs:
            return False
        if self.groups is not None and _group(reason.group_code) not in self.groups:
            return False
        if self.level != "any" and reason.level != self.level:
            return False
        present = {normalize_code(r) for r in rarcs}
        if self.rarc_any and not (present & self.rarc_any):
            return False
        if self.rarc_none and (present & self.rarc_none):
            return False
        if self.amount_positive is not None:
            if (Decimal(reason.amount or 0) > 0) != self.amount_positive:
                return False
        if self.claim_paid is not None:
            if remittance is None:
                return False
            if (Decimal(remittance.paid_amount or 0) > 0) != self.claim_paid:
                return False
        if (self.procedure_codes or self.procedure_ranges) and not _procedure_in(
            reason.procedure_code, self.procedure_codes, self.procedure_ranges
        ):
            return False
        if self.modifiers_any or self.modifiers_none:
            if reason.level != "line":
                return False
            modifiers = {str(m).strip().upper() for m in reason.modifiers}
            if self.modifiers_any and not (modifiers & self.modifiers_any):
                return False
            if self.modifiers_none and (modifiers & self.modifiers_none):
                return False
        return True

    @property
    def codes(self) -> dict[str, frozenset[str]]:
        """Codes the rule references, by kind (used by the catalog coverage test)."""
        return {"carc": self.carcs or frozenset(), "rarc": self.rarc_any | self.rarc_none}


def _parse_rule(raw: Any, position: int) -> Rule:
    if not isinstance(raw, dict):
        raise ValueError(f"rules.yaml: rule #{position} is not a mapping")
    rule_id = str(raw.get("id") or "").strip()
    if not rule_id:
        raise ValueError(f"rules.yaml: rule #{position} has no id")
    unknown = set(raw) - _RULE_KEYS
    if unknown:
        raise ValueError(f"rules.yaml: rule {rule_id}: unknown keys {sorted(unknown)}")
    try:
        category = DenialCategory(str(raw.get("category")))
    except ValueError as exc:
        raise ValueError(f"rules.yaml: rule {rule_id}: unknown category") from exc
    try:
        confidence = float(raw.get("confidence"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"rules.yaml: rule {rule_id}: confidence must be a number") from exc
    if not 0.0 <= confidence <= 1.0:
        raise ValueError(f"rules.yaml: rule {rule_id}: confidence must be between 0 and 1")
    match = raw.get("match") or {}
    if not isinstance(match, dict):
        raise ValueError(f"rules.yaml: rule {rule_id}: match must be a mapping")
    unknown = set(match) - _MATCH_KEYS
    if unknown:
        raise ValueError(f"rules.yaml: rule {rule_id}: unknown match keys {sorted(unknown)}")
    level = str(match.get("level") or "any").strip().lower()
    if level not in _LEVELS:
        raise ValueError(f"rules.yaml: rule {rule_id}: level must be claim, line or any")
    groups_raw = match.get("group")
    groups: frozenset[str] | None
    if groups_raw is None or groups_raw == "*":
        groups = None
    else:
        if isinstance(groups_raw, str):
            groups_raw = [groups_raw]
        if not isinstance(groups_raw, list) or not groups_raw:
            raise ValueError(f"rules.yaml: rule {rule_id}: group must be a non-empty list")
        groups = frozenset(_group(g) for g in groups_raw)
        if "*" in groups:
            groups = None
    for flag in ("claim_paid", "amount_positive"):
        if match.get(flag) is not None and not isinstance(match.get(flag), bool):
            raise ValueError(f"rules.yaml: rule {rule_id}: {flag} must be true or false")
    rationale = str(raw.get("rationale") or "").strip()
    if not rationale:
        raise ValueError(f"rules.yaml: rule {rule_id}: rationale is required")
    procedure_codes, procedure_ranges = _procedure_spec(match.get("procedure_codes"), rule_id=rule_id)
    return Rule(
        id=rule_id,
        category=category,
        confidence=confidence,
        rationale=rationale,
        carcs=_code_set(match.get("carc"), what="carc", rule_id=rule_id),
        rarc_any=_code_set(match.get("rarc_any"), what="rarc_any", rule_id=rule_id) or frozenset(),
        rarc_none=_code_set(match.get("rarc_none"), what="rarc_none", rule_id=rule_id) or frozenset(),
        groups=groups,
        level=level,
        claim_paid=match.get("claim_paid"),
        amount_positive=match.get("amount_positive"),
        procedure_codes=procedure_codes,
        procedure_ranges=procedure_ranges,
        modifiers_any=_modifier_set(match.get("modifiers_any"), what="modifiers_any", rule_id=rule_id),
        modifiers_none=_modifier_set(match.get("modifiers_none"), what="modifiers_none", rule_id=rule_id),
    )


@lru_cache(maxsize=1)
def load_rules() -> tuple[Rule, ...]:
    """Parse and validate rules.yaml once. Raises ValueError naming the offending rule."""
    with RULES_PATH.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if isinstance(raw, dict):
        raw = raw.get("rules")
    if not isinstance(raw, list) or not raw:
        raise ValueError("rules.yaml must be a non-empty list of rules")
    rules = tuple(_parse_rule(item, i + 1) for i, item in enumerate(raw))
    seen: set[str] = set()
    for rule in rules:
        if rule.id in seen:
            raise ValueError(f"rules.yaml: duplicate rule id {rule.id}")
        seen.add(rule.id)
    return rules


@dataclass(frozen=True)
class ReasonMatch:
    """Outcome of matching one denial reason against the rules."""

    index: int
    category: DenialCategory
    confidence: float
    rule_id: str
    rationale: str
    amount: Decimal
    citation: str
    rarcs: tuple[str, ...]


def effective_rarcs(reason: DenialReason, claim_rarcs: Iterable[Any] = ()) -> tuple[str, ...]:
    """The reason's remark codes followed by claim-level remark codes it does not already carry."""
    seen: list[str] = []
    for code in [*reason.rarcs, *claim_rarcs]:
        key = normalize_code(code)
        if key and key not in seen:
            seen.append(key)
    return tuple(seen)


def citation(reason: DenialReason, rarcs: Iterable[str] = ()) -> str:
    """Code citation for a reason, e.g. ``CO-16 with RARC M119 on line 1 ($120.00)``."""
    rarcs = tuple(rarcs)
    text = f"{_group(reason.group_code) or 'XX'}-{normalize_code(reason.carc) or '?'}"
    if rarcs:
        text += " with RARC " + ", ".join(rarcs)
    if reason.line_number is not None:
        text += f" on line {reason.line_number}"
    text += f" (${_money(reason.amount)})"
    return text


def _first_match(
    reason: DenialReason, rarcs: Iterable[str], remittance: RemittanceContext | None
) -> Rule | None:
    for rule in load_rules():
        if rule.matches(reason, rarcs, remittance):
            return rule
    return None


def match_reason(
    reason: DenialReason,
    *,
    remittance: RemittanceContext | None = None,
    claim_rarcs: Iterable[Any] = (),
    index: int = 0,
) -> ReasonMatch:
    """First-match-wins lookup of one reason; falls back to ``other`` 0.30 (``unmatched``).

    The reason's own remark codes are tried first. ``claim_rarcs`` (claim-level remark codes)
    are added only when the own codes did not select a remark-specific rule, so a line that
    carries its own remark (CO-16 with M119) is explained by it even when the claim also carries
    one (N54), while a reason without remarks still benefits from the claim-level ones. The
    citation always lists both sets.
    """
    own = effective_rarcs(reason)
    rarcs = effective_rarcs(reason, claim_rarcs)
    cite = citation(reason, rarcs)
    amount = Decimal(reason.amount or 0)
    rule = _first_match(reason, own, remittance)
    if rarcs != own and (rule is None or not rule.rarc_any):
        rule = _first_match(reason, rarcs, remittance)
    if rule is not None:
        return ReasonMatch(
            index=index,
            category=rule.category,
            confidence=rule.confidence,
            rule_id=rule.id,
            rationale=f"{cite}: {rule.rationale}",
            amount=amount,
            citation=cite,
            rarcs=rarcs,
        )
    return ReasonMatch(
        index=index,
        category=DenialCategory.OTHER,
        confidence=UNMATCHED_CONFIDENCE,
        rule_id=UNMATCHED_RULE_ID,
        rationale=f"{cite}: {UNMATCHED_RATIONALE}",
        amount=amount,
        citation=cite,
        rarcs=rarcs,
    )


def classify_reason(
    reason: DenialReason,
    *,
    remittance: RemittanceContext | None = None,
    claim_rarcs: Iterable[Any] = (),
) -> tuple[DenialCategory, float, str, str]:
    """Classify one denial reason: ``(category, confidence, rule_id, rationale)``.

    ``remittance`` enables the ``claim_paid`` match condition (CARC 23 as coordination of
    benefits when the claim was not paid); without it those rules never match. ``claim_rarcs``
    are claim-level remark codes considered in addition to ``reason.rarcs``.
    """
    m = match_reason(reason, remittance=remittance, claim_rarcs=claim_rarcs)
    return m.category, m.confidence, m.rule_id, m.rationale


def classify_rules(ctx: DenialContext) -> ClassificationResult:
    """Classify a denied claim with the rules alone (source ``rules``).

    Work-item category selection, in order (deterministic for a given context):

    1. Every reason is matched on its own (``match_reason``) using its remark codes, with the
       claim-level remark codes of the remittance filling in (see ``match_reason``).
    2. Reasons whose category is in ``ACTIONABLE_CATEGORIES`` are the candidates. Among those
       whose amount is at least 20% of the largest actionable amount, the category earliest in
       ``CATEGORY_PRIORITY`` wins; within that category the largest amount (then the first listed)
       supplies the rule id, confidence and rationale. A trivial secondary adjustment never
       outranks the main denial, but a material timely-filing or eligibility reason outranks a
       larger bundling one.
    3. With no actionable reason, the largest-amount reason wins (contractual or patient
       responsibility); the pipeline creates no work item for those on their own.
    4. Confidence is the winning rule's confidence minus 0.10 when other actionable reasons map
       to a different category (floor 0.05): mixed denials deserve a second look.
    5. The rationale cites every code, winner first (e.g. ``CO-16 with RARC M119 on line 1
       ($120.00): missing or invalid NDC``); ``secondary_categories`` lists every other category
       seen, in priority order; ``injection_detected`` mirrors ``ctx.has_flagged_documents``.
    """
    claim_rarcs = list(ctx.remittance.remark_codes or [])
    matches = [
        match_reason(reason, remittance=ctx.remittance, claim_rarcs=claim_rarcs, index=i)
        for i, reason in enumerate(ctx.reasons)
    ]
    if not matches:
        return ClassificationResult(
            category=DenialCategory.OTHER,
            confidence=UNMATCHED_CONFIDENCE,
            source=ClassificationSource.RULES,
            rule_id=UNMATCHED_RULE_ID,
            rationale=NO_REASONS_RATIONALE,
            injection_detected=ctx.has_flagged_documents,
        )

    actionable = [m for m in matches if m.category in ACTIONABLE_CATEGORIES]
    priority_note = ""
    if actionable:
        largest = max(m.amount for m in actionable)
        cutoff = largest * AMOUNT_SHARE_THRESHOLD if largest > 0 else Decimal(0)
        candidates = [m for m in actionable if m.amount >= cutoff] or actionable
        winner = min(
            candidates,
            key=lambda m: (_PRIORITY_INDEX.get(m.category, len(CATEGORY_PRIORITY)), -m.amount, m.index),
        )
        mixed = any(m.category != winner.category for m in actionable)
        confidence = winner.confidence - MIXED_CATEGORY_PENALTY if mixed else winner.confidence
        if winner.amount < largest:
            bigger = min((m for m in actionable if m.amount == largest), key=lambda m: m.index)
            priority_note = (
                f" [{winner.category.value} outranks {bigger.category.value} by category priority]"
            )
    else:
        winner = min(matches, key=lambda m: (-m.amount, m.index))
        confidence = winner.confidence

    confidence = round(min(1.0, max(CONFIDENCE_FLOOR, confidence)), 3)
    ordered = [winner, *(m for m in matches if m is not winner)]
    rationale = "; ".join(m.rationale for m in ordered) + priority_note
    seen = {m.category for m in matches}
    secondary = [c for c in CATEGORY_PRIORITY if c != winner.category and c in seen]

    log.debug(
        "rules classified %d reason(s) as %s via %s (confidence %.2f)",
        len(matches),
        winner.category.value,
        winner.rule_id,
        confidence,
    )
    return ClassificationResult(
        category=winner.category,
        confidence=confidence,
        source=ClassificationSource.RULES,
        rule_id=winner.rule_id,
        rationale=rationale,
        injection_detected=ctx.has_flagged_documents,
        secondary_categories=secondary,
    )


__all__ = [
    "AMOUNT_SHARE_THRESHOLD",
    "CONFIDENCE_FLOOR",
    "MIXED_CATEGORY_PENALTY",
    "RULES_PATH",
    "UNMATCHED_CONFIDENCE",
    "UNMATCHED_RULE_ID",
    "ReasonMatch",
    "Rule",
    "citation",
    "classify_reason",
    "classify_rules",
    "effective_rarcs",
    "load_rules",
    "match_reason",
]
