"""Parser golden tests (docs/SPEC.md section 13: exact match on every field).

Hand check of tests/fixtures/835/sample_basic.expected.json against sample_basic.835, done on
2026-10-28 line by line (the file's line numbers are given in parentheses):

- envelope: ISA06/ISA08 with their padding stripped, ISA09 ``261015`` -> 2026-10-15, ISA13 kept as the
  9-digit string, GS06 ``101``, ST02 ``0001`` (lines 1-3)
- BPR01 I, BPR02 460.00, BPR03 C, BPR04 ACH, BPR16 = 16th element 20261016 (line 4); TRN02/TRN03 (5);
  DTM*405 -> production_date (7)
- REF*EV feeds no field and is the only entry in unparsed_segments (6)
- payer: N1*PR name, N104 and REF*2U both NHP001 -> payer_id, N3, N4, PER02 contact (8-12);
  payee: N1*PE name, N103 XX -> npi from N104, N3, N4, REF*TJ (13-16); LX consumed (17)
- CLP 1 (18-34): all nine CLP elements; NM1*QC last/first + MI member id; NM1*82 XX -> rendering NPI;
  REF*1L -> references; DTM 050/232/233; AMT*AU; SVC 1 ``HC:99213`` split into qualifier and
  procedure with no modifiers, 150.00/110.00, empty SVC04, units 1, DTM*472, CAS CO-45 40.00,
  REF*6R, AMT*B6; SVC 2 with no adjustment; no claim-level CAS or LQ
- CLP 2 (35-45): CLP02 ``4``; claim-level CAS CO-197 210.00; NM1*IL insured names; LQ*HE*N54 before
  the SVC -> claim remark code; one SVC paid 0.00 without AMT*B6 -> allowed_amount null; no NM1*82
- CLP 3 (46-60): SVC 1 CO-45 40.00 + B6 110.00; SVC 2 CAS CO-4 180.00, REF*6R L-24003-2, LQ*HE*M78
  attached to the line, allowed_amount null
- CLP 4 (61-79): patient_responsibility 50.00; SVC 1 ``HC:99213:25`` -> modifier 25, PR-1 50.00,
  B6 150.00; SVC 2 no adjustment; SVC 3 CO-16 60.00 with remark codes MA130 then N29
- PLB (80): provider id, fiscal period 20261231, ``WO:NHP-ICN-0001`` split into reason and reference,
  amount 25.00
- segment_count 79: ST on line 3 through SE on line 81 inclusive, equal to SE01

Hand check of sample_member_qualifiers.expected.json against sample_member_qualifiers.835 (2026-10-28):
five CLPs from a Medicaid payer (SMP001) exercising the NM1 identification qualifiers:
- MQ-0001: NM1*QC with ``MR`` -> member_id MCD12345678; MQ-0002: ``HN`` -> 123456789A
- MQ-0003: NM1*QC carries no id, NM1*IL (subscriber TAYLOR) ``MI`` -> member_id SUB778899; the
  patient names stay RIVERMOOR/CASEY and the insured names RIVERMOOR/TAYLOR; CLP02 4, CO-197
- MQ-0004: ``ZZ`` on NM1*QC is not a member-id qualifier (NOT-A-MEMBER-ID is dropped); NM1*IL ``II``
  -> IIX445566; PR-1 60.00
- MQ-0005: ``34`` on the patient -> 987654321, which wins over the subscriber's MI SUB990011
- segment_count 57 == SE01, nothing unparsed
"""

from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.ingest.dates import dtm_dates, parse_ccyymmdd, parse_date_range, parse_yymmdd
from app.ingest.parse_835 import MEMBER_ID_QUALIFIERS, parse_835
from app.ingest.x12 import Delimiters, Segment, X12Error, read_delimiters, split_composite, tokenize

FIXTURES = Path(__file__).parent / "fixtures" / "835"
BASIC = FIXTURES / "sample_basic.835"
EXPECTED = FIXTURES / "sample_basic.expected.json"
MULTI = FIXTURES / "sample_multi.835"
CRLF_UNKNOWN = FIXTURES / "sample_crlf_unknown.835"
MEMBER_QUALIFIERS = FIXTURES / "sample_member_qualifiers.835"
MEMBER_QUALIFIERS_EXPECTED = FIXTURES / "sample_member_qualifiers.expected.json"


