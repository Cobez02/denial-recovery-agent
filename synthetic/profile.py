"""Specialty profile: everything the synthetic generator knows about one specialty.

A profile is a YAML file under `synthetic/profiles/` (docs/SPEC.md section 12). It lists the payers
with their filing windows and billing rules (modifiers a line kind carries for that payer, line kinds
the payer never pays, line kinds the clinic does not bill to that payer, codes the payer wants in
place of others, the patients' age bands, sequestration, the line kinds that carry the ordering
provider, the payer's own letter paragraphs), the fee schedule (E/M codes, procedures, drugs with
NDCs, each with optional clinical `detail` texts for the notes and the anatomic regions a text or a
code covers), modifiers, diagnoses (with their regions), a vocabulary of placeholders the note
templates draw from, encounter templates (which codes one visit combines, and which diagnoses pair
by region) and denial scenarios (which CARC/RARC to emit at which level, the ground-truth category
and expected action, the payers the scenario is realistic for, note templates and payer letter
templates). `load_profile` validates the file and returns an immutable `Profile`.

Nothing here is real: payers, providers, addresses and identifiers are fictional.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

from app.enums import ActionType, DenialCategory
from app.ingest.denials import PATIENT_RESPONSIBILITY_CARCS, PATIENT_RESPONSIBILITY_GROUP

PROFILES_DIR = Path(__file__).resolve().parent / "profiles"
DEFAULT_PROFILE = "chiropractic"

LEVELS: tuple[str, ...] = ("claim", "line")
AMOUNT_RULES: tuple[str, ...] = ("full", "allowed")
LINE_ROLES: tuple[str, ...] = ("em", "procedure", "addon", "drug", "path")
TIMELY_FILING_MODES: tuple[str, ...] = ("inside", "outside")
PRIOR_AUTH_MODES: tuple[str, ...] = ("present", "absent")
REFERRING_PROVIDER_MODES: tuple[str, ...] = ("present", "absent", "rendering")
GROUP_CODES: tuple[str, ...] = ("CO", "PR", "OA", "PI", "CR")
VOCABULARY_KINDS: tuple[str, ...] = ("choice", "int", "days_before_service", "bundle")
LETTER_PARAGRAPHS: frozenset[str] = frozenset({"appeal_rights", "corrected_claim_rights", "no_appeal_rights"})
# Item 24E of the CMS-1500 (and the 837P SV107) holds at most four diagnosis pointers per line.
MAX_DIAGNOSIS_POINTERS = 4
DEFAULT_AGE_BAND: tuple[int, int, float] = (18, 85, 1.0)
CLAIM_SETUP_KEYS: frozenset[str] = frozenset(
    {
        "timely_filing",
        "prior_auth",
        "referring_provider",
        "force_em",
        "strip_em_modifier_25",
        "strip_target_modifiers",
        "set_target_modifiers",
        "duplicate_target_line",
        "swap_diagnosis_inconsistent",
        "bill_excluded_kinds",
    }
)

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
class NonCoveredRule:
    """Line kinds a payer never pays: the lines get this CAS (group, CARC, RARCs) instead of a payment.

    The CAS must be an informational adjustment under the ingest rule (docs/SPEC.md section 6): group
    PR with a patient-responsibility CARC (`app.ingest.denials.PATIENT_RESPONSIBILITY_CARCS`), which
    creates no work item on its own, because such lines sit on paid, unlabeled claims. The loader
    rejects anything else.
    """

    kinds: tuple[str, ...]
    group: str
    carc: str
    rarcs: tuple[str, ...] = ()


@dataclass(frozen=True)
class AgeBand:
    """Patients of a payer are drawn from weighted age bands (inclusive years at the date of service)."""

    low: int
    high: int
    weight: float = 1.0


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
    # kind -> modifiers every line of that kind carries on a claim to this payer (AT on CMT, GP
    # on therapy codes, GY on statutorily excluded lines, ...); appended after the slot's own.
    kind_modifiers: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # line kinds this payer never pays (see NonCoveredRule)
    noncovered: tuple[NonCoveredRule, ...] = ()
    # line kinds the clinic does not bill to this payer at all (billed to the patient instead);
    # a scenario's claim_setup `bill_excluded_kinds: true` keeps them on the claim
    excluded_kinds: tuple[str, ...] = ()
    # code -> the code this payer is billed instead (Medicare does not recognise 97014: G0283)
    code_map: dict[str, str] = field(default_factory=dict)
    # the age bands this payer's patients come from (Medicare mostly 65 and over, ...)
    age_bands: tuple[AgeBand, ...] = (AgeBand(*DEFAULT_AGE_BAND),)
    # share of every payment withheld as sequestration (CO-253 on each paid line; Medicare 2%)
    sequestration_rate: Decimal = Decimal("0")
    # line kinds whose claims carry the rendering provider as the ordering provider (a chiropractor
    # who orders and takes the office's own X-rays), unless a scenario's claim_setup says otherwise
    ordering_provider_kinds: tuple[str, ...] = ()
    # the payer's own wording for a letter paragraph (appeal_rights, corrected_claim_rights,
    # no_appeal_rights); the profile-wide paragraph applies otherwise
    letter_overrides: dict[str, str] = field(default_factory=dict)

    def noncovered_rule(self, kind: str) -> NonCoveredRule | None:
        for rule in self.noncovered:
            if kind in rule.kinds:
                return rule
        return None

    def billed_code(self, code: str) -> str:
        return self.code_map.get(code, code)


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
class DetailSpec:
    """One clinical detail text for the notes and the anatomic regions it covers.

    `sites` pairs a region with the site the text names there (the segment adjusted, "C5-C6"); a
    region named without a site has none. Notes that quote the text and name a site for a diagnosis
    of that region use this site, so the finding and the segments adjusted always agree.
    """

    text: str
    regions: tuple[str, ...] = ()
    sites: tuple[tuple[str, str], ...] = ()

    def site_for(self, region: str) -> str | None:
        for name, site in self.sites:
            if name == region and site:
                return site
        return None


@dataclass(frozen=True)
class CodeSpec:
    code: str
    description: str
    charge: Decimal
    role: str  # em | procedure | addon | drug | path
    kind: str  # em, cmt, therapy, xray, drug, ... (free text used by scenarios and payer rules)
    ndc: str | None = None
    units: tuple[int, int] = (1, 1)
    # clinical detail texts for the notes ({<kind>_detail}, {line_detail}); one is drawn per line
    detail: tuple[DetailSpec, ...] = ()
    # anatomic regions the code itself covers (a cervical X-ray: cervical); see ProcedureSlot.match_regions
    regions: tuple[str, ...] = ()


@dataclass(frozen=True)
class DiagnosisSpec:
    code: str
    description: str
    sites: tuple[str, ...]
    lesion: str
    # anatomic regions the condition belongs to; diagnosis pairs and line details follow them
    regions: tuple[str, ...] = ()


@dataclass(frozen=True)
class VocabularyEntry:
    """A profile-defined placeholder: a choice from a list, an integer range, a date before the
    service date (`days_before_service` low..high), or a bundle (one of several mappings drawn
    together, so placeholders that depend on each other agree). A range bound may name an earlier
    integer placeholder (`{range: [3, plan_visits]}`)."""

    name: str
    kind: str  # choice | int | days_before_service | bundle
    values: tuple[str, ...] = ()
    low: int | str = 0
    high: int | str = 0
    options: tuple[tuple[tuple[str, str], ...], ...] = ()

    @property
    def names(self) -> tuple[str, ...]:
        """The placeholders this entry defines: the bundle's keys, or the entry's own name."""
        if self.kind == "bundle" and self.options:
            return tuple(key for key, _ in self.options[0])
        return (self.name,)


@dataclass(frozen=True)
class ProcedureSlot:
    codes: tuple[str, ...]
    probability: float = 1.0
    modifiers: tuple[str, ...] = ()
    modifier_probability: float = 1.0
    dx_groups: tuple[int, ...] = (1,)
    # The anchor of the encounter (at most one slot): its drawn detail's regions decide the treated
    # regions, and its diagnosis groups are drawn per region (one code of each group per region, up
    # to `max_pairs` regions), so a level and its condition always share a region.
    pair_regions: bool = False
    max_pairs: int = 2
    # The slot's code is chosen among codes whose `regions` meet the treated regions, and the line
    # points to the diagnoses of that region.
    match_regions: bool = False


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
    # drawn only for a scenario that requires one of its tags, never for an unlabeled claim
    scenario_only: bool = False

    @property
    def diagnosis_codes(self) -> tuple[str, ...]:
        seen: list[str] = []
        for group in self.diagnosis_groups:
            for code in group:
                if code not in seen:
                    seen.append(code)
        return tuple(seen)

    @property
    def anchor_index(self) -> int | None:
        return next((i for i, slot in enumerate(self.procedures) if slot.pair_regions), None)


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
    requires_payers: tuple[str, ...]
    other_payer: str | None
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
    vocabulary: dict[str, VocabularyEntry]
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
    def kinds(self) -> tuple[str, ...]:
        kinds: list[str] = []
        for code in self.codes.values():
            if code.kind not in kinds:
                kinds.append(code.kind)
        return tuple(kinds)

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


def _build_noncovered(raw: Any, where: str) -> NonCoveredRule:
    if not isinstance(raw, dict):
        raise ValueError(f"profile {where}: noncovered entries must be mappings")
    kinds = _tuple(raw.get("kinds"))
    if not kinds:
        raise ValueError(f"profile {where}: noncovered entry without kinds")
    group = str(raw.get("group", PATIENT_RESPONSIBILITY_GROUP)).upper()
    carc = str(_require(raw, "carc", where)).strip()
    # Non-covered lines sit on paid, unlabeled claims: their CAS must be one the ingest rule never
    # turns into a work item (docs/SPEC.md section 6), or the profile would plant unlabeled denials.
    if group != PATIENT_RESPONSIBILITY_GROUP or carc not in PATIENT_RESPONSIBILITY_CARCS:
        raise ValueError(
            f"profile {where}: a noncovered CAS must be group {PATIENT_RESPONSIBILITY_GROUP} with a "
            f"patient-responsibility CARC ({', '.join(sorted(PATIENT_RESPONSIBILITY_CARCS))}), "
            f"not {group}-{carc}"
        )
    return NonCoveredRule(kinds=kinds, group=group, carc=carc, rarcs=_tuple(raw.get("rarcs")))


def _build_age_bands(raw: Any, where: str) -> tuple[AgeBand, ...]:
    if raw is None:
        return (AgeBand(*DEFAULT_AGE_BAND),)
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"profile {where}: age_bands must be a non-empty list of {{range, weight}}")
    bands: list[AgeBand] = []
    for entry in raw:
        if not isinstance(entry, dict) or "range" not in entry:
            raise ValueError(f"profile {where}: every age band needs a range: [low, high]")
        low, high = _int_range(entry["range"], (0, 0))
        weight = float(entry.get("weight", 1.0))
        if low < 0 or high > 110 or weight <= 0:
            raise ValueError(f"profile {where}: age band {low}-{high} out of range or without weight")
        bands.append(AgeBand(low=low, high=high, weight=weight))
    return tuple(bands)


def _build_payer(raw: dict[str, Any]) -> PayerSpec:
    where = f"payer {raw.get('payer_id', '?')}"
    allowed_ratio = Decimal(str(raw.get("allowed_ratio", "0.70")))
    if not 0 < allowed_ratio <= 1:
        raise ValueError(f"profile {where}: allowed_ratio must be above 0 and at most 1")
    kind_modifiers_raw = raw.get("kind_modifiers") or {}
    if not isinstance(kind_modifiers_raw, dict):
        raise ValueError(f"profile {where}: kind_modifiers must be a mapping of kind -> modifiers")
    code_map_raw = raw.get("code_map") or {}
    if not isinstance(code_map_raw, dict):
        raise ValueError(f"profile {where}: code_map must be a mapping of code -> billed code")
    sequestration = Decimal(str(raw.get("sequestration_rate", "0")))
    if not 0 <= sequestration < 1:
        raise ValueError(f"profile {where}: sequestration_rate must be at least 0 and below 1")
    letters_raw = raw.get("letters") or {}
    if not isinstance(letters_raw, dict) or set(letters_raw) - LETTER_PARAGRAPHS:
        raise ValueError(f"profile {where}: letters may only override {sorted(LETTER_PARAGRAPHS)}")
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
        kind_modifiers={str(k): _tuple(v) for k, v in kind_modifiers_raw.items()},
        noncovered=tuple(_build_noncovered(entry, where) for entry in (raw.get("noncovered") or [])),
        excluded_kinds=_tuple(raw.get("excluded_kinds")),
        code_map={str(k): str(v) for k, v in code_map_raw.items()},
        age_bands=_build_age_bands(raw.get("age_bands"), where),
        sequestration_rate=sequestration,
        ordering_provider_kinds=_tuple(raw.get("ordering_provider_kinds")),
        letter_overrides={str(k): str(v).strip() for k, v in letters_raw.items()},
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


def _build_detail(raw: Any, where: str) -> DetailSpec:
    """A detail text: a string, or {text, regions} where regions is a list of region names or a
    mapping region -> the site the text names there."""
    if isinstance(raw, str):
        return DetailSpec(text=raw)
    if not isinstance(raw, dict) or not raw.get("text"):
        raise ValueError(f"profile {where}: a detail is a string or a mapping with text and regions")
    regions_raw = raw.get("regions") or []
    if isinstance(regions_raw, dict):
        sites = tuple((str(region), str(site or "")) for region, site in regions_raw.items())
        regions = tuple(region for region, _ in sites)
    else:
        regions, sites = _tuple(regions_raw), ()
    return DetailSpec(text=str(raw["text"]), regions=regions, sites=sites)


def _build_code(code: str, raw: dict[str, Any]) -> CodeSpec:
    where = f"code {code}"
    role = str(raw.get("role", "procedure"))
    if role not in LINE_ROLES:
        raise ValueError(f"profile {where}: unknown role '{role}'")
    detail_raw = raw.get("detail")
    if detail_raw is None:
        details: tuple[DetailSpec, ...] = ()
    elif isinstance(detail_raw, list):
        details = tuple(_build_detail(entry, where) for entry in detail_raw)
    else:
        details = (_build_detail(detail_raw, where),)
    return CodeSpec(
        code=str(code),
        description=str(_require(raw, "description", where)),
        charge=_dec(_require(raw, "charge", where)),
        role=role,
        kind=str(raw.get("kind", role)),
        ndc=str(raw["ndc"]) if raw.get("ndc") else None,
        units=_int_range(raw.get("units"), (1, 1)),
        detail=details,
        regions=_tuple(raw.get("regions")),
    )


def _build_diagnosis(code: str, raw: dict[str, Any]) -> DiagnosisSpec:
    where = f"diagnosis {code}"
    return DiagnosisSpec(
        code=str(code),
        description=str(_require(raw, "description", where)),
        sites=_tuple(raw.get("sites")) or ("the treated area",),
        lesion=str(raw.get("lesion", "finding")),
        regions=_tuple(raw.get("regions")),
    )


def _range_bound(value: Any, where: str) -> int | str:
    if isinstance(value, bool):
        raise ValueError(f"profile {where}: a range bound is an integer or a placeholder name")
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if text.lstrip("-").isdigit():
        return int(text)
    if not text.isidentifier():
        raise ValueError(
            f"profile {where}: range bound '{text}' is neither an integer nor a placeholder name"
        )
    return text


def _build_vocabulary_entry(name: str, raw: Any) -> VocabularyEntry:
    where = f"vocabulary {name}"
    if isinstance(raw, list):
        values = tuple(str(v) for v in raw)
        if not values:
            raise ValueError(f"profile {where}: empty choice list")
        return VocabularyEntry(name=name, kind="choice", values=values)
    if isinstance(raw, dict):
        if "range" in raw:
            bounds = raw["range"]
            if not isinstance(bounds, list) or len(bounds) != 2:
                raise ValueError(f"profile {where}: range must be [low, high]")
            lo, hi = _range_bound(bounds[0], where), _range_bound(bounds[1], where)
            if isinstance(lo, int) and isinstance(hi, int) and lo > hi:
                raise ValueError(f"profile {where}: range low must not exceed high")
            return VocabularyEntry(name=name, kind="int", low=lo, high=hi)
        if "days_before_service" in raw:
            lo, hi = _int_range(raw["days_before_service"], (0, 0))
            if lo < 0:
                raise ValueError(f"profile {where}: days_before_service must not be negative")
            return VocabularyEntry(name=name, kind="days_before_service", low=lo, high=hi)
        if "bundle" in raw:
            options_raw = raw["bundle"]
            if not isinstance(options_raw, list) or not options_raw:
                raise ValueError(f"profile {where}: bundle must be a non-empty list of mappings")
            if not all(isinstance(option, dict) and option for option in options_raw):
                raise ValueError(f"profile {where}: every bundle option is a mapping")
            keys = tuple(str(k) for k in options_raw[0])
            options: list[tuple[tuple[str, str], ...]] = []
            for option in options_raw:
                mapping = {str(k): str(v) for k, v in option.items()}
                if set(mapping) != set(keys):
                    raise ValueError(f"profile {where}: every bundle option sets the same placeholders")
                options.append(tuple((key, mapping[key]) for key in keys))
            return VocabularyEntry(name=name, kind="bundle", options=tuple(options))
    raise ValueError(
        f"profile {where}: must be a list of choices, {{range: [lo, hi]}}, "
        "{{days_before_service: [lo, hi]}} or {{bundle: [mappings]}}"
    )


def _check_vocabulary(vocabulary: dict[str, VocabularyEntry]) -> None:
    """Placeholder names are unique; a named range bound refers to an earlier integer placeholder."""
    defined: set[str] = set()
    integer: set[str] = set()
    for entry in vocabulary.values():
        where = f"vocabulary {entry.name}"
        for bound in (entry.low, entry.high) if entry.kind == "int" else ():
            if isinstance(bound, str) and bound not in integer:
                raise ValueError(
                    f"profile {where}: range bound '{bound}' must be an earlier integer placeholder"
                )
        names = entry.names
        clash = defined & set(names)
        if clash:
            raise ValueError(f"profile {where}: placeholder {sorted(clash)[0]} is defined twice")
        defined.update(names)
        if entry.kind == "int":
            integer.add(entry.name)
        elif entry.kind == "bundle":
            for index, key in enumerate(names):
                if all(option[index][1].strip().lstrip("-").isdigit() for option in entry.options):
                    integer.add(key)
        elif entry.kind == "choice" and all(v.strip().lstrip("-").isdigit() for v in entry.values):
            integer.add(entry.name)


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
        if "dx_groups" in slot_raw:
            dx_groups = tuple(int(g) for g in slot_raw["dx_groups"])
        else:
            dx_groups = (int(slot_raw.get("dx_group", 1)),)
        if not dx_groups:
            raise ValueError(f"profile {where}: procedure slot without diagnosis groups")
        slot = ProcedureSlot(
            codes=slot_codes,
            probability=float(slot_raw.get("probability", 1.0)),
            modifiers=_tuple(slot_raw.get("modifiers")),
            modifier_probability=float(slot_raw.get("modifier_probability", 1.0)),
            dx_groups=dx_groups,
            pair_regions=bool(slot_raw.get("pair_regions", False)),
            max_pairs=int(slot_raw.get("max_pairs", 2)),
            match_regions=bool(slot_raw.get("match_regions", False)),
        )
        if slot.pair_regions and (slot.probability < 1.0 or not 1 <= slot.max_pairs):
            raise ValueError(
                f"profile {where}: a pair_regions slot is always present and pairs at least once"
            )
        if slot.pair_regions and slot.max_pairs * len(dx_groups) > MAX_DIAGNOSIS_POINTERS:
            raise ValueError(
                f"profile {where}: max_pairs x dx_groups exceeds {MAX_DIAGNOSIS_POINTERS} diagnosis pointers"
            )
        slots.append(slot)
    if not em_codes and not slots:
        raise ValueError(f"profile {where}: needs an E/M code or a procedure slot")
    if sum(1 for slot in slots if slot.pair_regions) > 1:
        raise ValueError(f"profile {where}: at most one procedure slot may pair diagnoses by region")
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
        for dx_group in slot.dx_groups:
            if not 1 <= dx_group <= len(groups):
                raise ValueError(f"profile {where}: dx_group {dx_group} out of range")
    for slot in slots:
        if not slot.pair_regions:
            continue
        # Every detail of every anchor code must name at least one region that each paired group
        # can supply a diagnosis for, or the generator could not draw a coherent pair.
        pairable = pairable_regions(slot, groups, diagnoses)
        for code in slot.codes:
            spec = codes[code]
            if not spec.detail or not all(set(d.regions) & pairable for d in spec.detail):
                raise ValueError(
                    f"profile {where}: every detail of {code} must name a region the paired diagnosis "
                    f"groups cover ({', '.join(sorted(pairable)) or 'none'})"
                )
    return EncounterTemplate(
        id=str(_require(raw, "id", where)),
        weight=float(raw.get("weight", 1.0)),
        tags=_tuple(raw.get("tags")),
        em_codes=em_codes,
        em_probability=float(raw.get("em_probability", 1.0 if em_codes else 0.0)),
        em_modifier_25=bool(raw.get("em_modifier_25", False)),
        procedures=tuple(slots),
        diagnosis_groups=tuple(groups),
        scenario_only=bool(raw.get("scenario_only", False)),
    )


def pairable_regions(
    slot: ProcedureSlot,
    groups: tuple[tuple[str, ...], ...] | list[tuple[str, ...]],
    diagnoses: dict[str, DiagnosisSpec],
) -> set[str]:
    """Regions for which every diagnosis group the slot points to holds a code of that region."""
    per_group = [
        {region for code in groups[index - 1] for region in diagnoses[code].regions}
        for index in slot.dx_groups
    ]
    return set.intersection(*per_group) if per_group else set()


def _build_scenario(
    raw: dict[str, Any],
    codes: dict[str, CodeSpec],
    known_tags: tuple[str, ...],
    payer_ids: tuple[str, ...],
    diagnoses: dict[str, DiagnosisSpec] | None = None,
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
    requires_payers = _tuple(raw.get("requires_payers"))
    for payer_id in requires_payers:
        if payer_id not in payer_ids:
            raise ValueError(f"profile {where}: unknown payer '{payer_id}' in requires_payers")
    other_payer = str(raw["other_payer"]) if raw.get("other_payer") else None
    if other_payer is not None and other_payer not in payer_ids:
        raise ValueError(f"profile {where}: unknown payer '{other_payer}' in other_payer")
    setup = dict(raw.get("claim_setup") or {})
    unknown = set(setup) - CLAIM_SETUP_KEYS
    if unknown:
        raise ValueError(f"profile {where}: unknown claim_setup keys {sorted(unknown)}")
    if setup.get("timely_filing") not in (None, *TIMELY_FILING_MODES):
        raise ValueError(f"profile {where}: timely_filing must be one of {TIMELY_FILING_MODES}")
    if setup.get("prior_auth") not in (None, *PRIOR_AUTH_MODES):
        raise ValueError(f"profile {where}: prior_auth must be one of {PRIOR_AUTH_MODES}")
    if setup.get("referring_provider") not in (None, *REFERRING_PROVIDER_MODES):
        raise ValueError(f"profile {where}: referring_provider must be one of {REFERRING_PROVIDER_MODES}")
    strip = setup.get("strip_target_modifiers")
    if strip is not None and not isinstance(strip, bool):
        # a list names the modifiers to remove; the line keeps every other one (the payer's GP, ...)
        if not isinstance(strip, list) or not strip:
            raise ValueError(f"profile {where}: strip_target_modifiers is true or a list of modifiers")
        setup["strip_target_modifiers"] = [str(m).strip().upper() for m in strip]
    swap = setup.get("swap_diagnosis_inconsistent")
    if swap is not None:
        # the declared pool the scenario swaps onto the claim, never a code the encounters bill
        if not isinstance(swap, list) or not swap:
            raise ValueError(f"profile {where}: swap_diagnosis_inconsistent is a non-empty list of diagnoses")
        pool = [str(c) for c in swap]
        unknown_dx = [c for c in pool if diagnoses is not None and c not in diagnoses]
        if unknown_dx:
            raise ValueError(
                f"profile {where}: unknown diagnosis '{unknown_dx[0]}' in swap_diagnosis_inconsistent"
            )
        setup["swap_diagnosis_inconsistent"] = pool
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
        requires_payers=requires_payers,
        other_payer=other_payer,
        claim_setup=setup,
        notes=notes,
        note_count=note_count,
        letter=str(raw["letter"]).strip() if raw.get("letter") else None,
        letter_required=bool(raw.get("letter_required", False)),
        no_appeal_rights=bool(raw.get("no_appeal_rights", False)),
        allow_benign_notes=bool(raw.get("allow_benign_notes", True)),
    )


def _check_payer_kinds(payer: PayerSpec, kinds: tuple[str, ...], codes: dict[str, CodeSpec]) -> None:
    """Every kind a payer rule names must be the kind of some fee-schedule code (catches typos); a
    code_map entry maps a fee-schedule code to another of the same kind."""
    named = [
        *payer.kind_modifiers,
        *payer.excluded_kinds,
        *payer.ordering_provider_kinds,
        *(k for r in payer.noncovered for k in r.kinds),
    ]
    for kind in named:
        if kind not in kinds:
            raise ValueError(f"profile payer {payer.payer_id}: no fee-schedule code has kind '{kind}'")
    for source, target in payer.code_map.items():
        if source not in codes or target not in codes:
            raise ValueError(
                f"profile payer {payer.payer_id}: code_map {source} -> {target} names an unknown code"
            )
        if codes[source].kind != codes[target].kind:
            raise ValueError(
                f"profile payer {payer.payer_id}: code_map {source} -> {target} changes the line kind"
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
    kinds: list[str] = []
    for code in codes.values():
        if code.kind not in kinds:
            kinds.append(code.kind)
    for payer in payers:
        _check_payer_kinds(payer, tuple(kinds), codes)
    diagnoses = {str(k): _build_diagnosis(str(k), v) for k, v in _require(raw, "diagnoses", "root").items()}
    vocabulary = {
        str(k): _build_vocabulary_entry(str(k), v) for k, v in (raw.get("vocabulary") or {}).items()
    }
    _check_vocabulary(vocabulary)
    encounters = tuple(_build_encounter(e, codes, diagnoses) for e in _require(raw, "encounters", "root"))
    if not encounters:
        raise ValueError("profile root: needs at least one encounter template")
    if len({e.id for e in encounters}) != len(encounters):
        raise ValueError("profile root: encounter ids must be unique")
    if all(e.scenario_only for e in encounters):
        raise ValueError(
            "profile root: unlabeled claims need at least one encounter that is not scenario_only"
        )
    known_tags: list[str] = []
    for enc in encounters:
        for tag in enc.tags:
            if tag not in known_tags:
                known_tags.append(tag)
    payer_ids = tuple(p.payer_id for p in payers)
    scenarios = tuple(
        _build_scenario(s, codes, tuple(known_tags), payer_ids, diagnoses)
        for s in _require(raw, "scenarios", "root")
    )
    if not scenarios:
        raise ValueError("profile root: needs at least one denial scenario")
    if len({s.id for s in scenarios}) != len(scenarios):
        raise ValueError("profile root: scenario ids must be unique")
    billed_dx = {code for enc in encounters for code in enc.diagnosis_codes}
    for scenario in scenarios:
        pool = scenario.claim_setup.get("swap_diagnosis_inconsistent") or []
        billed = [code for code in pool if code in billed_dx]
        if billed:
            raise ValueError(
                f"profile scenario {scenario.id}: swap pool code '{billed[0]}' is billed by an encounter"
            )
    code_texts = {str(k): str(v) for k, v in (raw.get("code_texts") or {}).items()}
    for scenario in scenarios:
        for code in (scenario.carc, *scenario.rarcs):
            if code not in code_texts:
                raise ValueError(f"profile scenario {scenario.id}: no code text for '{code}'")
    for payer in payers:
        for rule in payer.noncovered:
            for code in (rule.carc, *rule.rarcs):
                if code not in code_texts:
                    raise ValueError(f"profile payer {payer.payer_id}: no code text for '{code}'")
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
        vocabulary=vocabulary,
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
    "AgeBand",
    "CLAIM_SETUP_KEYS",
    "CodeSpec",
    "DEFAULT_PROFILE",
    "DetailSpec",
    "DiagnosisSpec",
    "EncounterTemplate",
    "LETTER_PARAGRAPHS",
    "LEVELS",
    "MAX_DIAGNOSIS_POINTERS",
    "NonCoveredRule",
    "PROFILES_DIR",
    "PayerSpec",
    "ProcedureSlot",
    "Profile",
    "ProviderSpec",
    "REFERRING_PROVIDER_MODES",
    "ScenarioSpec",
    "TargetSpec",
    "VOCABULARY_KINDS",
    "VocabularyEntry",
    "build_profile",
    "is_valid_npi",
    "load_profile",
    "make_npi",
    "npi_check_digit",
    "pairable_regions",
    "resolve_profile_path",
]
