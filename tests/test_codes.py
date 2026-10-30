"""CARC/RARC catalog (docs/SPEC.md section 4.3): coverage and the describe helpers."""

from __future__ import annotations

from decimal import Decimal

import pytest
import yaml

from app.classify.codes import (
    CODES_PATH,
    all_codes,
    carc_text,
    describe,
    enrich_reasons,
    normalize_code,
    rarc_text,
)
from app.classify.engine import load_rules
from app.schemas import DenialReason

# Codes the module brief requires the catalog to cover (rules, playbooks and the generator cite them).
REQUIRED_CARCS = [
    "1", "2", "3", "4", "5", "6", "11", "15", "16", "18", "19", "20", "21", "22", "23", "24", "26", "27",
    "29", "31", "32", "33", "39", "45", "49", "50", "56", "59", "62", "66", "94", "96", "97", "104",
    "109", "118", "119", "125", "137", "140", "144", "150", "151", "167", "170", "177", "182", "185",
    "187", "197", "198", "200", "204", "206", "226", "227", "231", "234", "236", "237", "242", "243",
    "252", "253", "273", "288", "B7", "B15", "B20", "B22",
]  # fmt: skip
# The chiropractic profile's codes (docs/SPECIALTY.md section 2) are among them: 24, 19, 170,
# 119, 226 and the remarks N425, N95, N626, N640, N657, N706, N822, MA122, N264, N265, N587, M62.
REQUIRED_RARCS = [
    "M15", "M20", "M25", "M51", "M62", "M76", "M78", "M79", "M86", "M119", "M122", "M127", "M144",
    "MA04", "MA121", "MA122", "MA130", "N4", "N10", "N19", "N29", "N30", "N54", "N56", "N59", "N95",
    "N111", "N115", "N130", "N179", "N211", "N225", "N264", "N265", "N285", "N286", "N290", "N351",
    "N362", "N382", "N390", "N393", "N418", "N425", "N435", "N519", "N522", "N525", "N587", "N598",
    "N626", "N640", "N657", "N661", "N704", "N706", "N822", "N823",
]  # fmt: skip


def test_every_code_referenced_by_a_rule_has_catalog_text() -> None:
    catalog = all_codes()
    missing: list[str] = []
    for rule in load_rules():
        for code in sorted(rule.codes["carc"]):
            if code not in catalog["carc"]:
                missing.append(f"{rule.id}: CARC {code}")
        for code in sorted(rule.codes["rarc"]):
            if code not in catalog["rarc"]:
                missing.append(f"{rule.id}: RARC {code}")
    assert not missing, missing


@pytest.mark.parametrize("code", REQUIRED_CARCS)
def test_required_carc_has_text(code: str) -> None:
    text = carc_text(code)
    assert isinstance(text, str) and text.strip()


@pytest.mark.parametrize("code", REQUIRED_RARCS)
def test_required_rarc_has_text(code: str) -> None:
    text = rarc_text(code)
    assert isinstance(text, str) and text.strip()


def test_lookup_normalizes_codes() -> None:
    assert carc_text(" 16 ") == carc_text("16")
    assert carc_text("016") == carc_text("16")
    assert carc_text("b7") == carc_text("B7")
    assert rarc_text("n4") == rarc_text("N4")
    assert rarc_text(" ma130 ") == rarc_text("MA130")


def test_normalize_code() -> None:
    assert normalize_code(" n4 ") == "N4"
    assert normalize_code("016") == "16"
    assert normalize_code(16) == "16"
    assert normalize_code("000") == "0"
    assert normalize_code("b15") == "B15"
    assert normalize_code(None) == ""
    assert normalize_code("") == ""


def test_normalize_code_leaves_non_ascii_digits_alone() -> None:
    # A hostile 835 could carry exotic digits; they must neither raise nor alias a real code.
    assert normalize_code("\u00b2") == "\u00b2"  # superscript two: int() would raise
    assert normalize_code("\u0661\u0666") == "\u0661\u0666"  # Arabic-Indic 16: int() would say "16"
    assert carc_text("\u0661\u0666") is None


def test_unknown_codes_return_none() -> None:
    assert carc_text("9999") is None
    assert rarc_text("ZZ999") is None
    assert carc_text("") is None
    assert rarc_text("16") is None  # a CARC is not a RARC


def test_all_codes_shape() -> None:
    codes = all_codes()
    assert set(codes) == {"carc", "rarc"}
    assert isinstance(codes["carc"], frozenset) and isinstance(codes["rarc"], frozenset)
    assert set(REQUIRED_CARCS) <= codes["carc"]
    assert set(REQUIRED_RARCS) <= codes["rarc"]


def test_catalog_file_texts_are_short_single_lines() -> None:
    with CODES_PATH.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    assert set(raw) == {"carc", "rarc"}
    for kind in ("carc", "rarc"):
        for code, text in raw[kind].items():
            assert isinstance(code, str), f"{kind} key {code!r} must be a quoted string"
            assert isinstance(text, str) and text.strip(), f"{kind} {code} has no text"
            assert "\n" not in text, f"{kind} {code} text spans lines"
            assert len(text) <= 200, f"{kind} {code} text is too long for a citation"


def test_describe_fills_texts_without_mutating_the_input() -> None:
    original = DenialReason(
        level="line",
        group_code="CO",
        carc="16",
        rarcs=["M119", "ZZ999"],
        amount=Decimal("50.00"),
        line_number=1,
        procedure_code="J3301",
    )
    described = describe(original)
    assert described is not original
    assert original.carc_text is None and original.rarc_texts == {}
    assert described.carc_text == carc_text("16")
    assert described.rarc_texts == {"M119": rarc_text("M119")}  # unknown RARCs get no text
    assert described.rarcs == ["M119", "ZZ999"]
    assert described.amount == original.amount
    assert described.line_number == 1 and described.procedure_code == "J3301"


def test_describe_keeps_existing_texts() -> None:
    original = DenialReason(
        level="claim",
        group_code="CO",
        carc="16",
        carc_text="custom carc text",
        rarcs=["M119", "N29"],
        rarc_texts={"M119": "custom rarc text"},
    )
    described = describe(original)
    assert described.carc_text == "custom carc text"
    assert described.rarc_texts == {"M119": "custom rarc text", "N29": rarc_text("N29")}


def test_describe_unknown_carc_stays_without_text() -> None:
    described = describe(DenialReason(level="claim", group_code="CO", carc="9999"))
    assert described.carc_text is None
    assert described.rarc_texts == {}


def test_enrich_reasons_returns_a_new_list_in_order() -> None:
    reasons = [
        DenialReason(
            level="line", group_code="CO", carc="197", rarcs=["N54"], amount=Decimal("200"), line_number=1
        ),
        DenialReason(
            level="line", group_code="CO", carc="4", rarcs=["M78"], amount=Decimal("30"), line_number=2
        ),
    ]
    enriched = enrich_reasons(reasons)
    assert enriched is not reasons
    assert [r.carc for r in enriched] == ["197", "4"]
    assert enriched[0].carc_text == carc_text("197")
    assert enriched[0].rarc_texts == {"N54": rarc_text("N54")}
    assert enriched[1].rarc_texts == {"M78": rarc_text("M78")}
    assert all(r.carc_text is None for r in reasons)  # inputs untouched
    assert enrich_reasons([]) == []