def _read(path: Path) -> str:
    return path.read_bytes().decode("ascii")


def _interchange(
    segments: list[str],
    *,
    element: str = "*",
    sub: str = ":",
    terminator: str = "~",
    newline: str = "\n",
) -> str:
    """Build a small interchange. Body segments are written with ``*``/``:`` and translated."""
    isa = (
        f"ISA{element}00{element}{'':10}{element}00{element}{'':10}{element}ZZ{element}{'SENDER':<15}"
        f"{element}ZZ{element}{'RECEIVER':<15}{element}261015{element}1200{element}^{element}00501"
        f"{element}000000001{element}0{element}T{element}{sub}{terminator}"
    )
    assert len(isa) == 106
    gs = f"GS{element}HP{element}SENDER{element}RECEIVER{element}20261015{element}1200{element}1{element}X{element}005010X221A1{terminator}"
    body = [seg.replace("*", element).replace(":", sub) + terminator for seg in segments]
    tail = [f"GE{element}1{element}1{terminator}", f"IEA{element}1{element}000000001{terminator}"]
    joiner = "" if terminator == "\n" else newline
    return joiner.join([isa, gs, *body, *tail]) + joiner


MINIMAL = [
    "ST*835*0001",
    "BPR*I*80.00*C*CHK************20261016",  # BPR16 is the 16th element
    "TRN*1*TRACE1*PAYER1",
    "N1*PR*PAYER ONE",
    "N1*PE*PAYEE ONE*XX*1234567893",
    "CLP*X1*1*100.00*80.00*0.00",
    "SVC*HC:99213:25:59*100.00*80.00**1*HC:99214",
    "CAS*CO*45*20.00",
    "SE*9*0001",
]


# --------------------------------------------------------------------------------------
# Golden file
# --------------------------------------------------------------------------------------


def test_sample_basic_matches_expected_exactly():
    parsed = parse_835(_read(BASIC))
    assert len(parsed) == 1
    expected = json.loads(EXPECTED.read_text(encoding="utf-8"))
    assert parsed[0].model_dump(mode="json") == expected


def test_hand_checked_values():
    """Independent assertions for the values listed in the module docstring."""
    tx = parse_835(_read(BASIC))[0]
    assert tx.sender_id == "NHP001" and tx.receiver_id == "1234567893"
    assert tx.interchange_date == date(2026, 10, 15)
    assert tx.interchange_control_number == "000000101"
    assert tx.group_control_number == "101" and tx.transaction_control_number == "0001"

    assert tx.payment.amount == Decimal("460.00")
    assert tx.payment.method == "ACH" and tx.payment.credit_debit == "C"
    assert tx.payment.payment_date == date(2026, 10, 16)
    assert tx.payment.trace_number == "NHP2026101500123"
    assert tx.payment.payer_identifier == "1123456789"
    assert tx.payment.production_date == date(2026, 10, 15)

    assert tx.payer.name == "NORTHWIND HEALTH PLAN" and tx.payer.payer_id == "NHP001"
    assert tx.payer.contact == "PROVIDER SERVICES" and tx.payer.zip == "62701"
    assert tx.payee.npi == "1234567893" and tx.payee.tax_id == "123456789"
    assert tx.payee.address_line1 == "100 CLINIC WAY"
    assert tx.unparsed_segments == ["REF*EV*1234567893"]

    a, b, c, d = tx.claims
    assert [x.patient_control_number for x in tx.claims] == [
        "CLM-24001",
        "CLM-24002",
        "CLM-24003",
        "CLM-24004",
    ]
    assert [x.sequence for x in tx.claims] == [1, 2, 3, 4]

    assert a.claim_status_code == "1" and a.paid_amount == Decimal("230.00")
    assert a.member_id == "NHP884421001" and a.rendering_provider_npi == "1467812345"
    assert a.references == {"1L": "GRP-5521"} and a.coverage_amount == Decimal("230.00")
    assert a.claim_received_date == date(2026, 10, 1) and a.statement_to == date(2026, 8, 4)
    assert a.adjustments == [] and a.remark_codes == []
    assert len(a.services) == 2
    assert a.services[0].procedure_code == "99213" and a.services[0].qualifier == "HC"
    assert a.services[0].adjustments[0].carc == "45" and a.services[0].adjustments[0].amount == Decimal(
        "40.00"
    )
    assert a.services[0].allowed_amount == Decimal("110.00")
    assert a.services[1].line_item_control_number == "L-24001-2" and a.services[1].adjustments == []

    assert b.claim_status_code == "4" and b.paid_amount == Decimal("0.00")
    assert [(x.group_code, x.carc, x.amount) for x in b.adjustments] == [("CO", "197", Decimal("210.00"))]
    assert b.remark_codes == ["N54"]
    assert b.insured_last_name == "PINECREST" and b.insured_first_name == "JORDAN"
    assert b.rendering_provider_npi is None
    assert b.services[0].allowed_amount is None and b.services[0].remark_codes == []

    assert c.services[1].adjustments[0].carc == "4" and c.services[1].remark_codes == ["M78"]
    assert c.services[1].line_item_control_number == "L-24003-2"

    assert d.patient_responsibility == Decimal("50.00")
    assert d.services[0].modifiers == ["25"] and d.services[0].adjustments[0].group_code == "PR"
    assert d.services[2].procedure_code == "J3301" and d.services[2].remark_codes == ["MA130", "N29"]
    assert d.services[2].adjustments[0].carc == "16"

    plb = tx.provider_adjustments
    assert len(plb) == 1
    assert plb[0].provider_id == "1234567893" and plb[0].fiscal_period_date == date(2026, 12, 31)
    assert (
        plb[0].reason_code == "WO"
        and plb[0].reference == "NHP-ICN-0001"
        and plb[0].amount == Decimal("25.00")
    )


