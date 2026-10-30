"""Tests for the synthetic data module (docs/SPEC.md section 12).

The committed fixture set under synthetic/fixtures/labeled_set_v2 (the chiropractic profile) is
checked for its counts, its reproducibility, the payer billing rules it encodes and the exact file
formats the ingest module reads (SPEC sections 4.1 and 4.2).
Tests that need modules other agents build (the 835 tokenizer, the injection detector) import them
lazily and skip that assertion when the module is not there yet.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.enums import ACTIONABLE_CATEGORIES, ActionType, DenialCategory
from app.schemas import CLAIM_EXPORT_COLUMNS, ClaimExportRow, NoteRow, PayerDocumentRow
from synthetic import names
from synthetic.generate import (
    DEFAULT_AS_OF,
    INJECTION_PAYLOADS,
    INJECTION_SIGNATURES,
    MIN_DENIALS_OTHER,
    MIN_DENIALS_PER_CATEGORY,
    generate,
)
from synthetic.profile import (
    DEFAULT_PROFILE,
    GROUP_CODES,
    PROFILES_DIR,
    build_profile,
    is_valid_npi,
    load_profile,
    make_npi,
    npi_check_digit,
)
from synthetic.x12_writer import ISA_LENGTH, build_835, cas_segments, format_amount, split_segments

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "synthetic" / "fixtures" / "labeled_set_v2"
FIXTURE_SEED = 20261028
FIXTURE_N_CLAIMS = 200
PROFILE = "chiropractic"
INFORMATIONAL_CARCS = {"1", "2", "3", "45", "66", "94", "104", "118", "137", "144", "187", "237", "253"}
DENIAL_GROUPS = {"CO", "PI", "OA", "PR"}
C, A = DenialCategory, ActionType
# scenario id -> (category, expected action, CARC, RARCs, group, level)
REQUIRED_SCENARIOS = {
    "elig_coverage_terminated": (C.ELIGIBILITY, A.CLINIC_QUESTION, "27", ("N30",), "CO", "claim"),
    "elig_ma_enrollee_billed_to_medicare": (C.ELIGIBILITY, A.CORRECTED_CLAIM, "24", (), "CO", "claim"),
    "auth_ma_plan_of_care_absent": (C.AUTHORIZATION, A.APPEAL, "197", ("N54",), "CO", "line"),
    "auth_number_not_on_claim": (C.AUTHORIZATION, A.CORRECTED_CLAIM, "16", ("M62",), "CO", "claim"),
    "auth_medicaid_service_limit": (C.AUTHORIZATION, A.CLINIC_QUESTION, "119", ("N640",), "CO", "line"),
    "modifier_at_missing": (C.MODIFIER, A.CORRECTED_CLAIM, "50", ("N115",), "CO", "line"),
    "modifier_at_missing_maintenance_care": (C.MODIFIER, A.NO_ACTION, "50", ("N115",), "CO", "line"),
    "modifier_gp_missing": (C.MODIFIER, A.CORRECTED_CLAIM, "4", ("N822",), "CO", "line"),
    "modifier_25_missing_em_with_cmt": (C.MODIFIER, A.CORRECTED_CLAIM, "97", ("N626",), "CO", "line"),
    "duplicate_exact": (C.DUPLICATE, A.NO_ACTION, "18", ("N522",), "OA", "claim"),
    "duplicate_second_cmt_same_day": (C.DUPLICATE, A.NO_ACTION, "18", ("M86",), "CO", "line"),
    "timely_filing_expired": (C.TIMELY_FILING, A.NO_ACTION, "29", (), "CO", "claim"),
    "timely_filing_in_window": (C.TIMELY_FILING, A.APPEAL, "29", (), "CO", "claim"),
    "med_nec_maintenance_review": (C.MEDICAL_NECESSITY, A.APPEAL, "50", ("N115",), "CO", "line"),
    "med_nec_um_vendor_review": (C.MEDICAL_NECESSITY, A.APPEAL, "50", ("N10",), "CO", "line"),
    "diagnosis_subluxation_primary_missing": (C.MEDICAL_NECESSITY, A.CORRECTED_CLAIM, "50", ("N115",), "CO", "line"),
    "diagnosis_inconsistent_with_cmt": (C.MEDICAL_NECESSITY, A.CORRECTED_CLAIM, "11", ("N657",), "CO", "line"),
    "bundling_97140_separate_region": (C.BUNDLING, A.CORRECTED_CLAIM, "97", ("M15",), "CO", "line"),
    "bundling_97140_same_region": (C.BUNDLING, A.NO_ACTION, "236", ("N20",), "CO", "line"),
    "cob_other_coverage_primary": (C.COORDINATION_OF_BENEFITS, A.CLINIC_QUESTION, "22", ("N4",), "CO", "claim"),
    "cob_medicare_secondary_payer": (C.COORDINATION_OF_BENEFITS, A.CLINIC_QUESTION, "22", ("N598",), "CO", "claim"),
    "cob_misrouted_wrong_payer": (C.COORDINATION_OF_BENEFITS, A.CORRECTED_CLAIM, "109", ("N418",), "CO", "claim"),
    "cob_work_injury_liability": (C.COORDINATION_OF_BENEFITS, A.CLINIC_QUESTION, "19", ("N418",), "CO", "claim"),
    "missing_initial_treatment_date": (C.MISSING_INFORMATION, A.CLINIC_QUESTION, "16", ("MA122", "MA130"), "CO", "claim"),
    "missing_ordering_provider_xray": (C.MISSING_INFORMATION, A.CORRECTED_CLAIM, "16", ("N264", "N265"), "CO", "line"),
    "missing_records_not_received": (C.MISSING_INFORMATION, A.CLINIC_QUESTION, "226", ("N706",), "CO", "claim"),
    "contractual_gz_maintenance": (C.CONTRACTUAL, A.NO_ACTION, "50", (), "CO", "line"),
    "pr_xray_without_gy": (C.PATIENT_RESPONSIBILITY, A.NO_ACTION, "170", ("N95",), "PR", "line"),
    "other_ppo_visit_limit": (C.OTHER, A.CLINIC_QUESTION, "119", ("N587",), "CO", "line"),
}  # fmt: skip
# scenarios whose corrected claim or appeal is a coding change and must ship a note excerpt
CODING_CHANGE_SCENARIOS = {
    "modifier_at_missing",
    "modifier_gp_missing",
    "modifier_25_missing_em_with_cmt",
    "diagnosis_subluxation_primary_missing",
    "diagnosis_inconsistent_with_cmt",
    "bundling_97140_separate_region",
}
# scenarios whose no_action / clinic_question verdict rests on what the notes say (the evidence must
# be on file for the label to hold): maintenance care documented, a second encounter documented for
# a once-per-day code, a routine visit note on a service-limit denial
NOTE_EVIDENCE_SCENARIOS = {
    "modifier_at_missing_maintenance_care",
    "duplicate_second_cmt_same_day",
    "auth_medicaid_service_limit",
}
# scenarios that must never carry a note (the no_action verdict is that nothing supports a fix)
NO_NOTE_SCENARIOS = {"bundling_97140_same_region"}
MEDICARE, MA, PPO, MEDICAID = "GBMB1", "GPMA2", "SWPPO3", "TLMC4"
# the strips each scenario's claim_setup makes on its target line (every other kind modifier stays)
INTENDED_STRIPS = {
    "modifier_at_missing": {"AT"},
    "modifier_at_missing_maintenance_care": {"AT"},
    "modifier_gp_missing": {"GP"},
    "pr_xray_without_gy": {"GY"},
    "bundling_97140_separate_region": {"XS", "59"},
}
SWAP_SCENARIOS = ("diagnosis_subluxation_primary_missing", "diagnosis_inconsistent_with_cmt")
SEGMENT_RE = re.compile(r"\b(?:[CTL]\d-[CTLS]\d|S\d-S\d|(?:left|right) sacroiliac joint|sacral base)\b")
PHONE_RE = re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def _read_csv(path: Path) -> list[dict[str, str]]:
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _fixture_rows() -> list[dict[str, str]]:
    return _read_csv(FIXTURE_DIR / "claims_export.csv")


def _fixture_labels() -> list[dict[str, Any]]:
    return json.loads((FIXTURE_DIR / "labels.json").read_text(encoding="utf-8"))


def _fixture_manifest() -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8"))


def _remit_files(directory: Path = FIXTURE_DIR) -> list[Path]:
    return sorted(directory.glob("remit_*.835"))


def _group_rows(rows: list[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        grouped.setdefault(row["claim_control_number"], []).append(row)
    return grouped


def _cas_triplets(segment: list[str]) -> list[tuple[str, str, Decimal]]:
    group = segment[1]
    elements = segment[2:]
    out: list[tuple[str, str, Decimal]] = []
    for index in range(0, len(elements), 3):
        out.append((group, elements[index], Decimal(elements[index + 1])))
    return out


def _parse_835(text: str) -> dict[str, Any]:
    """Tiny state-machine parse used by the tests: claim-level vs line-level attribution."""
    segments = split_segments(text)
    isa_line = text.split("\n", 1)[0]
    claims: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    service: dict[str, Any] | None = None
    for seg in segments:
        tag = seg[0]
        if tag == "CLP":
            current = {"clp": seg, "cas": [], "lq": [], "nm1": {}, "dtm": {}, "svcs": []}
            service = None
            claims.append(current)
        elif tag == "SVC" and current is not None:
            service = {"svc": seg, "cas": [], "lq": [], "ref": {}, "dtm": {}, "amt": {}}
            current["svcs"].append(service)
        elif tag in ("SE", "PLB", "GE", "IEA"):
            current, service = None, None
        elif current is not None:
            holder = service if service is not None else current
            if tag == "CAS":
                holder["cas"].extend(_cas_triplets(seg))
            elif tag == "LQ":
                holder["lq"].append(seg[2])
            elif tag == "REF" and service is not None:
                service["ref"][seg[1]] = seg[2]
            elif tag == "NM1" and service is None:
                current["nm1"][seg[1]] = seg
            elif tag == "DTM":
                holder["dtm"][seg[1]] = seg[2]
            elif tag == "AMT" and service is not None:
                service["amt"][seg[1]] = seg[2]
    return {"isa": isa_line, "segments": segments, "claims": claims}


def _claims_in_fixture_remits() -> dict[str, tuple[str, dict[str, Any]]]:
    """claim control number -> (payer_id of the file, parsed claim)."""
    found: dict[str, tuple[str, dict[str, Any]]] = {}
    for path in _remit_files():
        payer_id = path.stem.split("_", 1)[1]
        parsed = _parse_835(path.read_text(encoding="utf-8"))
        for claim in parsed["claims"]:
            ccn = claim["clp"][1]
            assert ccn not in found, f"{ccn} appears in more than one 835"
            found[ccn] = (payer_id, claim)
    return found


# --------------------------------------------------------------------------------------
# word lists, NPI, profile
# --------------------------------------------------------------------------------------


def test_word_lists_are_large_and_unique() -> None:
    assert len(names.FIRST_NAMES) >= 60 and len(set(names.FIRST_NAMES)) == len(names.FIRST_NAMES)
    assert len(names.LAST_NAMES) >= 60 and len(set(names.LAST_NAMES)) == len(names.LAST_NAMES)
    assert len(names.STREET_NAMES) >= 30 and len(set(names.STREET_NAMES)) == len(names.STREET_NAMES)
    assert len(names.CITIES) >= 15
    for city, state, zip_code in names.CITIES:
        assert city and len(state) == 2 and state.isupper() and len(zip_code) == 5 and zip_code.isdigit()
    assert len(names.PROVIDER_NAMES) >= 2


def test_npi_check_digit() -> None:
    assert npi_check_digit("123456789") == "3"
    assert make_npi("123456789") == "1234567893"
    assert is_valid_npi("1234567893")
    assert not is_valid_npi("1234567890")
    assert not is_valid_npi("123456789")
    with pytest.raises(ValueError):
        npi_check_digit("12345")


def test_profile_contract() -> None:
    assert DEFAULT_PROFILE == PROFILE
    profile = load_profile(PROFILE)
    assert profile.specialty == "Chiropractic"
    payers = {p.payer_id: p for p in profile.payers}
    assert list(payers) == [MEDICARE, MA, PPO, MEDICAID]
    expected = {
        MEDICARE: ("Great Basin Medicare Part B", "MB", 365, 365, 120),
        MA: ("Granite Peak Medicare Advantage", "16", 365, 365, 65),
        PPO: ("Sawtooth Health Plan PPO", "12", 90, 90, 180),
        MEDICAID: ("Timberline Medicaid Plan", "MC", 180, 180, 60),
    }
    for payer_id, (name, indicator, filing, corrected, appeal) in expected.items():
        payer = payers[payer_id]
        assert (
            payer.name,
            payer.filing_indicator,
            payer.timely_filing_days,
            payer.corrected_claim_days,
            payer.appeal_days,
        ) == (name, indicator, filing, corrected, appeal)
        assert payer.member_id_prefix and payer.member_id_prefix.isupper()
    assert len(profile.providers) == 2 and {p.credential for p in profile.providers} == {"DC"}
    assert all(is_valid_npi(p.npi) for p in profile.providers)
    assert profile.referring_providers == () and profile.referring_rate == 0.0
    assert is_valid_npi(str(profile.clinic["npi"]))
    assert profile.place_of_service == "11"
    em = {c for c in profile.codes if profile.codes[c].role == "em"}
    assert {"99202", "99203", "99204", "99212", "99213", "99214"} <= em
    for code in (
        "98940",
        "98941",
        "98942",
        "98943",
        "97110",
        "97140",
        "97012",
        "97014",
        "G0283",
        "97124",
        "97035",
        "72040",
        "72070",
        "72100",
    ):
        assert code in profile.codes, code
    kinds = {code: spec.kind for code, spec in profile.codes.items()}
    assert {kinds[c] for c in ("98940", "98941", "98942")} == {"cmt"} and kinds["98943"] == "extraspinal"
    assert {kinds[c] for c in ("97110", "97140", "97012", "97014", "G0283", "97124", "97035")} == {"therapy"}
    assert {kinds[c] for c in ("72040", "72070", "72100")} == {"xray"}
    assert all(spec.ndc is None for spec in profile.codes.values())  # a chiropractor bills no drugs
    assert all(spec.detail for spec in profile.codes.values()), "every code carries note detail"
    assert profile.codes["97110"].units == (1, 2) and profile.codes["98941"].units == (1, 1)
    # a CMT detail names one adjusted segment per region, as many regions as the code covers
    region_counts = {"98940": (1, 2), "98941": (3, 4), "98942": (5, 5)}
    for code, (low, high) in region_counts.items():
        for detail in profile.codes[code].detail:
            assert low <= len(detail.regions) <= high, (code, detail.text)
            assert all(detail.site_for(region) for region in detail.regions), (code, detail.text)
            assert all(site in detail.text for _, site in detail.sites), (code, detail.text)
    assert all("three" in d.text or "four" in d.text for d in profile.codes["98941"].detail)
    assert all("five" in d.text for d in profile.codes["98942"].detail)
    assert {profile.codes[c].regions for c in ("72040", "72070")} == {("cervical",), ("thoracic",)}
    for dx in (
        "M99.01",
        "M99.02",
        "M99.03",
        "M99.05",
        "M54.2",
        "M54.50",
        "M54.6",
        "M54.12",
        "M54.16",
        "M50.30",
        "M51.360",
        "M47.812",
        "M47.816",
        "M62.830",
        "S13.4XXA",
        "S23.3XXA",
        "S33.5XXA",
        "S33.6XXA",
        "M25.511",
        "M25.512",
        "M25.551",
        "M25.561",
        "G44.86",
    ):
        assert dx in profile.diagnoses, dx
    # M51.36 was split into six-character codes on 2024-10-01; the upper trapezius (M79.18) is not an
    # extremity region apart from the cervical and thoracic spine
    assert "M51.36" not in profile.diagnoses and "M79.18" not in profile.diagnoses
    for code in ("M99.01", "M99.02", "M99.03", "M99.05"):
        assert len(profile.diagnoses[code].regions) == 1, code
    assert set(profile.modifiers) >= {"25", "59", "XS", "XU", "AT", "GA", "GY", "GZ", "GP", "76", "77"}
    vocabulary_names = {name for entry in profile.vocabulary.values() for name in entry.names}
    assert {"pain_initial", "initial_treatment_date", "plan_frequency", "plan_visits", "visit_number"} <= (
        vocabulary_names
    )
    scenarios = {s.id: s for s in profile.scenarios}
    assert set(scenarios) == set(REQUIRED_SCENARIOS) and len(scenarios) == 29
    for scenario_id, (category, action, carc, rarcs, group, level) in REQUIRED_SCENARIOS.items():
        scenario = scenarios[scenario_id]
        assert (
            scenario.category,
            scenario.expected_action,
            scenario.carc,
            scenario.rarcs,
            scenario.group,
            scenario.level,
        ) == (category, action, carc, rarcs, group, level), scenario_id
        assert scenario.weight > 0
        assert set(scenario.requires_payers) <= set(payers), scenario_id
    assert {s.category for s in profile.scenarios} == set(DenialCategory)
    assert scenarios["cob_misrouted_wrong_payer"].letter_required
    assert scenarios["elig_ma_enrollee_billed_to_medicare"].letter_required
    assert scenarios["elig_ma_enrollee_billed_to_medicare"].other_payer == MA
    assert scenarios["missing_initial_treatment_date"].no_appeal_rights
    assert scenarios["med_nec_maintenance_review"].amount == "allowed"
    assert scenarios["contractual_gz_maintenance"].claim_setup == {"set_target_modifiers": ["GZ"]}
    # the denied X-ray claim leaves the ordering provider off; every other Medicare X-ray claim has it
    assert scenarios["missing_ordering_provider_xray"].claim_setup == {"referring_provider": "absent"}
    assert scenarios["bundling_97140_separate_region"].claim_setup == {"strip_target_modifiers": ["XS", "59"]}
    for scenario_id in SWAP_SCENARIOS:
        pool = scenarios[scenario_id].claim_setup["swap_diagnosis_inconsistent"]
        assert pool and not any(code.startswith("M99") for code in pool), scenario_id
    # every scenario that expects an appeal or a coding change ships documentation templates, and the
    # scenarios whose verdict rests on the notes always carry the evidence
    for scenario in profile.scenarios:
        if scenario.expected_action == ActionType.APPEAL or scenario.id in CODING_CHANGE_SCENARIOS:
            assert scenario.notes, scenario.id
        if scenario.id in NOTE_EVIDENCE_SCENARIOS:
            assert scenario.notes and scenario.note_count[0] == len(scenario.notes), scenario.id
        if scenario.id in NO_NOTE_SCENARIOS:
            assert not scenario.notes and not scenario.allow_benign_notes, scenario.id
    # the informational categories carry only no_action scenarios
    for scenario in profile.scenarios:
        if scenario.category not in ACTIONABLE_CATEGORIES:
            assert scenario.expected_action == ActionType.NO_ACTION, scenario.id


def test_profile_payer_billing_rules() -> None:
    """The payer rules docs/SPECIALTY.md describes: AT on CMT for Medicare, MA and Medicaid; GP on
    therapy codes for Medicare, MA and the PPO (GY on everything Medicare does not cover); the
    non-covered kinds denied with an informational PR CAS; the kinds never billed to Medicaid."""
    profile = load_profile(PROFILE)
    payers = {p.payer_id: p for p in profile.payers}
    assert payers[MEDICARE].kind_modifiers == {
        "cmt": ("AT",),
        "therapy": ("GP", "GY"),
        "xray": ("GY",),
        "em": ("GY",),
        "extraspinal": ("GY",),
    }
    assert payers[MA].kind_modifiers == {
        "cmt": ("AT",),
        "therapy": ("GP",),
        "xray": ("GY",),
        "extraspinal": ("GY",),
    }
    assert payers[PPO].kind_modifiers == {"therapy": ("GP",)}
    assert payers[MEDICAID].kind_modifiers == {"cmt": ("AT",)}
    medicare_rule = payers[MEDICARE].noncovered_rule("therapy")
    assert medicare_rule is not None
    assert (medicare_rule.group, medicare_rule.carc, medicare_rule.rarcs) == ("PR", "96", ("N425",))
    assert set(medicare_rule.kinds) == {"therapy", "xray", "em", "extraspinal"}
    assert payers[MEDICARE].noncovered_rule("cmt") is None
    assert {k for r in payers[MA].noncovered for k in r.kinds} == {"xray", "extraspinal"}
    assert payers[PPO].noncovered == () and payers[PPO].excluded_kinds == ()
    assert payers[MEDICAID].noncovered == ()
    assert set(payers[MEDICAID].excluded_kinds) == {"therapy", "extraspinal"}
    # every non-covered CAS is informational under the ingest rule (PR group, a patient-responsibility CARC)
    for payer in profile.payers:
        for rule in payer.noncovered:
            assert rule.group == "PR" and rule.carc in {"96", "204", "49", "119"}, payer.payer_id
    # Medicare billing rules: G0283 instead of 97014, the ordering provider on radiology claims, 2%
    # sequestration, corrections by reopening or a new claim (never a frequency-7 replacement)
    for payer_id in (MEDICARE, MA):
        assert payers[payer_id].code_map == {"97014": "G0283"}, payer_id
        assert payers[payer_id].ordering_provider_kinds == ("xray",), payer_id
        assert payers[payer_id].sequestration_rate == Decimal("0.02"), payer_id
    for payer_id in (PPO, MEDICAID):
        assert payers[payer_id].code_map == {} and payers[payer_id].sequestration_rate == 0, payer_id
    medicare_letter = payers[MEDICARE].letter_overrides["corrected_claim_rights"]
    assert "reopening" in medicare_letter and "new claim" in medicare_letter
    assert "does not accept replacement claims" in medicare_letter
    # patients come from the payer's age bands: Medicare and MA mostly 65 and over, Medicaid under 65
    assert payers[MEDICARE].age_bands[0].low == 65 and payers[MA].age_bands[0].low == 65
    assert all(band.high <= 64 for band in payers[MEDICAID].age_bands)


def test_scenario_payers_follow_their_research_pattern() -> None:
    """A scenario only lands on the payers its research pattern is about: a Medicare contractor's
    timely-filing denial carries no appeal rights, the AT edits are Medicare / MA rules, the visit
    limit and the UM vendor are the PPO's, the service limit the Medicaid plan's."""
    profile = load_profile(PROFILE)
    payers = {p.payer_id: p for p in profile.payers}
    medicare_ids = {pid for pid, p in payers.items() if p.filing_indicator == "MB"}
    scenarios = {s.id: s for s in profile.scenarios}
    for scenario in profile.scenarios:
        allowed = set(scenario.requires_payers) or set(payers)
        if scenario.carc == "29" and scenario.expected_action == ActionType.APPEAL:
            assert not allowed & medicare_ids, scenario.id
    expected = {
        "modifier_at_missing": {MEDICARE, MA},
        "modifier_at_missing_maintenance_care": {MEDICARE, MA},
        "med_nec_maintenance_review": {MEDICARE, MA},
        "med_nec_um_vendor_review": {PPO},
        "other_ppo_visit_limit": {PPO},
        "auth_medicaid_service_limit": {MEDICAID},
        "auth_ma_plan_of_care_absent": {MA},
        "cob_medicare_secondary_payer": {MEDICARE},
        "cob_other_coverage_primary": {MA, PPO, MEDICAID},
        "timely_filing_in_window": {MA, PPO, MEDICAID},
        "contractual_gz_maintenance": {MEDICARE},
        "pr_xray_without_gy": {MEDICARE},
        "missing_ordering_provider_xray": {MEDICARE},
    }
    for scenario_id, payer_ids in expected.items():
        assert set(scenarios[scenario_id].requires_payers) == payer_ids, scenario_id


def _profile_raw() -> dict[str, Any]:
    import yaml

    return yaml.safe_load((PROFILES_DIR / f"{PROFILE}.yaml").read_text(encoding="utf-8"))


def test_profile_rejects_bad_npi(tmp_path: Path) -> None:
    import yaml

    raw = _profile_raw()
    raw["providers"][0]["npi"] = "1234567890"
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ValueError):
        load_profile(bad)


def test_profile_rejects_lineless_encounters_and_bad_allowed_ratio() -> None:
    raw = _profile_raw()
    raw["encounters"][0] = {**raw["encounters"][0], "em_codes": [], "procedures": []}
    with pytest.raises(ValueError, match="E/M code or a procedure slot"):
        build_profile(raw)
    raw = _profile_raw()
    raw["payers"][0]["allowed_ratio"] = 1.5
    with pytest.raises(ValueError, match="allowed_ratio"):
        build_profile(raw)
    raw["payers"][0]["allowed_ratio"] = 0
    with pytest.raises(ValueError, match="allowed_ratio"):
        build_profile(raw)


def test_profile_rejects_unknown_payer_rules_and_scenario_payers() -> None:
    raw = _profile_raw()
    raw["payers"][0]["kind_modifiers"] = {"infusion": ["JW"]}
    with pytest.raises(ValueError, match="kind 'infusion'"):
        build_profile(raw)
    raw = _profile_raw()
    raw["payers"][0]["noncovered"] = [{"kinds": ["therapy"], "group": "PR", "carc": "204", "rarcs": []}]
    with pytest.raises(ValueError, match="no code text for '204'"):
        build_profile(raw)
    raw = _profile_raw()
    raw["scenarios"][0]["requires_payers"] = ["NOPE1"]
    with pytest.raises(ValueError, match="unknown payer 'NOPE1'"):
        build_profile(raw)
    raw = _profile_raw()
    raw["scenarios"][0]["other_payer"] = "NOPE1"
    with pytest.raises(ValueError, match="other_payer"):
        build_profile(raw)
    raw = _profile_raw()
    raw["scenarios"][0]["claim_setup"] = {"referring_provider": "sometimes"}
    with pytest.raises(ValueError, match="referring_provider"):
        build_profile(raw)
    raw = _profile_raw()
    raw["scenarios"][0]["claim_setup"] = {"strip_target_modifier": True}  # a typo is an error, not silence
    with pytest.raises(ValueError, match="unknown claim_setup keys"):
        build_profile(raw)
    raw = _profile_raw()
    raw["vocabulary"]["pain_now"] = {"between": [1, 2]}
    with pytest.raises(ValueError, match="vocabulary pain_now"):
        build_profile(raw)
    raw = _profile_raw()
    raw["encounters"][0]["procedures"][0]["dx_groups"] = [1, 9]
    with pytest.raises(ValueError, match="dx_group 9"):
        build_profile(raw)


@pytest.mark.parametrize(
    ("group", "carc"),
    [("CO", "96"), ("PR", "170"), ("PR", "50"), ("OA", "119")],
    ids=["co-96", "pr-170", "pr-50", "oa-119"],
)
def test_profile_rejects_a_noncovered_cas_that_would_create_work_items(group: str, carc: str) -> None:
    """Non-covered lines sit on paid, unlabeled claims, so their CAS must be one the ingest rule never
    turns into a work item: group PR with a patient-responsibility CARC (96, 204, 49, 119)."""
    raw = _profile_raw()
    raw["payers"][0]["noncovered"] = [{"kinds": ["therapy"], "group": group, "carc": carc, "rarcs": []}]
    with pytest.raises(ValueError, match="patient-responsibility CARC"):
        build_profile(raw)


def test_profile_validates_the_generator_features() -> None:
    """Swap pools, modifier strips, vocabulary bounds, region pairing and payer rules are checked at load."""
    raw = _profile_raw()
    raw["scenarios"] = [
        {**s, "claim_setup": {"swap_diagnosis_inconsistent": ["M54.2"]}}
        if s["id"] == "diagnosis_inconsistent_with_cmt"
        else s
        for s in raw["scenarios"]
    ]
    with pytest.raises(ValueError, match="swap pool code 'M54.2' is billed"):
        build_profile(raw)
    raw = _profile_raw()
    raw["scenarios"][0]["claim_setup"] = {"swap_diagnosis_inconsistent": True}
    with pytest.raises(ValueError, match="non-empty list of diagnoses"):
        build_profile(raw)
    raw = _profile_raw()
    raw["scenarios"][0]["claim_setup"] = {"strip_target_modifiers": "AT"}
    with pytest.raises(ValueError, match="strip_target_modifiers"):
        build_profile(raw)
    raw = _profile_raw()
    raw["vocabulary"]["visit_number"] = {"range": [3, "not_defined_yet"]}
    with pytest.raises(ValueError, match="earlier integer placeholder"):
        build_profile(raw)
    raw = _profile_raw()
    raw["vocabulary"]["plan"]["bundle"][1] = {"plan_frequency": "twice a week"}
    with pytest.raises(ValueError, match="same placeholders"):
        build_profile(raw)
    raw = _profile_raw()
    # an anchor whose diagnosis groups cannot pair a region every detail names
    raw["encounters"][2]["diagnosis_groups"] = [["M99.01"], ["M54.2"]]
    with pytest.raises(ValueError, match="must name a region the paired diagnosis groups cover"):
        build_profile(raw)
    raw = _profile_raw()
    raw["payers"][0]["code_map"] = {"97014": "98940"}
    with pytest.raises(ValueError, match="changes the line kind"):
        build_profile(raw)
    raw = _profile_raw()
    raw["payers"][0]["letters"] = {"denial_paragraph": "x"}
    with pytest.raises(ValueError, match="may only override"):
        build_profile(raw)
    raw = _profile_raw()
    for encounter in raw["encounters"]:
        encounter["scenario_only"] = True
    with pytest.raises(ValueError, match="not scenario_only"):
        build_profile(raw)


# --------------------------------------------------------------------------------------
# determinism
# --------------------------------------------------------------------------------------


def _file_bytes(directory: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(directory.iterdir()) if p.is_file()}


def test_generate_is_deterministic(tmp_path: Path) -> None:
    first = generate(seed=123, n_claims=60, out_dir=tmp_path / "a")
    second = generate(seed=123, n_claims=60, out_dir=tmp_path / "b")
    assert first == second
    assert _file_bytes(tmp_path / "a") == _file_bytes(tmp_path / "b")
    # a Profile instance is accepted in place of the profile name and changes nothing
    third = generate(seed=123, n_claims=60, out_dir=tmp_path / "d", profile=load_profile(PROFILE))
    assert third == first and _file_bytes(tmp_path / "d") == _file_bytes(tmp_path / "a")
    generate(seed=124, n_claims=60, out_dir=tmp_path / "c")
    assert (
        _file_bytes(tmp_path / "c")["claims_export.csv"] != _file_bytes(tmp_path / "a")["claims_export.csv"]
    )


def test_committed_fixture_is_reproducible(tmp_path: Path) -> None:
    assert FIXTURE_DIR.is_dir(), (
        "run generate(seed=20261028, n_claims=200, out_dir='synthetic/fixtures/labeled_set_v2')"
    )
    generate(seed=FIXTURE_SEED, n_claims=FIXTURE_N_CLAIMS, out_dir=tmp_path)
    regenerated = _file_bytes(tmp_path)
    committed = {k: v for k, v in _file_bytes(FIXTURE_DIR).items() if k != "REVIEW.md"}
    assert set(regenerated) == set(committed)
    for name, content in committed.items():
        assert regenerated[name] == content, f"{name} differs from the committed fixture"
    assert all(b"\r\n" not in content for content in committed.values())


# --------------------------------------------------------------------------------------
# committed fixture: counts and spread
# --------------------------------------------------------------------------------------


def test_fixture_counts_and_category_spread() -> None:
    labels = _fixture_labels()
    manifest = _fixture_manifest()
    profile = load_profile(PROFILE)
    assert manifest["seed"] == FIXTURE_SEED and manifest["n_claims"] == FIXTURE_N_CLAIMS
    assert manifest["profile"] == PROFILE
    assert len(labels) >= 150
    assert manifest["counts"]["denials"] == len(labels)
    assert manifest["counts"]["claims"] == FIXTURE_N_CLAIMS
    by_category: dict[str, int] = {}
    for label in labels:
        by_category[label["category"]] = by_category.get(label["category"], 0) + 1
    assert MIN_DENIALS_OTHER <= MIN_DENIALS_PER_CATEGORY == 6
    for category in DenialCategory:  # every category at least six times, `other` included
        assert by_category.get(str(category), 0) >= MIN_DENIALS_PER_CATEGORY, category
    assert by_category == manifest["counts"]["denials_by_category"]
    assert sum(1 for label in labels if label["injection_planted"]) >= 10
    assert manifest["counts"]["injection_planted"] == sum(1 for label in labels if label["injection_planted"])
    scenario_ids = {s.id for s in profile.scenarios}
    assert {label["scenario_id"] for label in labels} == scenario_ids, (
        "every scenario appears in the labeled set"
    )
    for label in labels:
        category = DenialCategory(label["category"])
        action = ActionType(label["expected_action"])
        if category not in ACTIONABLE_CATEGORIES:
            assert action == ActionType.NO_ACTION, label["scenario_id"]
        assert label["level"] in ("claim", "line")
        assert label["group"] in GROUP_CODES
        assert Decimal(label["denied_amount"]) > 0
        assert Decimal(label["dollar_value"]) >= Decimal(label["denied_amount"])
        assert isinstance(label["injection_planted"], bool)
        scenario = profile.scenario(label["scenario_id"])
        assert (
            str(scenario.category),
            str(scenario.expected_action),
            scenario.carc,
            list(scenario.rarcs),
            scenario.group,
            scenario.level,
        ) == (
            label["category"],
            label["expected_action"],
            label["carc"],
            label["rarcs"],
            label["group"],
            label["level"],
        )
        if scenario.requires_payers:
            assert label["payer_id"] in scenario.requires_payers, label["scenario_id"]
    assert manifest["counts"]["dropped_scenarios"] == []
    for name, digest in manifest["file_sha256"].items():
        assert hashlib.sha256((FIXTURE_DIR / name).read_bytes()).hexdigest() == digest, name


# --------------------------------------------------------------------------------------
# claims_export.csv
# --------------------------------------------------------------------------------------


def test_claims_export_columns_and_rows() -> None:
    with open(FIXTURE_DIR / "claims_export.csv", newline="", encoding="utf-8") as handle:
        header = next(csv.reader(handle))
    assert tuple(header) == CLAIM_EXPORT_COLUMNS
    rows = _fixture_rows()
    profile = load_profile(PROFILE)
    payers = {p.payer_id: p for p in profile.payers}
    provider_npis = {p.npi for p in profile.providers}
    for row in rows:
        parsed = ClaimExportRow(**row)
        assert parsed.claim_frequency_code == "1" and parsed.original_claim_control_number is None
        assert parsed.place_of_service == "11"
        assert parsed.billing_provider_npi == str(profile.clinic["npi"])
        assert parsed.rendering_provider_npi in provider_npis
        # the chiropractor orders the clinic's own films: the only referring NPI is the rendering one
        assert parsed.referring_provider_npi in (None, parsed.rendering_provider_npi)
        assert set(parsed.modifiers) <= set(profile.modifiers), row["modifiers"]
        kind = profile.codes[parsed.procedure_code].kind
        assert kind not in payers[parsed.payer_id].excluded_kinds, (parsed.claim_control_number, kind)
        assert parsed.payer_id in payers and parsed.payer_name == payers[parsed.payer_id].name
        assert parsed.member_id and parsed.member_id.startswith(payers[parsed.payer_id].member_id_prefix)
        assert parsed.member_id[len(payers[parsed.payer_id].member_id_prefix) :].isdigit()
        assert parsed.line_item_control_number == f"{parsed.claim_control_number}-{parsed.line_number}"
        assert parsed.diagnosis_codes and all(
            1 <= p <= len(parsed.diagnosis_codes) for p in parsed.diagnosis_pointers
        )
        assert parsed.charge_amount > 0 and parsed.units >= 1
        assert parsed.procedure_code in profile.codes
        if parsed.procedure_code.startswith("J"):
            assert parsed.ndc == profile.codes[parsed.procedure_code].ndc
        assert parsed.patient_sex in ("M", "F", "U")
        assert parsed.patient_dob is not None and parsed.original_submission_date is not None
    # one claim's rows are contiguous and numbered 1..n; the total equals the line sum
    grouped = _group_rows(rows)
    assert len(grouped) == FIXTURE_N_CLAIMS
    seen: list[str] = []
    for row in rows:
        if not seen or seen[-1] != row["claim_control_number"]:
            assert row["claim_control_number"] not in seen, "claim rows are not contiguous"
            seen.append(row["claim_control_number"])
    assert seen == sorted(seen) and seen[0] == "CLM000001"
    for ccn, claim_rows in grouped.items():
        assert [int(r["line_number"]) for r in claim_rows] == list(range(1, len(claim_rows) + 1)), ccn
        assert sum(Decimal(r["charge_amount"]) for r in claim_rows) == Decimal(
            claim_rows[0]["claim_total_charge"]
        )
        assert len({r["claim_total_charge"] for r in claim_rows}) == 1


def test_claims_export_dates_and_patients() -> None:
    rows = _fixture_rows()
    labels = {label["claim_control_number"]: label for label in _fixture_labels()}
    profile = load_profile(PROFILE)
    payers = {p.payer_id: p for p in profile.payers}
    grouped = _group_rows(rows)
    by_account: dict[str, set[tuple[str, ...]]] = {}
    claims_per_patient: dict[str, int] = {}
    for ccn, claim_rows in grouped.items():
        row = claim_rows[0]
        service = date.fromisoformat(row["service_date"])
        submission = date.fromisoformat(row["original_submission_date"])
        assert DEFAULT_AS_OF - timedelta(days=150) <= service <= DEFAULT_AS_OF - timedelta(days=25)
        label = labels.get(ccn)
        window = payers[row["payer_id"]].timely_filing_days
        scenario_id = label["scenario_id"] if label else None
        if scenario_id == "timely_filing_expired":
            assert (submission - service).days >= window + 10, ccn
        else:
            assert 2 <= (submission - service).days <= 10, ccn
            if scenario_id == "timely_filing_in_window":
                assert (submission - service).days <= window
        kind_rows: dict[str, list[dict[str, str]]] = {}
        for r in claim_rows:
            kind_rows.setdefault(profile.codes[r["procedure_code"]].kind, []).append(r)
        target = next(
            (r for r in claim_rows if label and r["line_number"] == str(label["line_number"])), None
        )
        if scenario_id == "auth_number_not_on_claim":
            assert row["prior_auth_number"], "CO-16 M62 claims carry a prior authorization number"
        if scenario_id in ("auth_ma_plan_of_care_absent", "auth_medicaid_service_limit"):
            assert not row["prior_auth_number"]
        if scenario_id == "modifier_25_missing_em_with_cmt":
            em_rows = kind_rows["em"]
            assert em_rows and "25" not in em_rows[0]["modifiers"].split("|") and kind_rows["cmt"]
        if scenario_id == "modifier_at_missing":
            assert target is not None and target["procedure_code"] in ("98940", "98941", "98942")
            assert "AT" not in target["modifiers"].split("|")
        if scenario_id == "modifier_gp_missing":
            assert target is not None and profile.codes[target["procedure_code"]].kind == "therapy"
            assert "GP" not in target["modifiers"].split("|")
        if scenario_id == "pr_xray_without_gy":
            assert target is not None and profile.codes[target["procedure_code"]].kind == "xray"
            assert "GY" not in target["modifiers"].split("|") and row["payer_id"] == MEDICARE
        if scenario_id == "contractual_gz_maintenance":
            assert target is not None and target["modifiers"] == "GZ" and len(claim_rows) == 1
        payer = payers[row["payer_id"]]
        ordering_expected = bool(
            set(payer.ordering_provider_kinds) & {profile.codes[r["procedure_code"]].kind for r in claim_rows}
        )
        if scenario_id == "missing_ordering_provider_xray":
            # the denied X-ray claim was sent without the ordering provider (Palmetto: unprocessable)
            assert not row["referring_provider_npi"], ccn
            assert target is not None and profile.codes[target["procedure_code"]].kind == "xray"
        elif ordering_expected:
            # every other Medicare / MA radiology claim names the treating chiropractor as the orderer
            assert row["referring_provider_npi"] == row["rendering_provider_npi"], ccn
        else:
            assert not row["referring_provider_npi"], ccn
        if scenario_id == "duplicate_second_cmt_same_day":
            cmt_rows = kind_rows["cmt"]
            assert len(cmt_rows) == 2 and cmt_rows[0]["procedure_code"] == cmt_rows[1]["procedure_code"]
        if scenario_id in SWAP_SCENARIOS:
            assert target is not None
            first_pointer = int(target["diagnosis_pointers"].split("|")[0])
            assert not row["diagnosis_codes"].split("|")[first_pointer - 1].startswith("M99"), ccn
        # every claim follows the payer's billing rules: its code substitutions, and the kind modifiers
        # on every line except the ones the scenario's claim_setup strips from its target line
        for r in claim_rows:
            assert r["procedure_code"] not in payer.code_map, (ccn, r["procedure_code"])
            kind = profile.codes[r["procedure_code"]].kind
            expected_mods = set(payer.kind_modifiers.get(kind, ()))
            if r is target:
                expected_mods -= INTENDED_STRIPS.get(scenario_id or "", set())
                if scenario_id in ("contractual_gz_maintenance", "duplicate_second_cmt_same_day"):
                    expected_mods = set()  # the GZ line's modifiers are set; the duplicate is a bare copy
            assert expected_mods <= set(r["modifiers"].split("|")), (ccn, scenario_id, r["procedure_code"])
        if label is None:
            assert kind_rows.get("cmt"), f"{ccn}: every chiropractic visit bills a spinal manipulation"
            assert all(r["diagnosis_codes"].split("|")[0].startswith("M99") for r in claim_rows), ccn
        by_account.setdefault(row["patient_account_number"], set()).add(
            (
                row["patient_first_name"],
                row["patient_last_name"],
                row["patient_dob"],
                row["member_id"],
                row["payer_id"],
            )
        )
        claims_per_patient[row["patient_account_number"]] = (
            claims_per_patient.get(row["patient_account_number"], 0) + 1
        )
    assert all(len(identity) == 1 for identity in by_account.values()), (
        "a patient's identity is stable across claims"
    )
    assert 0.6 * FIXTURE_N_CLAIMS <= len(by_account) <= 0.8 * FIXTURE_N_CLAIMS
    assert any(count >= 2 for count in claims_per_patient.values())
    assert max(claims_per_patient.values()) <= 3


def _age_at(dob: str, service: str) -> int:
    born, seen = date.fromisoformat(dob), date.fromisoformat(service)
    return seen.year - born.year - ((seen.month, seen.day) < (born.month, born.day))


def test_patients_ages_follow_the_payer() -> None:
    """Medicare and MA beneficiaries are mostly 65 and over; Medicaid members are under 65; every
    patient's age at the date of service is inside one of the payer's age bands."""
    profile = load_profile(PROFILE)
    ages: dict[str, list[int]] = {}
    for ccn, claim_rows in _group_rows(_fixture_rows()).items():
        row = claim_rows[0]
        age = _age_at(row["patient_dob"], row["service_date"])
        bands = profile.payer(row["payer_id"]).age_bands
        assert any(band.low <= age <= band.high for band in bands), (ccn, row["payer_id"], age)
        ages.setdefault(row["payer_id"], []).append(age)
    for payer_id in (MEDICARE, MA):
        over_65 = sum(1 for age in ages[payer_id] if age >= 65)
        assert over_65 >= 0.75 * len(ages[payer_id]), payer_id
    assert max(ages[MEDICAID]) < 65


def test_cmt_diagnoses_pair_by_region_and_notes_name_the_adjusted_segment() -> None:
    """Every CMT line leads with a subluxation level, each condition it points to shares a region with
    a level on the line (no cervical level with a lumbar condition, the B22 pattern), a 3-5 region
    code carries two pairs, and the segment a note names for the level is one the detail adjusted."""
    profile = load_profile(PROFILE)
    labels = {label["claim_control_number"]: label for label in _fixture_labels()}
    notes: dict[str, list[str]] = {}
    for note in _read_csv(FIXTURE_DIR / "notes.csv"):
        notes.setdefault(note["claim_control_number"], []).append(note["text"])
    checked = two_pairs = 0
    for ccn, claim_rows in _group_rows(_fixture_rows()).items():
        label = labels.get(ccn)
        if label and label["scenario_id"] in SWAP_SCENARIOS:
            continue  # the level was swapped for a symptom code on purpose; the notes name it
        dx = claim_rows[0]["diagnosis_codes"].split("|")
        for row in claim_rows:
            if row["procedure_code"] not in ("98940", "98941", "98942"):
                continue
            codes = [dx[int(p) - 1] for p in row["diagnosis_pointers"].split("|")]
            assert codes[0].startswith("M99.0"), (ccn, codes)
            levels = [c for c in codes if c.startswith("M99.0")]
            conditions = [c for c in codes if not c.startswith("M99.0")]
            assert conditions, (ccn, codes)
            level_regions = {region for c in levels for region in profile.diagnoses[c].regions}
            for condition in conditions:
                assert set(profile.diagnoses[condition].regions) & level_regions, (ccn, codes)
            for level in levels:
                regions = set(profile.diagnoses[level].regions)
                assert any(regions & set(profile.diagnoses[c].regions) for c in conditions), (ccn, codes)
            if row["procedure_code"] in ("98941", "98942"):
                assert len(levels) == 2, (ccn, row["procedure_code"], codes)
                two_pairs += 1
            checked += 1
        text = " ".join(notes.get(ccn, []))
        adjusted = set()
        for detail in re.findall(r"[^.;:]*adjusted: (?:one|two|three|four|five) spinal regions?", text):
            adjusted.update(SEGMENT_RE.findall(detail))
        named = set(re.findall(r"\bat ((?:[CTL]\d-[CTLS]\d)|(?:left|right) sacroiliac joint)\b", text))
        if adjusted and named:
            assert named <= adjusted, (ccn, sorted(named), sorted(adjusted))
    assert checked > 150 and two_pairs > 80


def test_notes_keep_plan_arithmetic_consistent() -> None:
    """A visit number never exceeds the planned visits, and a plan's frequency, length and visit count
    agree (the vocabulary bundle draws them together)."""
    plans = {
        ("three times a week for two weeks, then twice a week", "6"): "14",
        ("twice a week", "4"): "8",
        ("twice a week", "6"): "12",
        ("three times a week", "4"): "12",
    }
    seen = 0
    for note in _read_csv(FIXTURE_DIR / "notes.csv"):
        text = note["text"]
        for visit, planned in re.findall(r"[Vv]isit (\d+) of (?:the planned )?(\d+)", text):
            assert int(visit) <= int(planned), (note["claim_control_number"], visit, planned)
            assert planned in plans.values(), (note["claim_control_number"], planned)
            seen += 1
        for frequency, weeks in re.findall(
            r"(three times a week for two weeks, then twice a week|twice a week|three times a week) for (\d) weeks",
            text,
        ):
            assert (frequency, weeks) in plans, (note["claim_control_number"], frequency, weeks)
    assert seen >= 5


def test_medicare_and_ma_payments_carry_sequestration() -> None:
    """Every paid line of the payers that withhold sequestration carries CO-253 at 2% of the payment;
    the other payers never show it."""
    profile = load_profile(PROFILE)
    found = _claims_in_fixture_remits()
    paid_lines = 0
    for _ccn, (payer_id, claim) in found.items():
        rate = profile.payer(payer_id).sequestration_rate
        for svc in claim["svcs"]:
            paid = Decimal(svc["svc"][3])
            withheld = [amount for group, carc, amount in svc["cas"] if (group, carc) == ("CO", "253")]
            if rate > 0 and paid > 0:
                assert len(withheld) == 1, (_ccn, svc["svc"])
                assert withheld[0] == ((paid + withheld[0]) * rate).quantize(Decimal("0.01")), _ccn
                paid_lines += 1
            else:
                assert not withheld, (_ccn, payer_id)
    assert paid_lines > 20


def test_swapped_primary_comes_from_the_declared_pool_on_every_seed(tmp_path: Path) -> None:
    """The inconsistent-diagnosis scenarios swap the CMT line's level for a code from their declared
    pool only, never for another subluxation level, on any seed."""
    profile = load_profile(PROFILE)
    pools = {s: set(profile.scenario(s).claim_setup["swap_diagnosis_inconsistent"]) for s in SWAP_SCENARIOS}
    swaps = 0
    for seed in range(1, 9):
        out = tmp_path / f"s{seed}"
        generate(seed=seed, n_claims=200, out_dir=out)
        rows = _group_rows(_read_csv(out / "claims_export.csv"))
        for label in json.loads((out / "labels.json").read_text(encoding="utf-8")):
            if label["scenario_id"] not in pools:
                continue
            claim_rows = rows[label["claim_control_number"]]
            line = claim_rows[label["line_number"] - 1]
            first = int(line["diagnosis_pointers"].split("|")[0])
            swapped = claim_rows[0]["diagnosis_codes"].split("|")[first - 1]
            assert swapped in pools[label["scenario_id"]], (seed, label["claim_control_number"], swapped)
            swaps += 1
    assert swaps >= 20


# --------------------------------------------------------------------------------------
# 835 files
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", _remit_files(), ids=lambda p: p.name)
def test_835_envelope_and_balancing(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    assert "\r" not in text and text.endswith("~\n")
    parsed = _parse_835(text)
    isa = parsed["isa"]
    assert len(isa) == ISA_LENGTH and isa.startswith("ISA*00*") and isa.endswith("*:~")
    segments = parsed["segments"]
    tags = [s[0] for s in segments]
    assert tags[:3] == ["ISA", "GS", "ST"] and tags[-3:] == ["SE", "GE", "IEA"]
    isa_seg, gs, st, se, ge, iea = (
        segments[0],
        segments[1],
        segments[2],
        segments[-3],
        segments[-2],
        segments[-1],
    )
    assert isa_seg[13] == iea[2] and len(isa_seg[13]) == 9
    assert gs[6] == ge[2] and ge[1] == "1" and gs[8] == "005010X221A1" and gs[1] == "HP"
    assert st[1] == "835" and st[2] == se[2]
    assert int(se[1]) == len(segments) - 4  # ST..SE inclusive
    payer_id = path.stem.split("_", 1)[1]
    assert isa_seg[6].strip() == payer_id
    bpr = next(s for s in segments if s[0] == "BPR")
    assert bpr[16] and len(bpr[16]) == 8 and bpr[4] in ("ACH", "CHK", "NON")
    assert next(s for s in segments if s[0] == "TRN")[2]
    assert next(s for s in segments if s[0] == "DTM" and s[1] == "405")
    assert ["REF", "2U", payer_id] == next(s for s in segments if s[0] == "REF" and s[1] == "2U")
    payee = next(s for s in segments if s[0] == "N1" and s[1] == "PE")
    assert payee[3] == "XX" and is_valid_npi(payee[4])
    assert next(s for s in segments if s[0] == "LX")
    assert not any(s[0] == "PLB" for s in segments)
    total_paid = Decimal("0")
    for claim in parsed["claims"]:
        clp = claim["clp"]
        charge, paid, patient_resp = Decimal(clp[3]), Decimal(clp[4]), Decimal(clp[5])
        total_paid += paid
        assert clp[2] in ("1", "4") and clp[6] and clp[7] and clp[8] == "11" and clp[9] == "1"
        assert claim["nm1"]["QC"][8] == "MI" and claim["nm1"]["QC"][9]
        assert claim["nm1"]["82"][8] == "XX" and is_valid_npi(claim["nm1"]["82"][9])
        assert {"050", "232", "233"} <= set(claim["dtm"])
        assert claim["svcs"], "every claim has SVC loops"
        svc_charge = sum(Decimal(s["svc"][2]) for s in claim["svcs"])
        svc_paid = sum(Decimal(s["svc"][3]) for s in claim["svcs"])
        all_cas = list(claim["cas"]) + [t for s in claim["svcs"] for t in s["cas"]]
        assert svc_charge == charge and svc_paid == paid, clp[1]
        assert charge - paid == sum(amount for _, _, amount in all_cas), clp[1]
        assert patient_resp == sum(amount for group, _, amount in all_cas if group == "PR"), clp[1]
        for index, svc in enumerate(claim["svcs"], start=1):
            assert svc["svc"][1].startswith("HC:")
            assert svc["dtm"].get("472") and svc["ref"].get("6R") == f"{clp[1]}-{index}"
            assert svc["svc"][5] and Decimal(svc["svc"][5]) >= 1
    assert Decimal(bpr[2]) == total_paid


def test_every_claim_in_exactly_one_835_with_matching_payer() -> None:
    rows = _fixture_rows()
    grouped = _group_rows(rows)
    found = _claims_in_fixture_remits()
    assert set(found) == set(grouped)
    for ccn, claim_rows in grouped.items():
        payer_id, claim = found[ccn]
        assert payer_id == claim_rows[0]["payer_id"]
        assert claim["nm1"]["QC"][9] == claim_rows[0]["member_id"]
        assert claim["nm1"]["QC"][3] == claim_rows[0]["patient_last_name"].upper()
        assert Decimal(claim["clp"][3]) == Decimal(claim_rows[0]["claim_total_charge"])
        assert claim["dtm"]["232"] == claim_rows[0]["service_date"].replace("-", "")
        assert claim["dtm"]["050"] >= claim_rows[0]["original_submission_date"].replace("-", "")
        for svc, row in zip(claim["svcs"], claim_rows, strict=True):
            composite = svc["svc"][1].split(":")
            assert composite[1] == row["procedure_code"]
            assert composite[2:] == [m for m in row["modifiers"].split("|") if m]
            assert Decimal(svc["svc"][2]) == Decimal(row["charge_amount"])


def _denial_dollar_value(claim: dict[str, Any]) -> Decimal:
    """The ingest rule (SPEC 6): every CO/PI/OA/PR adjustment with a non-informational CARC and a
    positive amount, at both levels; the full charge for a CLP02 4 claim without one."""
    total = Decimal("0")
    for group, carc, amount in list(claim["cas"]) + [t for s in claim["svcs"] for t in s["cas"]]:
        if group in DENIAL_GROUPS and carc not in INFORMATIONAL_CARCS and amount > 0:
            total += amount
    return total if total > 0 or claim["clp"][2] != "4" else Decimal(claim["clp"][3])


def test_labels_match_remittance_adjustments() -> None:
    found = _claims_in_fixture_remits()
    profile = load_profile(PROFILE)
    groups_seen: set[str] = set()
    for label in _fixture_labels():
        payer_id, claim = found[label["claim_control_number"]]
        assert payer_id == label["payer_id"]
        carc, rarcs, amount = label["carc"], label["rarcs"], Decimal(label["denied_amount"])
        group = label["group"]
        groups_seen.add(group)
        if label["level"] == "claim":
            assert claim["clp"][2] == "4" and Decimal(claim["clp"][4]) == 0
            assert (group, carc, amount) in claim["cas"]
            assert claim["lq"] == rarcs
            assert label["line_number"] is None
            assert all(not svc["cas"] for svc in claim["svcs"])
        else:
            assert label["line_number"] is not None
            svc = claim["svcs"][label["line_number"] - 1]
            assert svc["svc"][1].split(":")[1] == label["procedure_code"]
            assert (group, carc, amount) in svc["cas"]
            assert svc["lq"] == rarcs
            assert Decimal(svc["svc"][3]) == 0
            assert not claim["cas"] and not claim["lq"]
            if label["scenario_id"] == "med_nec_maintenance_review":
                assert any(c == "45" for _, c, _ in svc["cas"]), (
                    "allowed-amount rule leaves CO-45 on the line"
                )
            # the other lines follow the payer's rules: paid, or the payer's non-covered CAS
            payer = profile.payer(payer_id)
            for index, other in enumerate(claim["svcs"], start=1):
                if index == label["line_number"]:
                    continue
                code = other["svc"][1].split(":")[1]
                rule = payer.noncovered_rule(profile.codes[code].kind)
                if rule is not None:
                    assert other["cas"] == [(rule.group, rule.carc, Decimal(other["svc"][2]))], (label, index)
                    assert other["lq"] == list(rule.rarcs) and Decimal(other["svc"][3]) == 0
                else:
                    assert ("CO", "45") in {(g, c) for g, c, _ in other["cas"]}, (label, index)
        # the label's dollar value is what the ingest rule stores on the work item
        assert Decimal(label["dollar_value"]) == _denial_dollar_value(claim), label["claim_control_number"]
        # a PR-group denial is the patient's share: CLP05 carries it
        if group == "PR":
            assert Decimal(claim["clp"][5]) >= amount
    assert groups_seen == {"CO", "OA", "PR"}


def test_paid_claims_carry_contractual_adjustments_and_no_label() -> None:
    labeled = {label["claim_control_number"] for label in _fixture_labels()}
    found = _claims_in_fixture_remits()
    profile = load_profile(PROFILE)
    unlabeled = [ccn for ccn in found if ccn not in labeled]
    assert len(unlabeled) == FIXTURE_N_CLAIMS - len(labeled) and unlabeled
    saw_patient_responsibility = False
    saw_noncovered: set[str] = set()
    for ccn in unlabeled:
        payer_id, claim = found[ccn]
        payer = profile.payer(payer_id)
        assert claim["clp"][2] == "1" and Decimal(claim["clp"][4]) >= 0
        if Decimal(claim["clp"][4]) == 0:
            # processed, nothing paid: the patient's cost share (a deductible or a copay at least the
            # allowed amount) took every allowed dollar; no denial, so no label
            allowed = sum(
                (Decimal(s["amt"]["B6"]) for s in claim["svcs"] if s["amt"].get("B6")), Decimal("0")
            )
            cost_share = sum(
                (a for s in claim["svcs"] for g, c, a in s["cas"] if g == "PR" and c in ("1", "2", "3")),
                Decimal("0"),
            )
            assert allowed > 0 and cost_share == allowed, ccn
        assert not claim["cas"] and not claim["lq"]
        for svc in claim["svcs"]:
            code = svc["svc"][1].split(":")[1]
            rule = payer.noncovered_rule(profile.codes[code].kind)
            if rule is not None:
                # a line kind the payer never pays: the informational PR CAS, no payment, no allowed amount
                assert svc["cas"] == [(rule.group, rule.carc, Decimal(svc["svc"][2]))], (ccn, code)
                assert svc["lq"] == list(rule.rarcs) and Decimal(svc["svc"][3]) == 0 and not svc["amt"]
                saw_noncovered.add(payer_id)
                continue
            carcs = {(g, c) for g, c, _ in svc["cas"]}
            assert ("CO", "45") in carcs
            assert all(c in INFORMATIONAL_CARCS for _, c, _ in svc["cas"])
            assert all(g in ("CO", "PR") for g, _, _ in svc["cas"])
            assert svc["amt"].get("B6"), "paid lines carry the allowed amount"
            if any(g == "PR" for g, _, _ in svc["cas"]):
                saw_patient_responsibility = True
                assert all(c in ("1", "2", "3") for g, c, _ in svc["cas"] if g == "PR")
        # CLP05 is every PR amount on the claim, the non-covered lines included
        assert Decimal(claim["clp"][5]) == sum(
            (a for s in claim["svcs"] for g, _, a in s["cas"] if g == "PR"), Decimal("0")
        )
    assert saw_patient_responsibility
    assert saw_noncovered == {MEDICARE, MA}, "Medicare and MA claims carry non-covered lines"


# --------------------------------------------------------------------------------------
# notes and payer documents
# --------------------------------------------------------------------------------------


def test_notes_cover_every_appeal_label() -> None:
    notes = _read_csv(FIXTURE_DIR / "notes.csv")
    rows = _group_rows(_fixture_rows())
    labels = _fixture_labels()
    profile = load_profile(PROFILE)
    authors = {p.name for p in profile.providers}
    by_ccn: dict[str, list[dict[str, str]]] = {}
    for note in notes:
        parsed = NoteRow(**note)
        assert parsed.note_date is not None and parsed.author in authors and parsed.text
        assert parsed.claim_control_number in rows
        assert note["note_date"] == rows[parsed.claim_control_number][0]["service_date"]
        by_ccn.setdefault(parsed.claim_control_number, []).append(note)
    for label in labels:
        ccn = label["claim_control_number"]
        if label["expected_action"] == "appeal":
            assert 1 <= len(by_ccn.get(ccn, [])) <= 3, f"{ccn}: appeal label needs note excerpts"
        if label["scenario_id"] in CODING_CHANGE_SCENARIOS or label["scenario_id"] in (
            "auth_number_not_on_claim",
            "missing_ordering_provider_xray",
        ):
            assert by_ccn.get(ccn), f"{ccn}: corrected-claim label needs a note excerpt"
        if label["scenario_id"] in NO_NOTE_SCENARIOS:
            assert ccn not in by_ccn, f"{ccn}: this verdict depends on the absence of notes"
        if label["scenario_id"] in NOTE_EVIDENCE_SCENARIOS:
            assert by_ccn.get(ccn), f"{ccn}: this verdict rests on the evidence in the notes"
        if label["scenario_id"] == "modifier_at_missing_maintenance_care":
            lowered = " ".join(note["text"] for note in by_ccn[ccn]).lower()
            assert "clinically stable" in lowered and "no further objective improvement" in lowered, ccn
        assert label["has_notes"] == (ccn in by_ccn)
    labeled = {label["claim_control_number"] for label in labels}
    benign = [ccn for ccn in by_ccn if ccn not in labeled]
    assert benign, "some paid claims carry benign notes"
    # the SOAP elements the MACs ask for are in the active-treatment notes, and no note names an
    # ICD-10 code the claim does not carry (the playbook would turn that into a coding change),
    # except the two inconsistent-diagnosis scenarios, whose notes name the documented M99 level
    icd = re.compile(r"(?<![A-Za-z0-9])([A-Z][0-9]{2}\.[0-9A-Z]{1,4})(?![A-Za-z0-9])")
    for label in labels:
        ccn = label["claim_control_number"]
        texts = " ".join(note["text"] for note in by_ccn.get(ccn, []))
        on_claim = set(rows[ccn][0]["diagnosis_codes"].split("|"))
        mentioned = set(icd.findall(texts))
        if label["scenario_id"] in (
            "diagnosis_subluxation_primary_missing",
            "diagnosis_inconsistent_with_cmt",
        ):
            assert mentioned - on_claim and all(c.startswith("M99") for c in mentioned - on_claim), ccn
        else:
            assert mentioned <= on_claim, (ccn, mentioned - on_claim)
        if label["scenario_id"] in ("modifier_at_missing", "med_nec_maintenance_review"):
            lowered = texts.lower()
            assert "treatment plan" in lowered and "re-examination" in lowered and "/10" in lowered, ccn
            assert "range of motion" in lowered and "maintenance" in lowered, ccn


def test_payer_documents_quote_codes_and_plant_injections() -> None:
    docs = _read_csv(FIXTURE_DIR / "payer_documents.csv")
    labels = {label["claim_control_number"]: label for label in _fixture_labels()}
    rows = _group_rows(_fixture_rows())
    profile = load_profile(PROFILE)
    payer_names = {p.payer_id: p.name for p in profile.payers}
    by_ccn: dict[str, list[dict[str, str]]] = {}
    styles_seen: set[str] = set()
    for doc in docs:
        parsed = PayerDocumentRow(**doc)
        assert parsed.document_type in ("letter", "portal") and parsed.received_date is not None
        label = labels[parsed.claim_control_number]
        text = parsed.text
        assert f"Reason Code {label['carc']}:" in text or f"Reason: {label['carc']} -" in text
        for rarc in label["rarcs"]:
            assert f"Remark Code {rarc}:" in text or f"Remark: {rarc} -" in text
        row = rows[parsed.claim_control_number][0]
        assert parsed.claim_control_number in text and row["member_id"] in text
        assert row["patient_last_name"] in text and payer_names[label["payer_id"]] in text
        assert not PHONE_RE.search(text) and not EMAIL_RE.search(text) and not SSN_RE.search(text)
        planted = [sig for sig in INJECTION_SIGNATURES if sig in text]
        if label["injection_planted"]:
            assert planted, parsed.claim_control_number
            styles_seen.update(sig for sig in planted)
        else:
            assert not planted, parsed.claim_control_number
        by_ccn.setdefault(parsed.claim_control_number, []).append(doc)
    for ccn, label in labels.items():
        assert label["has_letter"] == (ccn in by_ccn)
        if label["scenario_id"] in ("cob_misrouted_wrong_payer", "elig_ma_enrollee_billed_to_medicare"):
            assert ccn in by_ccn
            other = [name for pid, name in payer_names.items() if pid != label["payer_id"]]
            assert any(name in by_ccn[ccn][0]["text"] for name in other), (
                "the letter names the responsible payer"
            )
        if label["scenario_id"] == "elig_ma_enrollee_billed_to_medicare":
            assert payer_names[MA] in by_ccn[ccn][0]["text"] and label["payer_id"] == MEDICARE
        if label["injection_planted"]:
            assert ccn in by_ccn
    share = len(by_ccn) / len(labels)
    assert 0.3 <= share <= 0.6, f"letters cover {share:.0%} of denied claims"
    assert len(styles_seen) == len(INJECTION_SIGNATURES), "every planted style appears in the fixture"
    assert len(INJECTION_PAYLOADS) == len(INJECTION_SIGNATURES)
    notes = _read_csv(FIXTURE_DIR / "notes.csv")
    for note in notes:
        assert not PHONE_RE.search(note["text"]) and not EMAIL_RE.search(note["text"])
        assert not any(sig in note["text"] for sig in INJECTION_SIGNATURES)


def test_injection_detector_flags_planted_documents() -> None:
    from app.redact.injection import detect_injection

    labels = {label["claim_control_number"]: label for label in _fixture_labels()}
    docs = _read_csv(FIXTURE_DIR / "payer_documents.csv")
    checked = 0
    for doc in docs:
        if labels[doc["claim_control_number"]]["injection_planted"]:
            assert detect_injection(doc["text"]), doc["claim_control_number"]
            checked += 1
    assert checked >= 10


# --------------------------------------------------------------------------------------
# follow-up remittances
# --------------------------------------------------------------------------------------


def _seed_work_items(session, clinic) -> list[Any]:
    from app.models import Claim, ClaimLine, Patient, Payer, Remittance, RemittanceClaim, WorkItem

    payers = [
        Payer(
            name="Sawtooth Health Plan PPO",
            payer_id="SWPPO3",
            timely_filing_days=90,
            corrected_claim_days=90,
            appeal_days=180,
        ),
        Payer(
            name="Granite Peak Medicare Advantage",
            payer_id="GPMA2",
            timely_filing_days=365,
            corrected_claim_days=365,
            appeal_days=65,
        ),
    ]
    session.add_all(payers)
    patient = Patient(
        clinic_id=clinic.id,
        token="PT-TEST01",
        account_number="ACC100001",
        first_name="Hollis",
        last_name="Yelverton",
        dob=date(1961, 3, 14),
        sex="F",
        member_id="SW123456789",
    )
    session.add(patient)
    session.flush()
    remittance = Remittance(file_name="remit_test.835", file_sha256="0" * 64, payment_amount=Decimal("0"))
    session.add(remittance)
    session.flush()
    items = []
    reasons = [
        [
            {
                "level": "line",
                "group_code": "CO",
                "carc": "4",
                "rarcs": ["N822"],
                "amount": "150.00",
                "line_number": 1,
            }
        ],
        [
            {
                "level": "claim",
                "group_code": "CO",
                "carc": "27",
                "rarcs": [],
                "amount": "330.00",
                "line_number": None,
            }
        ],
        [],
        [
            {
                "level": "line",
                "group_code": "CO",
                "carc": "97",
                "rarcs": ["M15"],
                "amount": "165.00",
                "line_number": 2,
            }
        ],
    ]
    # dollar_value = the denied amount the pipeline stores: one line (150), the whole claim (330) ...
    dollar_values = [Decimal("150.00"), Decimal("330.00"), Decimal("330.00"), Decimal("165.00")]
    for index in range(4):
        payer = payers[index % 2]
        claim = Claim(
            clinic_id=clinic.id,
            payer_id=payer.id,
            patient_id=patient.id,
            claim_control_number=f"CLM00000{index + 1}",
            rendering_provider_npi="1428573012",
            rendering_provider_name="Rowan Halvorsen, DC",
            place_of_service="11",
            diagnosis_codes=["M99.01", "M54.2"],
            service_date_from=date(2026, 7, 14),
            service_date_to=date(2026, 7, 14),
            total_charge=Decimal("330.00"),
            original_submission_date=date(2026, 7, 20),
            status="denied",
        )
        session.add(claim)
        session.flush()
        session.add_all(
            [
                ClaimLine(
                    claim_id=claim.id,
                    line_number=1,
                    service_date=date(2026, 7, 14),
                    procedure_code="99213",
                    modifiers=["25"],
                    units=Decimal("1"),
                    charge_amount=Decimal("150.00"),
                    diagnosis_pointers=[1, 2],
                    line_item_control_number=f"{claim.claim_control_number}-1",
                ),
                ClaimLine(
                    claim_id=claim.id,
                    line_number=2,
                    service_date=date(2026, 7, 14),
                    procedure_code="98941",
                    modifiers=["AT"],
                    units=Decimal("1"),
                    charge_amount=Decimal("180.00"),
                    diagnosis_pointers=[1, 2],
                    line_item_control_number=f"{claim.claim_control_number}-2",
                ),
            ]
        )
        rc = RemittanceClaim(
            remittance_id=remittance.id,
            claim_id=claim.id,
            sequence=index + 1,
            patient_control_number=claim.claim_control_number,
            claim_status_code="4",
            charge_amount=Decimal("330.00"),
            is_denial=True,
            denied_amount=Decimal("330.00"),
        )
        session.add(rc)
        session.flush()
        item = WorkItem(
            claim_id=claim.id,
            remittance_claim_id=rc.id,
            category="modifier",
            all_reasons=reasons[index],
            status="submitted",
            dollar_value=dollar_values[index],
            submitted_claim_control_number=f"{claim.claim_control_number}-R1" if index % 2 == 0 else None,
        )
        session.add(item)
        items.append(item)
    session.commit()
    return items


def test_followup_files_pay_deny_and_reverse(session, clinic) -> None:
    from synthetic.followup import generate_followup_835, generate_followup_files

    items = _seed_work_items(session, clinic)
    plan = {items[0].id: "paid", items[1].id: "partial", items[2].id: "denied_again", items[3].id: "reversed"}
    files = generate_followup_files(session, items, plan, date(2026, 12, 1))
    assert set(files) == {"SWPPO3", "GPMA2"}
    parsed = {payer_id: _parse_835(text) for payer_id, text in files.items()}
    for payer_id, data in parsed.items():
        assert len(data["isa"]) == ISA_LENGTH
        segments = data["segments"]
        se = next(s for s in segments if s[0] == "SE")
        assert int(se[1]) == len(segments) - 4
        assert next(s for s in segments if s[0] == "REF" and s[1] == "2U")[2] == payer_id
        bpr = next(s for s in segments if s[0] == "BPR")
        assert Decimal(bpr[2]) == sum(Decimal(c["clp"][4]) for c in data["claims"])
        for claim in data["claims"]:
            charge, paid = Decimal(claim["clp"][3]), Decimal(claim["clp"][4])
            all_cas = list(claim["cas"]) + [t for s in claim["svcs"] for t in s["cas"]]
            assert charge - paid == sum(a for _, _, a in all_cas)
    claims = {c["clp"][1]: c for data in parsed.values() for c in data["claims"]}
    paid = claims["CLM000001-R1"]  # submitted control number wins
    assert paid["clp"][2] == "1" and Decimal(paid["clp"][4]) == Decimal("264.00")  # 330 minus 20%
    assert [svc["cas"] for svc in paid["svcs"]] == [
        [("CO", "45", Decimal(svc["svc"][2]) * Decimal("0.2"))] for svc in paid["svcs"]
    ]  # the contracted rate and nothing else: no denial remains
    partial = claims["CLM000002"]
    assert partial["clp"][2] == "1" and Decimal(partial["clp"][4]) == Decimal("165.00")
    # the unpaid part of every line is a real denial (the item's first reason, CO-27), never CO-45
    assert [svc["cas"] for svc in partial["svcs"]] == [
        [("CO", "27", Decimal("75"))],
        [("CO", "27", Decimal("90"))],
    ]
    denied = claims["CLM000003-R1"]
    assert denied["clp"][2] == "4" and Decimal(denied["clp"][4]) == 0
    assert denied["cas"] == [("CO", "16", Decimal("330"))], (
        "no stored reasons falls back to CO-16 at claim level"
    )
    reversed_claim = claims["CLM000004"]
    assert reversed_claim["clp"][2] == "22"
    assert Decimal(reversed_claim["clp"][3]) == Decimal("-330") and Decimal(
        reversed_claim["clp"][4]
    ) == Decimal("-264")
    # denied_again honours a line-level reason: every line carries the CARC, the referenced line its RARCs
    files2 = generate_followup_files(
        session,
        [items[0], items[3]],
        {items[0].id: "denied_again", items[3].id: "denied_again"},
        date(2026, 12, 2),
    )
    first = _parse_835(files2["SWPPO3"])["claims"][0]
    assert first["clp"][2] == "4" and not first["cas"]
    assert [t[1] for s in first["svcs"] for t in s["cas"]] == ["4", "4"] and first["svcs"][0]["lq"] == [
        "N822"
    ]
    fourth = _parse_835(files2["GPMA2"])["claims"][0]
    assert [s["lq"] for s in fourth["svcs"]] == [[], ["M15"]]
    single = generate_followup_835(session, items, plan)
    segments = split_segments(single)
    assert segments[0][0] == "ISA" and sum(1 for s in segments if s[0] == "ST") == 2
    assert next(s for s in segments if s[0] == "GE")[1] == "2"
    from app.ingest.parse_835 import parse_835
    from app.ingest.x12 import tokenize

    tokens = tokenize(files["SWPPO3"])
    assert [t.id for t in tokens][:3] == ["ISA", "GS", "ST"]
    assert sum(1 for t in tokens if t.id == "CLP") == 2
    parsed_files = parse_835(files["SWPPO3"])
    assert len(parsed_files) == 1 and len(parsed_files[0].claims) == 2


def _remittance_claim_from_parsed(claim: dict[str, Any]) -> Any:
    """A stored-shape RemittanceClaim (adjustment and line rows attached) for one parsed CLP."""
    from app.ingest.denials import detect_for, is_denial_adjustment
    from app.models import Adjustment, RemittanceClaim, RemittanceLine

    rc = RemittanceClaim(
        patient_control_number=claim["clp"][1],
        claim_status_code=claim["clp"][2],
        charge_amount=Decimal(claim["clp"][3]),
        paid_amount=Decimal(claim["clp"][4]),
    )
    for group, carc, amount in claim["cas"]:
        rc.adjustments.append(
            Adjustment(
                level="claim",
                group_code=group,
                carc=carc,
                amount=amount,
                is_denial=is_denial_adjustment(group, carc, amount),
            )
        )
    for number, svc in enumerate(claim["svcs"], start=1):
        line = RemittanceLine(
            line_number=number,
            procedure_code=svc["svc"][1].split(":")[1],
            charge_amount=Decimal(svc["svc"][2]),
            paid_amount=Decimal(svc["svc"][3]),
        )
        rc.lines.append(line)
        for group, carc, amount in svc["cas"]:
            row = Adjustment(
                level="line",
                group_code=group,
                carc=carc,
                amount=amount,
                is_denial=is_denial_adjustment(group, carc, amount),
            )
            row.remittance_line = line
            rc.adjustments.append(row)
    rc.is_denial, rc.denied_amount, _reasons = detect_for(rc)
    return rc


@pytest.mark.parametrize("dollar_value", ["330.00", "150.00", "48.00"])
def test_followup_amounts_follow_the_outcome_rule(session, clinic, dollar_value: str) -> None:
    """SPEC section 10 records `paid` when no denial adjustment remains on what the work item
    disputed (or at >= 95% of its dollar value) and `partial` when something is paid but a denial
    remains below that. The follow-up pays at the contracted rate whatever the dollar value (a
    full-charge denial overturned is paid below 95% of it) and makes a partial payment a real
    denial on the unpaid part."""
    from app.outcomes.matching import classify_outcome, remaining_denial_codes
    from synthetic.followup import PLAN_VALUES, generate_followup_files

    items = _seed_work_items(session, clinic)
    value = Decimal(dollar_value)
    for item in items:
        item.dollar_value = value
    session.commit()
    plan = dict(zip([item.id for item in items], PLAN_VALUES, strict=True))
    files = generate_followup_files(session, items, plan, date(2026, 12, 3))
    claims = {c["clp"][1]: c for text in files.values() for c in _parse_835(text)["claims"]}
    by_plan = {
        plan[item.id]: claims[item.submitted_claim_control_number or item.claim.claim_control_number]
        for item in items
    }
    for claim in by_plan.values():  # every claim balances
        charge, paid = Decimal(claim["clp"][3]), Decimal(claim["clp"][4])
        all_cas = list(claim["cas"]) + [t for s in claim["svcs"] for t in s["cas"]]
        assert charge - paid == sum(a for _, _, a in all_cas)
    threshold = value * Decimal("0.95")
    paid = Decimal(by_plan["paid"]["clp"][4])
    assert by_plan["paid"]["clp"][2] == "1" and paid == Decimal("264")  # 20% contractual on every line
    assert [svc["cas"] for svc in by_plan["paid"]["svcs"]] == [
        [("CO", "45", Decimal(svc["svc"][2]) * Decimal("0.2"))] for svc in by_plan["paid"]["svcs"]
    ]
    if value == Decimal("330"):  # the whole charge was denied: the contracted rate is below 95% of it
        assert paid < threshold
    partial = Decimal(by_plan["partial"]["clp"][4])
    assert by_plan["partial"]["clp"][2] == "1" and Decimal("0") < partial < threshold
    assert value / 2 - Decimal("0.02") <= partial <= value / 2  # half the dollar value, rounded down per line
    partial_cas = [t for svc in by_plan["partial"]["svcs"] for t in svc["cas"]]
    assert partial_cas and all((g, c) == ("CO", "27") for g, c, _ in partial_cas)  # the item's first reason
    assert by_plan["denied_again"]["clp"][2] == "4" and Decimal(by_plan["denied_again"]["clp"][4]) == 0
    assert by_plan["reversed"]["clp"][2] == "22" and Decimal(by_plan["reversed"]["clp"][4]) < 0

    reasons_of = {plan[item.id]: item.all_reasons for item in items}
    for planned, claim in by_plan.items():
        rc = _remittance_claim_from_parsed(claim)
        assert rc.is_denial == (planned in ("partial", "denied_again")), planned
        result, details = classify_outcome(rc, value, reasons=reasons_of[planned])
        assert str(result) == planned, f"plan {planned} at dollar_value {value} was recorded as {result}"
        if planned == "paid":
            assert details["reason"] == "no_denial_remaining" and details["remaining_denials"] == []
        elif planned == "partial":
            assert details["reason"] == "denial_remains"
            assert remaining_denial_codes(rc, reasons_of[planned]) == ["CO-27@1", "CO-27@2"]


# --------------------------------------------------------------------------------------
# x12 writer and small batches
# --------------------------------------------------------------------------------------


def test_x12_writer_formats_and_counts() -> None:
    assert format_amount(Decimal("150.00")) == "150"
    assert format_amount(Decimal("12.50")) == "12.5"
    assert format_amount(Decimal("-7.00")) == "-7"
    assert format_amount(0) == "0"
    many = cas_segments([("CO", str(code), Decimal("1")) for code in range(7)] + [("PR", "1", Decimal("2"))])
    # six triplets (carc, amount, empty quantity) with the trailing empty element dropped: 19 elements
    assert (
        len(many) == 3 and many[0].count("*") == 18 and many[1] == "CAS*CO*6*1~" and many[2] == "CAS*PR*1*2~"
    )
    claim = {
        "patient_control_number": "CLM000001",
        "status_code": "22",
        "charge": Decimal("-100"),
        "paid": Decimal("-80"),
        "patient_last_name": "Yel*ver~ton",
        "patient_first_name": "Hollis",
        "member_id": "SW123456789",
        "received_date": date(2026, 11, 1),
        "statement_from": date(2026, 7, 14),
        "statement_to": date(2026, 7, 14),
        "rarcs": ["N4"],
        "lines": [
            {
                "procedure": "99213",
                "modifiers": ["25"],
                "charge": Decimal("-100"),
                "paid": Decimal("-80"),
                "units": 1,
                "service_date": date(2026, 7, 14),
                "line_item_control_number": "CLM000001-1",
                "adjustments": [("CO", "45", Decimal("-20"))],
                "rarcs": [],
            }
        ],
    }
    clinic = load_profile(PROFILE).clinic
    text = build_835(
        payer={"name": "Sawtooth Health Plan PPO", "payer_id": "SWPPO3", "tax_id": "923456789"},
        payee={"name": clinic["name"], "npi": str(clinic["npi"]), "tax_id": clinic["tax_id"]},
        claims=[claim],
        payment_date=date(2026, 12, 1),
        production_date=date(2026, 12, 1),
        trace_number="TRN1",
        interchange_control_number=42,
        sender_id="SWPPO3",
        receiver_id=str(clinic["npi"]),
    )
    segments = split_segments(text)
    assert len(text.split("\n", 1)[0]) == ISA_LENGTH and segments[0][13] == "000000042"
    assert segments[-1] == ["IEA", "1", "000000042"]
    se = next(s for s in segments if s[0] == "SE")
    assert int(se[1]) == len(segments) - 4
    assert next(s for s in segments if s[0] == "BPR")[1:5] == ["H", "-80", "C", "NON"]
    clp_index = next(i for i, s in enumerate(segments) if s[0] == "CLP")
    assert segments[clp_index][1:5] == ["CLM000001", "22", "-100", "-80"]
    assert segments[clp_index + 1][:4] == ["NM1", "QC", "1", "YEL VER TON"]
    lq_index = next(i for i, s in enumerate(segments) if s[0] == "LQ")
    svc_index = next(i for i, s in enumerate(segments) if s[0] == "SVC")
    assert lq_index < svc_index, "claim-level remark codes precede the service loops"
    assert segments[svc_index][1] == "HC:99213:25"


def test_small_batch_and_custom_rates(tmp_path: Path) -> None:
    manifest = generate(seed=7, n_claims=12, out_dir=tmp_path / "small")
    assert manifest["counts"]["claims"] == 12 and 1 <= manifest["counts"]["denials"] <= 12
    labels = json.loads((tmp_path / "small" / "labels.json").read_text(encoding="utf-8"))
    assert len(labels) == manifest["counts"]["denials"]
    found: dict[str, int] = {}
    for path in _remit_files(tmp_path / "small"):
        for claim in _parse_835(path.read_text(encoding="utf-8"))["claims"]:
            found[claim["clp"][1]] = found.get(claim["clp"][1], 0) + 1
    assert all(found.get(label["claim_control_number"]) == 1 for label in labels)
    realistic = generate(
        seed=7, n_claims=100, out_dir=tmp_path / "realistic", denial_rate=0.35, injection_rate=0.0
    )
    assert 30 <= realistic["counts"]["denials"] <= 40 and realistic["counts"]["injection_planted"] == 0
    rows = _read_csv(tmp_path / "realistic" / "claims_export.csv")
    for row in rows:
        ClaimExportRow(**row)
    with pytest.raises(ValueError):
        generate(seed=1, n_claims=0, out_dir=tmp_path / "none")


def test_generate_survives_name_list_exhaustion(tmp_path: Path, monkeypatch) -> None:
    """More patients than distinct names must not hang: names may repeat, account numbers never do."""
    monkeypatch.setattr(names, "FIRST_NAMES", ("Adalyn", "Bexley"))
    monkeypatch.setattr(names, "LAST_NAMES", ("Amberly",))
    manifest = generate(seed=3, n_claims=10, out_dir=tmp_path)
    rows = _read_csv(tmp_path / "claims_export.csv")
    accounts = {row["patient_account_number"] for row in rows}
    assert manifest["counts"]["patients"] == 7 and len(accounts) == 7
    assert {(row["patient_first_name"], row["patient_last_name"]) for row in rows} <= {
        ("Adalyn", "Amberly"),
        ("Bexley", "Amberly"),
    }
    assert len({row["member_id"] for row in rows}) == 7


def test_generate_gives_every_claim_a_line(tmp_path: Path) -> None:
    """Encounter templates whose E/M and procedure slots are all probabilistic can draw nothing;
    the generator must still produce a claim with a line, a positive charge and a positive label."""
    raw = _profile_raw()
    tags = sorted({tag for scenario in raw["scenarios"] for tag in (scenario.get("requires_tags") or [])})
    for payer in raw["payers"]:  # every payer pays every kind here: the test is about the line fallback
        payer.pop("noncovered", None)
        payer.pop("excluded_kinds", None)
    raw["encounters"] = [
        {
            "id": "maybe_nothing",
            "weight": 1,
            "tags": tags,
            "em_codes": ["99213"],
            "em_probability": 0.0,
            "em_modifier_25": True,
            "procedures": [{"codes": ["98940"], "probability": 0.0}],
            "diagnosis_groups": [["M99.01"]],
        },
        {
            "id": "procedure_only",
            "weight": 1,
            "tags": [],
            "em_codes": [],
            "procedures": [{"codes": ["97110"], "probability": 0.0}],
            "diagnosis_groups": [["M54.2"]],
        },
    ]
    manifest = generate(seed=5, n_claims=30, out_dir=tmp_path, profile=build_profile(raw, source="test"))
    grouped = _group_rows(_read_csv(tmp_path / "claims_export.csv"))
    assert len(grouped) == 30 and manifest["counts"]["lines"] >= 30
    codes_seen: set[str] = set()
    for claim_rows in grouped.values():
        assert claim_rows
        for row in claim_rows:
            parsed = ClaimExportRow(**row)
            assert parsed.charge_amount > 0
            codes_seen.add(parsed.procedure_code)
    assert {"99213", "97110"} <= codes_seen
    labels = json.loads((tmp_path / "labels.json").read_text(encoding="utf-8"))
    assert labels and all(Decimal(label["denied_amount"]) > 0 for label in labels)


def test_generate_refuses_an_encounter_with_nothing_billable_to_the_payer(tmp_path: Path) -> None:
    """A claim never falls back onto a line kind the clinic does not bill to the payer
    (`excluded_kinds`): an encounter with nothing billable to the payer is a profile error."""
    raw = _profile_raw()
    tags = sorted({tag for scenario in raw["scenarios"] for tag in (scenario.get("requires_tags") or [])})
    for payer in raw["payers"]:
        payer.pop("noncovered", None)
        payer["excluded_kinds"] = ["therapy"]
    raw["encounters"] = [
        {
            "id": "therapy_only",
            "weight": 1,
            "tags": tags,
            "em_codes": [],
            "procedures": [{"codes": ["97110"]}],
            "diagnosis_groups": [["M54.2"]],
        }
    ]
    with pytest.raises(ValueError, match="has no line the clinic bills"):
        generate(seed=5, n_claims=5, out_dir=tmp_path, profile=build_profile(raw, source="test"))


# SHA-256 of NPIs that pass the check digit but turned out to belong to real providers in the CMS
# NPPES registry (the dermatology clinic's and the first chiropractic clinic's). Only the hashes are
# kept, so the repository never carries a real provider's identifier; they must never come back.
KNOWN_REAL_NPI_SHA256 = frozenset(
    {
        "7da336bb15e6412bbd3f7e9b6f073c13a900b62fd4adb9223a1f2803c96dd74b",
        "5377961710501239cd3d654708f863ce9036f59c0c64bcbfca6f1672334decc3",
    }
)


def test_no_known_real_npi_in_the_profile_or_the_fixture() -> None:
    """The check digit cannot tell an unassigned NPI from a real one (synthetic/README.md, Rules: look
    every new synthetic NPI up in NPPES first); the ones found real are kept out for good."""
    texts = [(PROFILES_DIR / f"{PROFILE}.yaml").read_text(encoding="utf-8")]
    texts.extend(path.read_text(encoding="utf-8") for path in sorted(FIXTURE_DIR.iterdir()) if path.is_file())
    candidates = {token for text in texts for token in re.findall(r"(?<!\d)\d{10}(?!\d)", text)}
    assert candidates, "the profile and the fixture carry NPIs"
    hits = [
        npi for npi in candidates if hashlib.sha256(npi.encode("ascii")).hexdigest() in KNOWN_REAL_NPI_SHA256
    ]
    assert not hits, "a known real NPI is back in the profile or the fixture"


def test_allowed_amount_never_exceeds_charge(tmp_path: Path) -> None:
    """A payer ratio of 1.0 plus jitter must not pay more than the charge or write a negative CO-45."""
    raw = _profile_raw()
    for payer in raw["payers"]:
        payer["allowed_ratio"] = 1.0
    generate(seed=11, n_claims=40, out_dir=tmp_path, profile=build_profile(raw, source="test"))
    paid_in_full = 0
    for path in _remit_files(tmp_path):
        for claim in _parse_835(path.read_text(encoding="utf-8"))["claims"]:
            assert all(amount >= 0 for _, _, amount in claim["cas"])
            for svc in claim["svcs"]:
                assert Decimal(svc["svc"][3]) <= Decimal(svc["svc"][2])
                assert all(amount >= 0 for _, _, amount in svc["cas"])
                if Decimal(svc["svc"][3]) == Decimal(svc["svc"][2]):
                    paid_in_full += 1
    assert paid_in_full > 0
