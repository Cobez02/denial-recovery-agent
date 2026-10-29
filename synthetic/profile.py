"""Specialty profile: everything the synthetic generator knows about one specialty.

A profile is a YAML file under `synthetic/profiles/` (docs/SPEC.md section 12). It lists the payers
with their filing windows, the fee schedule (E/M codes, procedures, drugs with NDCs), modifiers,
diagnoses, encounter templates (which codes one visit combines) and denial scenarios (which
CARC/RARC to emit at which level, the ground-truth category and expected action, note templates and
payer letter templates). `load_profile` validates the file and returns an immutable `Profile`.

Nothing here is real: payers, providers, addresses and identifiers are fictional.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from app.enums import ActionType, DenialCategory

PROFILES_DIR = Path(__file__).resolve().parent / "profiles"
DEFAULT_PROFILE = "dermatology"

LEVELS: tuple[str, ...] = ("claim", "line")
AMOUNT_RULES: tuple[str, ...] = ("full", "allowed")
LINE_ROLES: tuple[str, ...] = ("em", "procedure", "addon", "drug", "path")
TIMELY_FILING_MODES: tuple[str, ...] = ("inside", "outside")
PRIOR_AUTH_MODES: tuple[str, ...] = ("present", "absent")
GROUP_CODES: tuple[str, ...] = ("CO", "PR", "OA", "PI", "CR")

# Luhn contribution of the constant "80840" prefix that precedes every NPI.
_NPI_PREFIX_LUHN = 24


# --------------------------------------------------------------------------------------
# NPI check digit (Luhn with the 80840 prefix)
# --------------------------------------------------------------------------------------


def npi_check_digit(base: str) -> str:
    """Return the Luhn check digit for a nine-digit NPI base (e.g. '123456789' -> '3')."""
    digits = str(base).strip()
    if len(digits) != 9 or not digits.isdigit():
        raise ValueError("NPI base must be exactly nine digits")
    total = _NPI_PREFIX_LUHN
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 0:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return str((10 - total % 10) % 10)


def make_npi(base: str) -> str:
    """Complete a nine-digit base into a ten-digit NPI."""
    return f"{str(base).strip()}{npi_check_digit(base)}"


def is_valid_npi(npi: str) -> bool:
    value = str(npi).strip()
    return len(value) == 10 and value.isdigit() and npi_check_digit(value[:9]) == value[9]


# --------------------------------------------------------------------------------------
# Profile dataclasses
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class PayerSpec:
    payer_id: str
    name: str
    timely_filing_days: int
    corrected_claim_days: int
    appeal_days: int
    member_id_prefix: str
    filing_indicator: str
    tax_id: str
    address: dict[str, str]
    phone: str
    allowed_ratio: Decimal
    weight: float
    patient_responsibility_rate: float
    copay_amounts: tuple[Decimal, ...]


@dataclass(frozen=True)
class ProviderSpec:
    first_name: str
    last_name: str
    credential: str
    npi: str
    weight: float = 1.0

    @property
    def name(self) -> str:
        return f"{self.first_name} {self.last_name}, {self.credential}"


@dataclass(frozen=True)
class CodeSpec:
    code: str
    description: str
    charge: Decimal
    role: str  # em | procedure | addon | drug | path
    kind: str  # em, biopsy, destruction, surgery, drug, phototherapy, ... (free text used by scenarios)
    ndc: str | None = None
    units: tuple[int, int] = (1, 1)


@dataclass(frozen=True)
class DiagnosisSpec:
    code: str
    description: str
    sites: tuple[str, ...]
    lesion: str


@dataclass(frozen=True)
class ProcedureSlot:
    codes: tuple[str, ...]
    probability: float = 1.0
    modifiers: tuple[str, ...] = ()
    modifier_probability: float = 1.0
    dx_group: int = 1


@dataclass(frozen=True)
class EncounterTemplate:
    id: str
    weight: float
    tags: tuple[str, ...]
    em_codes: tuple[str, ...]
    em_probability: float
    em_modifier_25: bool
    procedures: tuple[ProcedureSlot, ...]
    diagnosis_groups: tuple[tuple[str, ...], ...]

    @property
    def diagnosis_codes(self) -> tuple[str, ...]:
        seen: list[str] = []
        for group in self.diagnosis_groups:
            for code in group:
                if code not in seen:
                    seen.append(code)
        return tuple(seen)


@dataclass(frozen=True)
class TargetSpec:
    roles: tuple[str, ...] = ()
    kinds: tuple[str, ...] = ()
    codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScenarioSpec:
    id: str
    description: str
    category: DenialCategory
    expected_action: ActionType
    weight: float
    carc: str
    rarcs: tuple[str, ...]
    group: str
    level: str
    amount: str
    target: TargetSpec
    requires_tags: tuple[str, ...]
    claim_setup: dict[str, Any]
    notes: tuple[str, ...]
    note_count: tuple[int, int]
    letter: str | None
    letter_required: bool
    no_appeal_rights: bool
    allow_benign_notes: bool


@dataclass(frozen=True)
class Profile:
    name: str
    specialty: str
    place_of_service: str
    clinic: dict[str, Any]
    payers: tuple[PayerSpec, ...]
    providers: tuple[ProviderSpec, ...]
    referring_providers: tuple[ProviderSpec, ...]
    referring_rate: float
    codes: dict[str, CodeSpec]
    modifiers: tuple[str, ...]
    diagnoses: dict[str, DiagnosisSpec]
    encounters: tuple[EncounterTemplate, ...]
    scenarios: tuple[ScenarioSpec, ...]
    code_texts: dict[str, str]
    benign_notes: tuple[str, ...]
    letter_template: str
    portal_template: str
    appeal_rights_text: str
    corrected_claim_rights_text: str
    no_appeal_rights_text: str
    letter_rate: float
    portal_rate: float
    benign_note_rate: float
    source: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False, compare=False)

    # -- lookups ---------------------------------------------------------------------

    def payer(self, payer_id: str) -> PayerSpec:
        for payer in self.payers:
            if payer.payer_id == payer_id:
                return payer
        raise KeyError(payer_id)

    def scenario(self, scenario_id: str) -> ScenarioSpec:
        for scenario in self.scenarios:
            if scenario.id == scenario_id:
                return scenario
        raise KeyError(scenario_id)

    def scenarios_by_category(self) -> dict[DenialCategory, list[ScenarioSpec]]:
        """Scenarios grouped by ground-truth category, in profile order."""
        grouped: dict[DenialCategory, list[ScenarioSpec]] = {}
        for scenario in self.scenarios:
            grouped.setdefault(scenario.category, []).append(scenario)
        return grouped

    def code_text(self, code: str) -> str:
        return self.code_texts.get(code, "")

    @property
    def em_codes(self) -> tuple[str, ...]:
        return tuple(c.code for c in self.codes.values() if c.role == "em")

    @property
    def encounter_tags(self) -> tuple[str, ...]:
        tags: list[str] = []
        for enc in self.encounters:
            for tag in enc.tags:
                if tag not in tags:
                    tags.append(tag)
        return tuple(tags)


# --------------------------------------------------------------------------------------
# Loading and validation
# --------------------------------------------------------------------------------------


def resolve_profile_path(name_or_path: str | Path = DEFAULT_PROFILE) -> Path:
    """A bare name resolves under synthetic/profiles/; anything with a .yaml suffix is a path."""
    text = str(name_or_path)
    candidate = Path(text)
    if candidate.suffix.lower() in (".yaml", ".yml"):
        if not candidate.exists():
            raise FileNotFoundError(f"profile file not found: {candidate.name}")
        return candidate
    packaged = PROFILES_DIR / f"{text}.yaml"
    if packaged.exists():
        return packaged
    raise FileNotFoundError(f"profile not found: {text}")


def load_profile(name_or_path: str | Path = DEFAULT_PROFILE) -> Profile:
    path = resolve_profile_path(name_or_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"profile {path.name} must be a mapping")
    return build_profile(raw, source=str(path))


def _dec(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def _tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(str(v) for v in value)


def _int_range(value: Any, default: tuple[int, int]) -> tuple[int, int]:
    if value is None:
        return default
    if isinstance(value, int):
        return (value, value)
    lo, hi = (int(value[0]), int(value[1]))
    if lo > hi:
        raise ValueError("range low must not exceed high")
    return (lo, hi)


def _require(mapping: dict[str, Any], key: str, where: str) -> Any:
    if key not in mapping:
        raise ValueError(f"profile {where}: missing '{key}'")
    return mapping[key]


def _build_payer(raw: dict[str, Any]) -> PayerSpec:
    where = f"payer {raw.get('payer_id', '?')}"
    allowed_ratio = Decimal(str(raw.get("allowed_ratio", "0.70")))
    if not 0 < allowed_ratio <= 1:
        raise ValueError(f"profile {where}: allowed_ratio must be above 0 and at most 1")
    return PayerSpec(
        payer_id=str(_require(raw, "payer_id", where)),
        name=str(_require(raw, "name", where)),
        timely_filing_days=int(_require(raw, "timely_filing_days", where)),
        corrected_claim_days=int(_require(raw, "corrected_claim_days", where)),
        appeal_days=int(_require(raw, "appeal_days", where)),
        member_id_prefix=str(_require(raw, "member_id_prefix", where)),
        filing_indicator=str(raw.get("filing_indicator", "12")),
        tax_id=str(raw.get("tax_id", "")),
        address={k: str(v) for k, v in (raw.get("address") or {}).items()},
        phone=str(raw.get("phone", "")),
        allowed_ratio=allowed_ratio,
        weight=float(raw.get("weight", 1.0)),
        patient_responsibility_rate=float(raw.get("patient_responsibility_rate", 0.0)),
        copay_amounts=tuple(_dec(v) for v in (raw.get("copay_amounts") or [])),
    )


def _build_provider(raw: dict[str, Any]) -> ProviderSpec:
    where = f"provider {raw.get('last_name', '?')}"
    npi = str(_require(raw, "npi", where))
    if not is_valid_npi(npi):
        raise ValueError(f"profile {where}: NPI fails the check digit")
    return ProviderSpec(
        first_name=str(_require(raw, "first_name", where)),
        last_name=str(_require(raw, "last_name", where)),
        credential=str(raw.get("credential", "MD")),
        npi=npi,
        weight=float(raw.get("weight", 1.0)),
    )


def _build_code(code: str, raw: dict[str, Any]) -> CodeSpec:
    where = f"code {code}"
    role = str(raw.get("role", "procedure"))
    if role not in LINE_ROLES:
        raise ValueError(f"profile {where}: unknown role '{role}'")
    return CodeSpec(
        code=str(code),
        description=str(_require(raw, "description", where)),
        charge=_dec(_require(raw, "charge", where)),
        role=role,
        kind=str(raw.get("kind", role)),
        ndc=str(raw["ndc"]) if raw.get("ndc") else None,
        units=_int_range(raw.get("units"), (1, 1)),
    )


def _build_diagnosis(code: str, raw: dict[str, Any]) -> DiagnosisSpec:
    where = f"diagnosis {code}"
    return DiagnosisSpec(
        code=str(code),
        description=str(_require(raw, "description", where)),
        sites=_tuple(raw.get("sites")) or ("skin",),
        lesion=str(raw.get("lesion", "lesion")),
    )


def _build_encounter(
    raw: dict[str, Any], codes: dict[str, CodeSpec], diagnoses: dict[str, DiagnosisSpec]
) -> EncounterTemplate:
    where = f"encounter {raw.get('id', '?')}"
    em_codes = _tuple(raw.get("em_codes"))
    for code in em_codes:
        if code not in codes or codes[code].role != "em":
            raise ValueError(f"profile {where}: '{code}' is not an E/M code in the fee schedule")
    slots: list[ProcedureSlot] = []
    for slot_raw in raw.get("procedures") or []:
        slot_codes = _tuple(slot_raw.get("codes"))
        if not slot_codes:
            raise ValueError(f"profile {where}: procedure slot without codes")
        for code in slot_codes:
            if code not in codes:
                raise ValueError(f"profile {where}: unknown code '{code}'")
        slots.append(
            ProcedureSlot(
                codes=slot_codes,
                probability=float(slot_raw.get("probability", 1.0)),
                modifiers=_tuple(slot_raw.get("modifiers")),
                modifier_probability=float(slot_raw.get("modifier_probability", 1.0)),
                dx_group=int(slot_raw.get("dx_group", 1)),
            )
        )
    if not em_codes and not slots:
        raise ValueError(f"profile {where}: needs an E/M code or a procedure slot")
    groups_raw = raw.get("diagnosis_groups") or []
    if not groups_raw:
        raise ValueError(f"profile {where}: needs at least one diagnosis group")
    groups: list[tuple[str, ...]] = []
    for group in groups_raw:
        group_codes = _tuple(group)
        for code in group_codes:
            if code not in diagnoses:
                raise ValueError(f"profile {where}: unknown diagnosis '{code}'")
        groups.append(group_codes)
    for slot in slots:
        if not 1 <= slot.dx_group <= len(groups):
            raise ValueError(f"profile {where}: dx_group {slot.dx_group} out of range")
    return EncounterTemplate(
        id=str(_require(raw, "id", where)),
        weight=float(raw.get("weight", 1.0)),
        tags=_tuple(raw.get("tags")),
        em_codes=em_codes,
        em_probability=float(raw.get("em_probability", 1.0 if em_codes else 0.0)),
        em_modifier_25=bool(raw.get("em_modifier_25", False)),
        procedures=tuple(slots),
        diagnosis_groups=tuple(groups),
    )


def _build_scenario(
    raw: dict[str, Any], codes: dict[str, CodeSpec], known_tags: tuple[str, ...]
) -> ScenarioSpec:
    where = f"scenario {raw.get('id', '?')}"
    try:
        category = DenialCategory(str(_require(raw, "category", where)))
    except ValueError as exc:
        raise ValueError(f"profile {where}: unknown category") from exc
    try:
        action = ActionType(str(_require(raw, "expected_action", where)))
    except ValueError as exc:
        raise ValueError(f"profile {where}: unknown expected_action") from exc
    level = str(raw.get("level", "claim"))
    if level not in LEVELS:
        raise ValueError(f"profile {where}: level must be one of {LEVELS}")
    amount = str(raw.get("amount", "full"))
    if amount not in AMOUNT_RULES:
        raise ValueError(f"profile {where}: amount must be one of {AMOUNT_RULES}")
    group = str(raw.get("group", "CO"))
    if group not in GROUP_CODES:
        raise ValueError(f"profile {where}: group must be one of {GROUP_CODES}")
    target_raw = raw.get("target") or {}
    target = TargetSpec(
        roles=_tuple(target_raw.get("roles")),
        kinds=_tuple(target_raw.get("kinds")),
        codes=_tuple(target_raw.get("codes")),
    )
    for role in target.roles:
        if role not in LINE_ROLES:
            raise ValueError(f"profile {where}: unknown target role '{role}'")
    for code in target.codes:
        if code not in codes:
            raise ValueError(f"profile {where}: unknown target code '{code}'")
    requires_tags = _tuple(raw.get("requires_tags"))
    for tag in requires_tags:
        if tag not in known_tags:
            raise ValueError(f"profile {where}: no encounter carries tag '{tag}'")
    setup = dict(raw.get("claim_setup") or {})
    if setup.get("timely_filing") not in (None, *TIMELY_FILING_MODES):
        raise ValueError(f"profile {where}: timely_filing must be one of {TIMELY_FILING_MODES}")
    if setup.get("prior_auth") not in (None, *PRIOR_AUTH_MODES):
        raise ValueError(f"profile {where}: prior_auth must be one of {PRIOR_AUTH_MODES}")
    notes = _tuple(raw.get("notes"))
    note_count = _int_range(raw.get("note_count"), (1, min(3, len(notes))) if notes else (0, 0))
    if notes and note_count[1] > len(notes):
        raise ValueError(f"profile {where}: note_count exceeds the number of note templates")
    return ScenarioSpec(
        id=str(_require(raw, "id", where)),
        description=str(raw.get("description", "")),
        category=category,
        expected_action=action,
        weight=float(raw.get("weight", 1.0)),
        carc=str(_require(raw, "carc", where)),
        rarcs=_tuple(raw.get("rarcs")),
        group=group,
        level=level,
        amount=amount,
        target=target,
        requires_tags=requires_tags,
        claim_setup=setup,
        notes=notes,
        note_count=note_count,
        letter=str(raw["letter"]).strip() if raw.get("letter") else None,
        letter_required=bool(raw.get("letter_required", False)),
        no_appeal_rights=bool(raw.get("no_appeal_rights", False)),
        allow_benign_notes=bool(raw.get("allow_benign_notes", True)),
    )


def build_profile(raw: dict[str, Any], *, source: str = "") -> Profile:
    """Turn the parsed YAML mapping into a validated Profile. Raises ValueError on problems."""
    name = str(_require(raw, "name", "root"))
    payers = tuple(_build_payer(p) for p in _require(raw, "payers", "root"))
    if not payers:
        raise ValueError("profile root: needs at least one payer")
    if len({p.payer_id for p in payers}) != len(payers):
        raise ValueError("profile root: payer ids must be unique")
    providers = tuple(_build_provider(p) for p in _require(raw, "providers", "root"))
    if not providers:
        raise ValueError("profile root: needs at least one rendering provider")
    referring = tuple(_build_provider(p) for p in raw.get("referring_providers") or [])
    clinic = dict(_require(raw, "clinic", "root"))
    if not is_valid_npi(str(clinic.get("npi", ""))):
        raise ValueError("profile clinic: NPI fails the check digit")
    codes = {str(k): _build_code(str(k), v) for k, v in _require(raw, "codes", "root").items()}
    if not any(c.role == "em" for c in codes.values()):
        raise ValueError("profile root: fee schedule needs at least one E/M code")
    diagnoses = {str(k): _build_diagnosis(str(k), v) for k, v in _require(raw, "diagnoses", "root").items()}
    encounters = tuple(_build_encounter(e, codes, diagnoses) for e in _require(raw, "encounters", "root"))
    if not encounters:
        raise ValueError("profile root: needs at least one encounter template")
    if len({e.id for e in encounters}) != len(encounters):
        raise ValueError("profile root: encounter ids must be unique")
    known_tags: list[str] = []
    for enc in encounters:
        for tag in enc.tags:
            if tag not in known_tags:
                known_tags.append(tag)
    scenarios = tuple(
        _build_scenario(s, codes, tuple(known_tags)) for s in _require(raw, "scenarios", "root")
    )
    if not scenarios:
        raise ValueError("profile root: needs at least one denial scenario")
    if len({s.id for s in scenarios}) != len(scenarios):
        raise ValueError("profile root: scenario ids must be unique")
    code_texts = {str(k): str(v) for k, v in (raw.get("code_texts") or {}).items()}
    for scenario in scenarios:
        for code in (scenario.carc, *scenario.rarcs):
            if code not in code_texts:
                raise ValueError(f"profile scenario {scenario.id}: no code text for '{code}'")
    letters = raw.get("letters") or {}
    return Profile(
        name=name,
        specialty=str(raw.get("specialty", name)),
        place_of_service=str(raw.get("place_of_service", "11")),
        clinic=clinic,
        payers=payers,
        providers=providers,
        referring_providers=referring,
        referring_rate=float(raw.get("referring_rate", 0.0)),
        codes=codes,
        modifiers=_tuple(raw.get("modifiers")),
        diagnoses=diagnoses,
        encounters=encounters,
        scenarios=scenarios,
        code_texts=code_texts,
        benign_notes=_tuple(raw.get("benign_notes")),
        letter_template=str(letters.get("letter_template", "{body}")),
        portal_template=str(letters.get("portal_template", "{body}")),
        appeal_rights_text=str(letters.get("appeal_rights", "")),
        corrected_claim_rights_text=str(letters.get("corrected_claim_rights", "")),
        no_appeal_rights_text=str(letters.get("no_appeal_rights", "")),
        letter_rate=float(letters.get("letter_rate", 0.40)),
        portal_rate=float(letters.get("portal_rate", 0.20)),
        benign_note_rate=float(raw.get("benign_note_rate", 0.30)),
        source=source,
        raw=raw,
    )


__all__ = [
    "AMOUNT_RULES",
    "CodeSpec",
    "DEFAULT_PROFILE",
    "DiagnosisSpec",
    "EncounterTemplate",
    "LEVELS",
    "PROFILES_DIR",
    "PayerSpec",
    "ProcedureSlot",
    "Profile",
    "ProviderSpec",
    "ScenarioSpec",
    "TargetSpec",
    "build_profile",
    "is_valid_npi",
    "load_profile",
    "make_npi",
    "npi_check_digit",
    "resolve_profile_path",
]