def test_segment_count_matches_se01():
    text = _read(BASIC)
    se01 = int(re.search(r"SE\*(\d+)\*0001~", text).group(1))
    tx = parse_835(text)[0]
    assert tx.segment_count == se01 == 79
    # count independently: every segment between ST and SE inclusive
    segments = [s for s in text.replace("\n", "").split("~") if s]
    start = next(i for i, s in enumerate(segments) if s.startswith("ST*"))
    end = next(i for i, s in enumerate(segments) if s.startswith("SE*"))
    assert end - start + 1 == 79


# --------------------------------------------------------------------------------------
# Member id qualifiers (golden file two)
# --------------------------------------------------------------------------------------


def test_member_qualifiers_file_matches_expected_exactly():
    parsed = parse_835(_read(MEMBER_QUALIFIERS))
    assert len(parsed) == 1
    expected = json.loads(MEMBER_QUALIFIERS_EXPECTED.read_text(encoding="utf-8"))
    assert parsed[0].model_dump(mode="json") == expected


def test_member_id_accepted_under_every_member_qualifier_with_subscriber_fallback():
    """Medicaid (MR), Medicare HICN (HN), II and 34 identify the member like MI; a dependent's claim
    without an id on NM1*QC takes the subscriber's from NM1*IL."""
    assert MEMBER_ID_QUALIFIERS == {"MI", "MR", "HN", "II", "34"}
    tx = parse_835(_read(MEMBER_QUALIFIERS))[0]
    assert tx.segment_count == 57 and tx.unparsed_segments == []
    assert tx.payer.payer_id == "SMP001" and tx.payment.payment_date == date(2026, 10, 21)
    by_pcn = {claim.patient_control_number: claim for claim in tx.claims}
    assert list(by_pcn) == ["MQ-0001", "MQ-0002", "MQ-0003", "MQ-0004", "MQ-0005"]

    assert by_pcn["MQ-0001"].member_id == "MCD12345678"  # MR
    assert by_pcn["MQ-0001"].insured_last_name is None
    assert by_pcn["MQ-0002"].member_id == "123456789A"  # HN

    dependent = by_pcn["MQ-0003"]  # no id on the patient: the subscriber's MI id stands in
    assert dependent.member_id == "SUB778899"
    assert (dependent.patient_last_name, dependent.patient_first_name) == ("RIVERMOOR", "CASEY")
    assert (dependent.insured_last_name, dependent.insured_first_name) == ("RIVERMOOR", "TAYLOR")
    assert dependent.claim_status_code == "4" and dependent.adjustments[0].carc == "197"

    unknown = by_pcn["MQ-0004"]  # an unknown qualifier on NM1*QC is not an id; NM1*IL's II is
    assert unknown.member_id == "IIX445566"
    assert unknown.patient_responsibility == Decimal("60.00")

    assert by_pcn["MQ-0005"].member_id == "987654321"  # 34 on the patient wins over the subscriber
    # the first golden file is unchanged: MI as before, no NM1*IL id involved
    assert parse_835(_read(BASIC))[0].claims[0].member_id == "NHP884421001"


