"""Category -> playbook registry.

The category modules are imported lazily on first use so `app.playbooks` stays cheap to import and
free of circular imports. `register()` lets a test or a specialty profile replace a playbook.
"""

from __future__ import annotations

from app.enums import DenialCategory
from app.playbooks.base import Playbook

_PLAYBOOKS: dict[DenialCategory, Playbook] | None = None


def _build() -> dict[DenialCategory, Playbook]:
    from app.playbooks.authorization import AuthorizationPlaybook
    from app.playbooks.bundling import BundlingPlaybook
    from app.playbooks.coverage import CoordinationOfBenefitsPlaybook, EligibilityPlaybook
    from app.playbooks.duplicate import DuplicatePlaybook
    from app.playbooks.fallback import ContractualPlaybook, OtherPlaybook, PatientResponsibilityPlaybook
    from app.playbooks.medical_necessity import MedicalNecessityPlaybook
    from app.playbooks.missing_information import MissingInformationPlaybook
    from app.playbooks.modifier import ModifierPlaybook
    from app.playbooks.timely_filing import TimelyFilingPlaybook

    books: list[Playbook] = [
        EligibilityPlaybook(),
        AuthorizationPlaybook(),
        ModifierPlaybook(),
        DuplicatePlaybook(),
        TimelyFilingPlaybook(),
        MedicalNecessityPlaybook(),
        BundlingPlaybook(),
        CoordinationOfBenefitsPlaybook(),
        MissingInformationPlaybook(),
        ContractualPlaybook(),
        PatientResponsibilityPlaybook(),
        OtherPlaybook(),
    ]
    registry = {book.category: book for book in books}
    missing = [c for c in DenialCategory if c not in registry]
    if missing:  # pragma: no cover - every category must have a playbook
        raise RuntimeError(f"no playbook registered for: {', '.join(missing)}")
    return registry


def all_playbooks() -> dict[DenialCategory, Playbook]:
    global _PLAYBOOKS
    if _PLAYBOOKS is None:
        _PLAYBOOKS = _build()
    return _PLAYBOOKS


def get_playbook(category: DenialCategory | str) -> Playbook:
    """The playbook for a category; unknown values fall back to the `other` playbook."""
    registry = all_playbooks()
    try:
        return registry[DenialCategory(category)]
    except (KeyError, ValueError):
        return registry[DenialCategory.OTHER]


def register(playbook: Playbook) -> None:
    """Replace the playbook for `playbook.category` (tests, specialty overrides)."""
    all_playbooks()[DenialCategory(playbook.category)] = playbook


def reset() -> None:
    """Drop overrides; the next lookup rebuilds the default registry."""
    global _PLAYBOOKS
    _PLAYBOOKS = None


__all__ = ["all_playbooks", "get_playbook", "register", "reset"]
