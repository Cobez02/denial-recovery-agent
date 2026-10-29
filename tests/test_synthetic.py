"""Tests for the synthetic data module (docs/SPEC.md section 12).

The committed fixture set under synthetic/fixtures/labeled_set_v1 is checked for its counts, its
reproducibility and the exact file formats the ingest module reads (SPEC sections 4.1 and 4.2).
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
    PROFILES_DIR,
    build_profile,
    is_valid_npi,
    load_profile,
    make_npi,
    npi_check_digit,
)
from synthetic.x12_writer import ISA_LENGTH, build_835, cas_segments, format_amount, split_segments

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "synthetic" / "fixtures" / "labeled_set_v1"
FIXTURE_SEED = 20261028
FIXTURE_N_CLAIMS = 200
INFORMATIONAL_CARCS = {"1", "2", "3", "45", "66", "94", "104", "118", "137", "144", "187", "237", "253"}
REQUIRED_SCENARIOS = {
    "elig_coverage_terminated": (DenialCategory.ELIGIBILITY, ActionType.CLINIC_QUESTION, "27"),
    "elig_patient_not_found": (DenialCategory.ELIGIBILITY, ActionType.CLINIC_QUESTION, "31"),
    "auth_missing": (DenialCategory.AUTHORIZATION, ActionType.APPEAL, "197"),
    "auth_number_not_on_claim": (DenialCategory.AUTHORIZATION, ActionType.CORRECTED_CLAIM, "15"),
    "modifier_25_missing": (DenialCategory.MODIFIER, ActionType.CORRECTED_CLAIM, "4"),
    "modifier_invalid_combination": (DenialCategory.MODIFIER, ActionType.CORRECTED_CLAIM, "4"),
    "duplicate_true": (DenialCategory.DUPLICATE, ActionType.NO_ACTION, "18"),
    "duplicate_distinct_lesion": (DenialCategory.DUPLICATE, ActionType.APPEAL, "18"),
    "timely_filing_expired": (DenialCategory.TIMELY_FILING, ActionType.NO_ACTION, "29"),
    "timely_filing_in_window": (DenialCategory.TIMELY_FILING, ActionType.APPEAL, "29"),
    "med_necessity_wart_destruction": (DenialCategory.MEDICAL_NECESSITY, ActionType.APPEAL, "50"),
    "diagnosis_inconsistent": (DenialCategory.MEDICAL_NECESSITY, ActionType.CORRECTED_CLAIM, "11"),
    "bundling_separate_sites": (DenialCategory.BUNDLING, ActionType.APPEAL, "97"),
    "bundling_unsupported": (DenialCategory.BUNDLING, ActionType.NO_ACTION, "97"),
    "bundling_ncci_236": (DenialCategory.BUNDLING, ActionType.NO_ACTION, "236"),
    "cob_other_coverage": (DenialCategory.COORDINATION_OF_BENEFITS, ActionType.CLINIC_QUESTION, "22"),
    "cob_wrong_payer": (DenialCategory.COORDINATION_OF_BENEFITS, ActionType.CORRECTED_CLAIM, "109"),
    "missing_ndc": (DenialCategory.MISSING_INFORMATION, ActionType.CORRECTED_CLAIM, "16"),
    "missing_info_unprocessable": (DenialCategory.MISSING_INFORMATION, ActionType.CLINIC_QUESTION, "16"),
    "missing_documentation_n29": (DenialCategory.MISSING_INFORMATION, ActionType.CLINIC_QUESTION, "16"),
    "missing_attachment_252": (DenialCategory.MISSING_INFORMATION, ActionType.CLINIC_QUESTION, "252"),
    "other_non_covered": (DenialCategory.OTHER, ActionType.CLINIC_QUESTION, "96"),
}
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
    profile = load_profile("dermatology")
    payers = {p.payer_id: p for p in profile.payers}
    assert list(payers) == ["NW001", "CMA02", "BH003", "EV004"]
    expected = {
        "NW001": ("Northwind Health Plan", 90, 90, 180),
        "CMA02": ("Cascade Medicare Advantage", 365, 365, 60),
        "BH003": ("Blue Harbor PPO", 120, 120, 180),
        "EV004": ("Evergreen Medicaid Plan", 180, 180, 90),
    }
    for payer_id, (name, filing, corrected, appeal) in expected.items():
        payer = payers[payer_id]
        assert (payer.name, payer.timely_filing_days, payer.corrected_claim_days, payer.appeal_days) == (
            name,
            filing,
            corrected,
            appeal,
        )
        assert payer.member_id_prefix and payer.member_id_prefix.isupper()
    assert len(profile.providers) == 2
    assert all(is_valid_npi(p.npi) for p in profile.providers)
    assert all(is_valid_npi(p.npi) for p in profile.referring_providers)
    assert is_valid_npi(str(profile.clinic["npi"]))
    assert profile.place_of_service == "11"
    em = {c for c in profile.codes if profile.codes[c].role == "em"}
    assert {"99202", "99203", "99204", "99205", "99211", "99212", "99213", "99214", "99215"} <= em
    for code in (
        "11102",
        "11103",
        "11104",
        "11105",
        "17110",
        "17111",
        "17000",
        "17003",
        "11400",
        "11406",
        "11600",
        "11606",
        "10060",
        "11900",
        "11042",
        "96910",
        "96920",
        "J3301",
        "J0702",
        "88305",
    ):
        assert code in profile.codes, code
    assert profile.codes["J3301"].ndc and profile.codes["J0702"].ndc
    for dx in (
        "L82.1",
        "L57.0",
        "D22.5",
        "D22.9",
        "D23.5",
        "C44.91",
        "C44.519",
        "L40.0",
        "L20.9",
        "B07.9",
        "L72.0",
        "L30.9",
        "L91.0",
        "Z85.828",
        "D48.5",
        "L72.3",
        "L57.8",
    ):
        assert dx in profile.diagnoses, dx
    scenarios = {s.id: s for s in profile.scenarios}
    for scenario_id, (category, action, carc) in REQUIRED_SCENARIOS.items():
        scenario = scenarios[scenario_id]
        assert (scenario.category, scenario.expected_action, scenario.carc) == (category, action, carc), (
            scenario_id
        )
        assert scenario.level in ("claim", "line")
        assert scenario.weight > 0
    assert {s.category for s in profile.scenarios} == set(ACTIONABLE_CATEGORIES)
    assert scenarios["auth_missing"].rarcs == ("N54",)
    assert scenarios["modifier_25_missing"].rarcs == ("M78",)
    assert scenarios["missing_ndc"].rarcs == ("M119",)
    assert scenarios["cob_wrong_payer"].letter_required
    assert scenarios["missing_info_unprocessable"].no_appeal_rights
    # every scenario that expects an appeal ships documentation templates
    for scenario in profile.scenarios:
        if scenario.expected_action == ActionType.APPEAL:
            assert scenario.notes, scenario.id


def _profile_raw() -> dict[str, Any]:
    import yaml

    return yaml.safe_load((PROFILES_DIR / "dermatology.yaml").read_text(encoding="utf-8"))


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
    third = generate(seed=123, n_claims=60, out_dir=tmp_path / "d", profile=load_profile("dermatology"))
    assert third == first and _file_bytes(tmp_path / "d") == _file_bytes(tmp_path / "a")
    generate(seed=124, n_claims=60, out_dir=tmp_path / "c")
    assert (
        _file_bytes(tmp_path / "c")["claims_export.csv"] != _file_bytes(tmp_path / "a")["claims_export.csv"]
    )


def test_committed_fixture_is_reproducible(tmp_path: Path) -> None:
    assert FIXTURE_DIR.is_dir(), (
        "run generate(seed=20261028, n_claims=200, out_dir='synthetic/fixtures/labeled_set_v1')"
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
    profile = load_profile("dermatology")
    assert manifest["seed"] == FIXTURE_SEED and manifest["n_claims"] == FIXTURE_N_CLAIMS
    assert len(labels) >= 150
    assert manifest["counts"]["denials"] == len(labels)
    assert manifest["counts"]["claims"] == FIXTURE_N_CLAIMS
    by_category: dict[str, int] = {}
    for label in labels:
        by_category[label["category"]] = by_category.get(label["category"], 0) + 1
    for category in ACTIONABLE_CATEGORIES:
        floor = MIN_DENIALS_OTHER if category == DenialCategory.OTHER else MIN_DENIALS_PER_CATEGORY
        assert by_category.get(str(category), 0) >= floor, category
    assert by_category == manifest["counts"]["denials_by_category"]
    assert sum(1 for label in labels if label["injection_planted"]) >= 10
    assert manifest["counts"]["injection_planted"] == sum(1 for label in labels if label["injection_planted"])
    scenario_ids = {s.id for s in profile.scenarios}
    assert {label["scenario_id"] for label in labels} == scenario_ids, (
        "every scenario appears in the labeled set"
    )
    for label in labels:
        assert DenialCategory(label["category"]) in ACTIONABLE_CATEGORIES
        ActionType(label["expected_action"])
        assert label["level"] in ("claim", "line")
        assert Decimal(label["denied_amount"]) > 0
        assert isinstance(label["injection_planted"], bool)
        scenario = profile.scenario(label["scenario_id"])
        assert (
            str(scenario.category),
            str(scenario.expected_action),
            scenario.carc,
            list(scenario.rarcs),
            scenario.level,
        ) == (
            label["category"],
            label["expected_action"],
            label["carc"],
            label["rarcs"],
            label["level"],
        )
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
    profile = load_profile("dermatology")
    payers = {p.payer_id: p for p in profile.payers}
    provider_npis = {p.npi for p in profile.providers}
    referring_npis = {p.npi for p in profile.referring_providers}
    for row in rows:
        parsed = ClaimExportRow(**row)
        assert parsed.claim_frequency_code == "1" and parsed.original_claim_control_number is None
        assert parsed.place_of_service == "11"
        assert parsed.billing_provider_npi == str(profile.clinic["npi"])
        assert parsed.rendering_provider_npi in provider_npis
        assert parsed.referring_provider_npi in referring_npis or parsed.referring_provider_npi is None
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
    profile = load_profile("dermatology")
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
        if label and label["scenario_id"] == "timely_filing_expired":
            assert (submission - service).days >= window + 10, ccn
        else:
            assert 2 <= (submission - service).days <= 10, ccn
            if label and label["scenario_id"] == "timely_filing_in_window":
                assert (submission - service).days <= window
        if label and label["scenario_id"] == "auth_number_not_on_claim":
            assert row["prior_auth_number"], "CO-15 claims carry a prior authorization number"
        if label and label["scenario_id"] == "auth_missing":
            assert not row["prior_auth_number"]
        if label and label["scenario_id"] == "modifier_25_missing":
            em_rows = [r for r in claim_rows if r["procedure_code"].startswith("992")]
            assert em_rows and em_rows[0]["modifiers"] == "" and len(claim_rows) >= 2
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


def test_labels_match_remittance_adjustments() -> None:
    found = _claims_in_fixture_remits()
    for label in _fixture_labels():
        payer_id, claim = found[label["claim_control_number"]]
        assert payer_id == label["payer_id"]
        carc, rarcs, amount = label["carc"], label["rarcs"], Decimal(label["denied_amount"])
        if label["level"] == "claim":
            assert claim["clp"][2] == "4" and Decimal(claim["clp"][4]) == 0
            assert ("CO", carc, amount) in claim["cas"]
            assert claim["lq"] == rarcs
            assert label["line_number"] is None
            assert all(not svc["cas"] for svc in claim["svcs"])
        else:
            assert label["line_number"] is not None
            svc = claim["svcs"][label["line_number"] - 1]
            assert svc["svc"][1].split(":")[1] == label["procedure_code"]
            assert ("CO", carc, amount) in svc["cas"]
            assert svc["lq"] == rarcs
            assert Decimal(svc["svc"][3]) == 0
            assert not claim["cas"] and not claim["lq"]
            if label["scenario_id"] == "med_necessity_wart_destruction":
                assert any(c == "45" for _, c, _ in svc["cas"]), (
                    "allowed-amount rule leaves CO-45 on the line"
                )


def test_paid_claims_carry_contractual_adjustments_and_no_label() -> None:
    labeled = {label["claim_control_number"] for label in _fixture_labels()}
    found = _claims_in_fixture_remits()
    unlabeled = [ccn for ccn in found if ccn not in labeled]
    assert len(unlabeled) == FIXTURE_N_CLAIMS - len(labeled) and unlabeled
    saw_patient_responsibility = False
    for ccn in unlabeled:
        _, claim = found[ccn]
        assert claim["clp"][2] == "1" and Decimal(claim["clp"][4]) > 0
        assert not claim["cas"] and not claim["lq"]
        for svc in claim["svcs"]:
            carcs = {(g, c) for g, c, _ in svc["cas"]}
            assert ("CO", "45") in carcs
            assert all(c in INFORMATIONAL_CARCS for _, c, _ in svc["cas"])
            assert all(g in ("CO", "PR") for g, _, _ in svc["cas"])
            assert svc["amt"].get("B6"), "paid lines carry the allowed amount"
            if any(g == "PR" for g, _, _ in svc["cas"]):
                saw_patient_responsibility = True
                assert all(c in ("1", "2", "3") for g, c, _ in svc["cas"] if g == "PR")
    assert saw_patient_responsibility


# --------------------------------------------------------------------------------------
# notes and payer documents
# --------------------------------------------------------------------------------------


def test_notes_cover_every_appeal_label() -> None:
    notes = _read_csv(FIXTURE_DIR / "notes.csv")
    rows = _group_rows(_fixture_rows())
    labels = _fixture_labels()
    profile = load_profile("dermatology")
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
        if label["scenario_id"] in ("modifier_25_missing", "diagnosis_inconsistent", "missing_ndc"):
            assert by_ccn.get(ccn), f"{ccn}: corrected-claim label needs a note excerpt"
        if label["scenario_id"] in ("bundling_unsupported", "bundling_ncci_236"):
            assert ccn not in by_ccn, f"{ccn}: no_action bundling label must not carry notes"
        assert label["has_notes"] == (ccn in by_ccn)
    labeled = {label["claim_control_number"] for label in labels}
    benign = [ccn for ccn in by_ccn if ccn not in labeled]
    assert benign, "some paid claims carry benign notes"


def test_payer_documents_quote_codes_and_plant_injections() -> None:
    docs = _read_csv(FIXTURE_DIR / "payer_documents.csv")
    labels = {label["claim_control_number"]: label for label in _fixture_labels()}
    rows = _group_rows(_fixture_rows())
    profile = load_profile("dermatology")
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
        if label["scenario_id"] == "cob_wrong_payer":
            assert ccn in by_ccn
            other = [name for pid, name in payer_names.items() if pid != label["payer_id"]]
            assert any(name in by_ccn[ccn][0]["text"] for name in other), (
                "the letter names the responsible payer"
            )
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
            name="Northwind Health Plan",
            payer_id="NW001",
            timely_filing_days=90,
            corrected_claim_days=90,
            appeal_days=180,
        ),
        Payer(
            name="Blue Harbor PPO",
            payer_id="BH003",
            timely_filing_days=120,
            corrected_claim_days=120,
            appeal_days=180,
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
        member_id="NW123456789",
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
                "rarcs": ["M78"],
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
            rendering_provider_npi="1748293655",
            rendering_provider_name="Priya Thornquist, MD",
            place_of_service="11",
            diagnosis_codes=["D48.5", "B07.9"],
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
                    diagnosis_pointers=[1],
                    line_item_control_number=f"{claim.claim_control_number}-1",
                ),
                ClaimLine(
                    claim_id=claim.id,
                    line_number=2,
                    service_date=date(2026, 7, 14),
                    procedure_code="17110",
                    modifiers=[],
                    units=Decimal("1"),
                    charge_amount=Decimal("180.00"),
                    diagnosis_pointers=[2],
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
    assert set(files) == {"NW001", "BH003"}
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
    first = _parse_835(files2["NW001"])["claims"][0]
    assert first["clp"][2] == "4" and not first["cas"]
    assert [t[1] for s in first["svcs"] for t in s["cas"]] == ["4", "4"] and first["svcs"][0]["lq"] == ["M78"]
    fourth = _parse_835(files2["BH003"])["claims"][0]
    assert [s["lq"] for s in fourth["svcs"]] == [[], ["M15"]]
    single = generate_followup_835(session, items, plan)
    segments = split_segments(single)
    assert segments[0][0] == "ISA" and sum(1 for s in segments if s[0] == "ST") == 2
    assert next(s for s in segments if s[0] == "GE")[1] == "2"
    from app.ingest.parse_835 import parse_835
    from app.ingest.x12 import tokenize

    tokens = tokenize(files["NW001"])
    assert [t.id for t in tokens][:3] == ["ISA", "GS", "ST"]
    assert sum(1 for t in tokens if t.id == "CLP") == 2
    parsed_files = parse_835(files["NW001"])
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
        "member_id": "NW123456789",
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
    text = build_835(
        payer={"name": "Northwind Health Plan", "payer_id": "NW001", "tax_id": "911234567"},
        payee={"name": "Larkspur Dermatology Associates", "npi": "1831952738", "tax_id": "82-5551234"},
        claims=[claim],
        payment_date=date(2026, 12, 1),
        production_date=date(2026, 12, 1),
        trace_number="TRN1",
        interchange_control_number=42,
        sender_id="NW001",
        receiver_id="1831952738",
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
    raw["encounters"] = [
        {
            "id": "maybe_nothing",
            "weight": 1,
            "tags": tags,
            "em_codes": ["99213"],
            "em_probability": 0.0,
            "em_modifier_25": True,
            "procedures": [{"codes": ["11102"], "probability": 0.0}],
            "diagnosis_groups": [["D48.5"]],
        },
        {
            "id": "procedure_only",
            "weight": 1,
            "tags": [],
            "em_codes": [],
            "procedures": [{"codes": ["J3301"], "probability": 0.0}],
            "diagnosis_groups": [["L91.0"]],
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
    assert {"99213", "J3301"} <= codes_seen
    labels = json.loads((tmp_path / "labels.json").read_text(encoding="utf-8"))
    assert labels and all(Decimal(label["denied_amount"]) > 0 for label in labels)


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