def test_patient_identifier_wins_over_the_subscriber_whatever_the_segment_order():
    body = [
        "ST*835*0001",
        "CLP*X1*1*100*80*0",
        "NM1*IL*1*LAST*FIRST****MI*SUB1",
        "NM1*QC*1*LAST*KID****MR*KID1",
        "SE*4*0001",
    ]
    assert parse_835(_interchange(body))[0].claims[0].member_id == "KID1"
    body[3] = "NM1*QC*1*LAST*KID"
    assert parse_835(_interchange(body))[0].claims[0].member_id == "SUB1"
    body[2] = "NM1*IL*1*LAST*FIRST****ZZ*NOT-AN-ID"
    assert parse_835(_interchange(body))[0].claims[0].member_id is None


# --------------------------------------------------------------------------------------
# Multiple transactions, line endings, unknown segments
# --------------------------------------------------------------------------------------


def test_multi_transaction_file_yields_two_results():
    results = parse_835(_read(MULTI))
    assert len(results) == 2
    assert [r.transaction_control_number for r in results] == ["0001", "0002"]
    assert [r.payment.trace_number for r in results] == ["NHP2026102200456", "NHP2026102200457"]
    assert [r.payment.method for r in results] == ["ACH", "CHK"]
    assert [r.payment.payment_date for r in results] == [date(2026, 10, 23), date(2026, 10, 23)]
    assert [r.payment.amount for r in results] == [Decimal("230.00"), Decimal("110.00")]
    assert [[c.patient_control_number for c in r.claims] for r in results] == [["CLM-24001"], ["CLM-24003"]]
    assert all(r.interchange_control_number == "000000102" for r in results)
    assert results[0].segment_count == 31 and results[1].segment_count == 29


def test_crlf_and_unknown_segment_tolerated():
    # .gitattributes normalises *.835 to LF on commit, so the CRLF form is forced here regardless of
    # how the file sits on disk.
    text = _read(CRLF_UNKNOWN).replace("\r\n", "\n").replace("\n", "\r\n")
    assert "~\r\n" in text
    results = parse_835(text)
    assert len(results) == 1
    tx = results[0]
    assert "ZZZ*1" in tx.unparsed_segments
    assert tx.unparsed_segments == ["REF*EV*1234567893", "ZZZ*1"]
    assert tx.segment_count == 80
    basic = parse_835(_read(BASIC))[0]
    assert tx.claims == basic.claims
    assert tx.provider_adjustments == basic.provider_adjustments
    assert tx.payer == basic.payer and tx.payee == basic.payee


def test_lf_and_crlf_parse_identically():
    lf = _read(BASIC)
    crlf = lf.replace("\n", "\r\n")
    assert parse_835(lf) == parse_835(crlf)
    assert parse_835("  \n" + lf + "\n\n") == parse_835(lf)


# --------------------------------------------------------------------------------------
# Tokenizer
# --------------------------------------------------------------------------------------


def test_tokenizer_reads_delimiters_from_isa():
    text = _read(BASIC)
    delims = read_delimiters(text)
    assert delims == Delimiters(element="*", sub_element=":", segment="~", repetition="^")
    segments = tokenize(text)
    assert segments[0].id == "ISA" and len(segments[0].elements) == 17
    assert segments[0].element(6) == "NHP001"  # padding stripped by element()
    assert segments[0].elements[6] == "NHP001         "  # raw elements keep the padding
    assert segments[-1].id == "IEA"
    clp = next(s for s in segments if s.id == "CLP")
    assert clp.raw == "CLP*CLM-24001*1*270.00*230.00*0.00*12*NHP-ICN-0001*11*1"
    assert clp.element(1) == "CLM-24001" and clp.element(99) is None
    svc = next(s for s in segments if s.id == "SVC")
    assert split_composite(svc.element(1), delims.sub_element) == ["HC", "99213"]


def test_custom_delimiters():
    text = _interchange(MINIMAL, element="|", sub="^", terminator="!")
    delims = read_delimiters(text)
    assert (delims.element, delims.sub_element, delims.segment) == ("|", "^", "!")
    tx = parse_835(text)[0]
    svc = tx.claims[0].services[0]
    assert svc.procedure_code == "99213" and svc.modifiers == ["25", "59"]
    assert svc.original_procedure_code == "99214"
    assert tx.claims[0].services[0].adjustments[0].amount == Decimal("20.00")
    assert tx.payment.payment_date == date(2026, 10, 16)


def test_newline_segment_terminator():
    text = _interchange(MINIMAL, terminator="\n")
    assert read_delimiters(text).segment == "\n"
    tx = parse_835(text)[0]
    assert tx.claims[0].patient_control_number == "X1"
    assert tx.segment_count == 9


def test_rejects_text_without_isa():
    with pytest.raises(X12Error):
        read_delimiters("GS*HP*1*2*20261015*1200*1*X*005010X221A1~")
    with pytest.raises(X12Error):
        parse_835("not an x12 file")
    with pytest.raises(X12Error):
        tokenize("ISA*00*short~")


def test_no_transaction_set_raises():
    text = _interchange([])
    with pytest.raises(X12Error):
        parse_835(text)


def test_split_composite():
    assert split_composite("HC:99213:25", ":") == ["HC", "99213", "25"]
    assert split_composite("HC", ":") == ["HC"]
    assert split_composite("", ":") == []
    assert split_composite(None, ":") == []
    assert split_composite(" WO : 123 ", ":") == ["WO", "123"]


def test_segment_helpers():
    seg = Segment(id="REF", elements=["REF", "6R", " ABC ", ""], raw="REF*6R* ABC *")
    assert seg.element(0) == "REF" and seg.element(2) == "ABC" and seg.element(3) is None
    assert seg.element(-1) is None and len(seg) == 4


# --------------------------------------------------------------------------------------
# Robustness
# --------------------------------------------------------------------------------------


def test_missing_optional_elements_do_not_crash():
    sparse = [
        "ST*835*0001",
        "BPR",
        "TRN*1",
        "DTM*405",
        "N1*PR",
        "N3",
        "N4",
        "PER",
        "REF*2U",
        "N1*PE*PAYEE",
        "REF*TJ",
        "LX",
        "CLP*X1*1",
        "CAS*CO",
        "CAS*CO*197",
        "NM1*QC*1",
        "NM1*82*1*LAST*FIRST",
        "REF*1L",
        "DTM*050",
        "AMT*AU",
        "LQ*HE",
        "SVC*HC",
        "SVC",
        "DTM*472",
        "DTM*150*20260804",
        "DTM*151*20260805",
        "CAS",
        "REF*6R",
        "AMT*B6",
        "LQ*HE*M78",
        "PLB*1",
        "PLB*1*20261231*WO",
        "SE*33*0001",
    ]
    tx = parse_835(_interchange(sparse))[0]
    claim = tx.claims[0]
    assert claim.patient_control_number == "X1" and claim.charge_amount == Decimal("0")
    assert claim.member_id is None and claim.patient_last_name is None
    assert claim.rendering_provider_npi is None
    assert [a.carc for a in claim.adjustments] == ["197"]
    assert claim.adjustments[0].amount == Decimal("0")
    assert claim.claim_received_date is None and claim.coverage_amount == Decimal("0")
    assert claim.remark_codes == [] and claim.references == {}
    assert len(claim.services) == 2
    first, second = claim.services
    assert first.procedure_code is None and first.qualifier == "HC" and first.units == Decimal("1")
    assert second.service_date == date(2026, 8, 4)  # DTM*150 used when 472 carries no date
    assert second.remark_codes == ["M78"] and second.adjustments == []
    assert second.line_item_control_number is None
    assert tx.provider_adjustments[0].reason_code == "WO" and tx.provider_adjustments[0].reference is None
    assert tx.payment.production_date is None


def test_malformed_amount_raises_x12error_without_the_value():
    bad = ["ST*835*0001", "CLP*X1*1*ABCDEF*0", "SE*3*0001"]
    with pytest.raises(X12Error) as info:
        parse_835(_interchange(bad))
    assert "ABCDEF" not in str(info.value)
    assert "CLP" in str(info.value)


@pytest.mark.parametrize(
    ("segment", "value"),
    [("CLP*X1*1*{v}*0", "NaN"), ("SVC*HC:99213*{v}*0**1", "Infinity"), ("CAS*CO*45*{v}", "-Infinity")],
)
def test_non_finite_amount_raises_x12error_without_the_value(segment, value):
    """Decimal accepts NaN/Infinity, but they can be neither compared nor stored: reject at the parser."""
    segment_id = segment.split("*")[0]
    body = ["ST*835*0001", "CLP*X1*1*100*0", "SVC*HC:99213*100*0**1", "CAS*CO*45*20", "SE*5*0001"]
    body = [segment.format(v=value) if s.split("*")[0] == segment_id else s for s in body]
    with pytest.raises(X12Error) as info:
        parse_835(_interchange(body))
    assert value not in str(info.value) and segment_id in str(info.value)


def test_moa_and_mia_carry_claim_level_remark_codes():
    """5010 puts claim-level RARCs in MOA03-07 (professional) / MIA05,20-23 (institutional)."""
    body = [
        "ST*835*0001",
        "CLP*X1*4*210*0*0",
        "CAS*CO*197*210",
        "NM1*QC*1*LAST*FIRST****MI*M1",
        "LQ*HE*N54",
        "MOA***M62*N54",  # MOA01/02 are amounts; MOA03-07 are remark codes
        "MIA*0****N382" + "*" * 14 + "*M76*N115",  # MIA05 and MIA20-23
        "SVC*HC:99214*210*0**1",
        "MOA***N29",  # invalid after an SVC: kept verbatim, attributed to nothing
        "SE*10*0001",
    ]
    tx = parse_835(_interchange(body))[0]
    claim = tx.claims[0]
    assert claim.remark_codes == ["N54", "M62", "N54", "N382", "M76", "N115"]
    assert claim.services[0].remark_codes == []
    assert tx.unparsed_segments == ["MOA***N29"]
    assert tx.segment_count == 10


def test_cas_with_six_triplets_and_quantities():
    body = [
        "ST*835*0001",
        "CLP*X1*1*100*10*0",
        "CAS*CO*45*10*1*16*20**4*30*2*97*5**253*1**59*4*1",
        "SE*4*0001",
    ]
    adjustments = parse_835(_interchange(body))[0].claims[0].adjustments
    assert [(a.carc, a.amount, a.quantity) for a in adjustments] == [
        ("45", Decimal("10"), Decimal("1")),
        ("16", Decimal("20"), None),
        ("4", Decimal("30"), Decimal("2")),
        ("97", Decimal("5"), None),
        ("253", Decimal("1"), None),
        ("59", Decimal("4"), Decimal("1")),
    ]
    assert all(a.group_code == "CO" for a in adjustments)


def test_segments_outside_transactions_and_missing_se():
    # an unterminated transaction set is closed at GE; stray segments before ST are ignored
    loose = ["ZZZ*before", "ST*835*0001", "CLP*X1*1*10*10*0"]
    tx = parse_835(_interchange(loose))[0]
    assert tx.claims[0].paid_amount == Decimal("10")
    assert tx.segment_count == 2


def test_date_helpers():
    assert parse_ccyymmdd("20260804") == date(2026, 8, 4)
    assert parse_ccyymmdd("20261301") is None
    assert parse_ccyymmdd("2026-08-04") is None
    assert parse_ccyymmdd("") is None and parse_ccyymmdd(None) is None
    assert parse_yymmdd("261015") == date(2026, 10, 15)
    assert parse_yymmdd("991231") == date(1999, 12, 31)
    assert parse_yymmdd("26101") is None
    assert parse_date_range("20260804-20260806") == (date(2026, 8, 4), date(2026, 8, 6))
    assert parse_date_range("20260804") == (date(2026, 8, 4), date(2026, 8, 4))
    rd8 = Segment(id="DTM", elements=["DTM", "232", "", "", "", "RD8", "20260804-20260806"], raw="")
    assert dtm_dates(rd8) == (date(2026, 8, 4), date(2026, 8, 6))
    empty = Segment(id="DTM", elements=["DTM", "472"], raw="DTM*472")
    assert dtm_dates(empty) == (None, None)
