"""835 import tests: claims CSV first, then the golden 835 (docs/SPEC.md section 6).

The four CLPs in sample_basic.835 line up with claims in sample_claims.csv:
CLM-24001 paid (CO-45 only), CLM-24002 denied at claim level (CLP02=4, CO-197, N54), CLM-24003
line-level denial (CO-4 + M78 on line 2), CLM-24004 line-level denial (CO-16 + MA130/N29 on
line 3, PR-1 deductible on line 1). CLM-24005 and CLM-24006 are not on the remittance.
"""

from __future__ import annotations

import re
import sys
import types
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, select

from app.ingest.claims_import import import_claims_csv
from app.ingest.denials import AdjustmentInput, denial_reasons_for, detect, detect_for
from app.ingest.remit_import import (
    PAID_CLAIM_STATUS_CODES,
    claim_status_for,
    import_835,
    rematch_unmatched,
    transaction_sha,
)
from app.models import Adjustment, AuditLog, Claim, Payer, Remittance, RemittanceClaim, RemittanceLine
from app.schemas import ParsedAdjustment

FIXTURES = Path(__file__).parent / "fixtures"
CLAIMS_CSV = FIXTURES / "claims" / "sample_claims.csv"
BASIC = FIXTURES / "835" / "sample_basic.835"
MULTI = FIXTURES / "835" / "sample_multi.835"

BASIC_COUNTS = {
    "transactions": 1,
    "claims": 4,
    "matched": 4,
    "unmatched": 0,
    "denials": 3,
    "lines": 8,
    "adjustments": 6,
    "outcomes": 0,
}


def _basic_text() -> str:
    return BASIC.read_bytes().decode("ascii")


def _import_both(session, clinic):
    import_claims_csv(session, CLAIMS_CSV, clinic_id=clinic.id)
    return import_835(session, BASIC, created_by="tester")


def _rcs(session) -> dict[str, RemittanceClaim]:
    rows = session.execute(select(RemittanceClaim).order_by(RemittanceClaim.id)).scalars().all()
    return {rc.patient_control_number: rc for rc in rows}


def _claim(session, ccn: str) -> Claim:
    return session.execute(select(Claim).where(Claim.claim_control_number == ccn)).scalar_one()


def _count(session, model) -> int:
    return session.execute(select(func.count(model.id))).scalar_one()


def _adjustment_tuples(rc: RemittanceClaim) -> list[tuple]:
    rows = sorted(rc.adjustments, key=lambda a: a.id)
    return [
        (
            a.level,
            a.remittance_line.line_number if a.remittance_line else None,
            a.group_code,
            a.carc,
            Decimal(a.amount),
            a.is_denial,
        )
        for a in rows
    ]


# --------------------------------------------------------------------------------------
# Batch, counts, duplicate handling
# --------------------------------------------------------------------------------------


def test_basic_import_counts_and_rows(session, clinic):
    batch = _import_both(session, clinic)
    assert batch.status == "done" and batch.kind == "835" and batch.error is None
    assert batch.file_name == "sample_basic.835"
    assert batch.counts == BASIC_COUNTS

    remittance = session.execute(select(Remittance)).scalar_one()
    northwind = session.execute(select(Payer).where(Payer.payer_id == "NHP001")).scalar_one()
    assert remittance.payer_id == northwind.id
    assert remittance.file_sha256 == batch.file_sha256 and remittance.transaction_index == 0
    assert remittance.trace_number == "NHP2026101500123" and remittance.payer_identifier == "1123456789"
    assert remittance.payment_method == "ACH" and remittance.payment_amount == Decimal("460.00")
    assert remittance.payment_date.isoformat() == "2026-10-16"
    assert remittance.production_date.isoformat() == "2026-10-15"
    assert remittance.payer_name == "NORTHWIND HEALTH PLAN" and remittance.payee_npi == "1234567893"
    assert remittance.import_batch_id == batch.id
    assert remittance.raw_meta["segment_count"] == 79 and remittance.raw_meta["claim_count"] == 4
    assert "unparsed_segment_count" in remittance.raw_meta and "REF*EV" not in str(remittance.raw_meta)

    rcs = _rcs(session)
    assert list(rcs) == ["CLM-24001", "CLM-24002", "CLM-24003", "CLM-24004"]
    assert [rc.sequence for rc in rcs.values()] == [1, 2, 3, 4]
    assert all(rc.match_method == "ccn" for rc in rcs.values())
    assert all(rc.claim_id == _claim(session, pcn).id for pcn, rc in rcs.items())
    assert _count(session, RemittanceLine) == 8 and _count(session, Adjustment) == 6
    a = rcs["CLM-24001"]
    assert (
        a.claim_status_code == "1"
        and a.charge_amount == Decimal("270.00")
        and a.paid_amount == Decimal("230.00")
    )
    assert a.patient_last_name == "BRIGHTWATER" and a.member_id == "NHP884421001"
    assert a.payer_claim_control_number == "NHP-ICN-0001" and a.rendering_provider_npi == "1467812345"
    assert a.statement_from.isoformat() == "2026-08-04" and a.claim_received_date.isoformat() == "2026-10-01"
    assert rcs["CLM-24004"].patient_responsibility == Decimal("50.00")
    assert rcs["CLM-24002"].remark_codes == ["N54"]


def test_duplicate_reimport_is_noop(session, clinic):
    first = _import_both(session, clinic)
    statuses = {
        ccn: _claim(session, ccn).status for ccn in ("CLM-24001", "CLM-24002", "CLM-24003", "CLM-24004")
    }
    second = import_835(session, BASIC)
    assert second.id != first.id and second.status == "duplicate" and second.counts == {}
    assert second.file_sha256 == first.file_sha256
    assert _count(session, Remittance) == 1 and _count(session, RemittanceClaim) == 4
    assert _count(session, RemittanceLine) == 8 and _count(session, Adjustment) == 6
    assert {ccn: _claim(session, ccn).status for ccn in statuses} == statuses
    inline = import_835(session, _basic_text())
    assert inline.status == "duplicate"


def test_single_line_inline_text_with_bom(session, clinic):
    """Real 835 files are often one long line; a BOM in front of it must not turn it into a path."""
    import_claims_csv(session, CLAIMS_CSV, clinic_id=clinic.id)
    single_line = chr(0xFEFF) + _basic_text().replace("\n", "")
    assert "\n" not in single_line
    batch = import_835(session, single_line, file_name="single-line.835")
    assert batch.status == "done" and batch.counts == BASIC_COUNTS


def test_import_835_accepts_positional_arguments(session, clinic):
    """docs/SPEC.md names the parameters positionally: (session, path | text, file_name, created_by)."""
    batch = import_835(session, BASIC, "positional.835", "tester")
    assert batch.status == "done" and batch.file_name == "positional.835" and batch.created_by == "tester"


def test_transaction_sha_is_the_plain_file_hash_for_every_transaction_set():
    sha = "a" * 64
    assert transaction_sha(sha, 0) == sha
    assert transaction_sha(sha, 1) == sha
    assert all(len(transaction_sha(sha, i)) == 64 for i in (1, 9, 10, 99, 100, 999, 1000, 10**6))


# --------------------------------------------------------------------------------------
# Denials, statuses, adjustments
# --------------------------------------------------------------------------------------


def test_detect_rule_on_inputs_and_stored_rows(session, clinic):
    zero = Decimal("0")
    # CLP02 4 without any CAS: denied for the full charge, no reasons to classify
    denied = RemittanceClaim(
        claim_status_code="4", charge_amount=Decimal("210.00"), paid_amount=zero, remark_codes=["N54"]
    )
    assert detect(denied, []) == (True, Decimal("210.00"), [])

    # informational adjustments never make a denial, whatever their amount
    paid = RemittanceClaim(claim_status_code="1", charge_amount=Decimal("270.00"), paid_amount=Decimal("180"))
    info = [
        AdjustmentInput("claim", "CO", "45", Decimal("40")),
        AdjustmentInput("line", "PR", "1", Decimal("50"), line_number=1),
    ]
    assert detect(paid, info) == (False, zero, [])
    # neither do reversals (negative amounts) or group CR
    negative = [
        AdjustmentInput("claim", "CO", "197", Decimal("-10")),
        AdjustmentInput("claim", "CR", "197", Decimal("10")),
    ]
    assert detect(paid, negative) == (False, zero, [])

    # ordering: amount desc, claim before line on ties, then line number; line facts travel with the reason
    mixed = [
        AdjustmentInput(
            "line", "CO", "4", Decimal("60"), line_number=2, procedure_code="11102", rarcs=["M78"]
        ),
        AdjustmentInput(
            "line",
            "CO",
            "16",
            Decimal("60"),
            line_number=1,
            procedure_code="99213",
            modifiers=["25"],
            rarcs=["N29"],
        ),
        AdjustmentInput("claim", "CO", "197", Decimal("60"), rarcs=["N54"]),
        AdjustmentInput("line", "CO", "97", Decimal("100"), line_number=3),
        AdjustmentInput("claim", "CO", "45", Decimal("500")),
    ]
    is_denial, denied_amount, reasons = detect(paid, mixed)
    assert is_denial and denied_amount == Decimal("280")
    assert [(r.level, r.carc, r.line_number) for r in reasons] == [
        ("line", "97", 3),
        ("claim", "197", None),
        ("line", "16", 1),
        ("line", "4", 2),
    ]
    assert reasons[1].rarcs == ["N54"] and reasons[2].modifiers == ["25"] and reasons[2].rarcs == ["N29"]
    assert reasons[3].procedure_code == "11102" and reasons[3].rarcs == ["M78"]

    # a parsed (claim-level) adjustment is accepted too and carries the claim's remark codes
    is_denial, denied_amount, reasons = detect(
        denied, [ParsedAdjustment(group_code="CO", carc="197", amount=Decimal("210.00"))]
    )
    assert (is_denial, denied_amount) == (True, Decimal("210.00"))
    assert reasons[0].level == "claim" and reasons[0].rarcs == ["N54"]

    # stored rows: detect(rc, rc.adjustments) == detect_for(rc), reasons as denial_reasons_for
    _import_both(session, clinic)
    rcs = _rcs(session)
    expected = {
        "CLM-24001": (False, zero),
        "CLM-24002": (True, Decimal("210.00")),
        "CLM-24003": (True, Decimal("180.00")),
        "CLM-24004": (True, Decimal("60.00")),
    }
    for pcn, flags in expected.items():
        stored = rcs[pcn]
        result = detect(stored, stored.adjustments)
        assert result[:2] == flags and result == detect_for(stored)
        assert result[2] == denial_reasons_for(stored)


def test_is_denial_and_denied_amount_per_clp(session, clinic):
    _import_both(session, clinic)
    rcs = _rcs(session)
    assert (rcs["CLM-24001"].is_denial, rcs["CLM-24001"].denied_amount) == (False, Decimal("0"))
    assert (rcs["CLM-24002"].is_denial, rcs["CLM-24002"].denied_amount) == (True, Decimal("210.00"))
    assert (rcs["CLM-24003"].is_denial, rcs["CLM-24003"].denied_amount) == (True, Decimal("180.00"))
    assert (rcs["CLM-24004"].is_denial, rcs["CLM-24004"].denied_amount) == (True, Decimal("60.00"))


def test_claim_statuses(session, clinic):
    _import_both(session, clinic)
    assert _claim(session, "CLM-24001").status == "paid"
    assert _claim(session, "CLM-24002").status == "denied"
    assert _claim(session, "CLM-24003").status == "partial"
    assert _claim(session, "CLM-24004").status == "partial"
    assert _claim(session, "CLM-24005").status == "submitted"
    assert _claim(session, "CLM-24006").status == "submitted"


def test_claim_status_rule():
    rc = RemittanceClaim(claim_status_code="1", paid_amount=Decimal("10"), is_denial=False)
    assert claim_status_for(rc) == "paid"
    rc = RemittanceClaim(claim_status_code="22", paid_amount=Decimal("10"), is_denial=False)
    assert claim_status_for(rc) is None  # reversal: left for the outcomes module
    rc = RemittanceClaim(claim_status_code="1", paid_amount=Decimal("0"), is_denial=False)
    assert claim_status_for(rc) is None
    rc = RemittanceClaim(claim_status_code="4", paid_amount=Decimal("0"), is_denial=True)
    assert claim_status_for(rc) == "denied"
    rc = RemittanceClaim(claim_status_code="1", paid_amount=Decimal("5"), is_denial=True)
    assert claim_status_for(rc) == "partial"
    assert PAID_CLAIM_STATUS_CODES == {"1", "2", "3", "19", "20", "21"}


def test_adjustment_rows_with_denial_flags(session, clinic):
    _import_both(session, clinic)
    rcs = _rcs(session)
    assert _adjustment_tuples(rcs["CLM-24001"]) == [("line", 1, "CO", "45", Decimal("40.00"), False)]
    assert _adjustment_tuples(rcs["CLM-24002"]) == [("claim", None, "CO", "197", Decimal("210.00"), True)]
    assert _adjustment_tuples(rcs["CLM-24003"]) == [
        ("line", 1, "CO", "45", Decimal("40.00"), False),
        ("line", 2, "CO", "4", Decimal("180.00"), True),
    ]
    assert _adjustment_tuples(rcs["CLM-24004"]) == [
        ("line", 1, "PR", "1", Decimal("50.00"), False),
        ("line", 3, "CO", "16", Decimal("60.00"), True),
    ]
    line3 = next(line for line in rcs["CLM-24004"].lines if line.line_number == 3)
    assert line3.remark_codes == ["MA130", "N29"] and line3.procedure_code == "J3301"
    assert line3.adjustments[0].carc == "16"


def test_denial_reasons_rebuilt_from_stored_rows(session, clinic):
    _import_both(session, clinic)
    rcs = _rcs(session)
    assert denial_reasons_for(rcs["CLM-24001"]) == []

    [reason] = denial_reasons_for(rcs["CLM-24002"])
    assert reason.level == "claim" and reason.group_code == "CO" and reason.carc == "197"
    assert reason.rarcs == ["N54"] and reason.amount == Decimal("210.00")
    assert reason.line_number is None and reason.procedure_code is None
    assert reason.carc_text is None and reason.rarc_texts == {}

    [reason] = denial_reasons_for(rcs["CLM-24003"])
    assert (reason.level, reason.carc, reason.line_number, reason.procedure_code) == ("line", "4", 2, "11102")
    assert reason.rarcs == ["M78"] and reason.amount == Decimal("180.00")

    [reason] = denial_reasons_for(rcs["CLM-24004"])
    assert (reason.level, reason.carc, reason.line_number, reason.procedure_code) == (
        "line",
        "16",
        3,
        "J3301",
    )
    assert reason.rarcs == ["MA130", "N29"] and reason.modifiers == []


# --------------------------------------------------------------------------------------
# Line linking
# --------------------------------------------------------------------------------------


def _links(rc: RemittanceClaim) -> list[tuple[int, int | None]]:
    return [(line.line_number, line.claim_line.line_number if line.claim_line else None) for line in rc.lines]


def test_lines_linked_by_line_item_control_number(session, clinic):
    _import_both(session, clinic)
    rcs = _rcs(session)
    for pcn, rc in rcs.items():
        claim = _claim(session, pcn)
        by_number = {line.line_number: line for line in claim.lines}
        for remit_line in rc.lines:
            assert remit_line.claim_line_id == by_number[remit_line.line_number].id
            assert remit_line.claim_line.line_item_control_number == remit_line.line_item_control_number
    assert _links(rcs["CLM-24004"]) == [(1, 1), (2, 2), (3, 3)]


def test_lines_linked_by_procedure_and_date_then_position(session, clinic):
    import_claims_csv(session, CLAIMS_CSV, clinic_id=clinic.id)
    text = re.sub(r"REF\*6R\*[^~]*~\n", "", _basic_text())
    assert "REF*6R" not in text
    # CLM-24001 line 1 gets an unknown procedure code so only its position can link it; line 2 links
    # on (procedure, date).
    text = text.replace("SVC*HC:99213*150.00*110.00**1~", "SVC*HC:99999*150.00*110.00**1~", 1)
    batch = import_835(session, text, file_name="no-6r.835")
    assert batch.status == "done" and batch.counts["matched"] == 4
    rcs = _rcs(session)
    assert _links(rcs["CLM-24001"]) == [(1, 1), (2, 2)]
    assert _links(rcs["CLM-24003"]) == [(1, 1), (2, 2)]
    assert _links(rcs["CLM-24004"]) == [(1, 1), (2, 2), (3, 3)]
    assert all(line.line_item_control_number is None for rc in rcs.values() for line in rc.lines)


# --------------------------------------------------------------------------------------
# Matching failures, audit, payer resolution, errors
# --------------------------------------------------------------------------------------


def test_unmatched_when_claims_are_unknown(session, clinic):
    batch = import_835(session, BASIC, created_by="tester")
    assert batch.status == "done"
    assert batch.counts == {**BASIC_COUNTS, "matched": 0, "unmatched": 4}
    rcs = _rcs(session)
    assert all(rc.claim_id is None and rc.match_method is None for rc in rcs.values())
    assert [rc.is_denial for rc in rcs.values()] == [False, True, True, True]
    remittance = session.execute(select(Remittance)).scalar_one()
    assert remittance.payer_id is None  # no payer row exists yet
    event = session.execute(select(AuditLog).where(AuditLog.action == "import_completed")).scalar_one()
    assert event.entity_id == batch.id
    assert event.details["unmatched"] == 4
    assert event.details["unmatched_sequences"] == [
        {"remittance_id": remittance.id, "sequence": s} for s in (1, 2, 3, 4)
    ]
    assert "CLM-24001" not in str(event.details)


def test_claims_imported_after_the_835_are_matched(session, clinic, monkeypatch):
    """An 835 that arrives before its claims export is resolved by the claims import."""
    calls: list[int] = []
    fake = types.ModuleType("app.outcomes.matching")
    fake.record_outcomes_for_remittance = lambda sess, remittance: calls.append(remittance.id)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.outcomes.matching", fake)

    first = import_835(session, BASIC, created_by="tester")
    assert first.counts["unmatched"] == 4 and first.counts["matched"] == 0
    remittance = session.execute(select(Remittance)).scalar_one()
    assert calls == [remittance.id]

    batch = import_claims_csv(session, CLAIMS_CSV, clinic_id=clinic.id, created_by="tester")
    assert batch.status == "done" and batch.counts["remittance_claims_matched"] == 4
    assert calls == [remittance.id, remittance.id]  # the outcomes module sees the now-matched CLPs again
    rcs = _rcs(session)
    assert all(rc.match_method == "ccn" and rc.claim_id == _claim(session, pcn).id for pcn, rc in rcs.items())
    assert _links(rcs["CLM-24001"]) == [(1, 1), (2, 2)] and _links(rcs["CLM-24004"]) == [
        (1, 1),
        (2, 2),
        (3, 3),
    ]
    statuses = {
        ccn: _claim(session, ccn).status for ccn in ("CLM-24001", "CLM-24002", "CLM-24003", "CLM-24004")
    }
    assert statuses == {
        "CLM-24001": "paid",
        "CLM-24002": "denied",
        "CLM-24003": "partial",
        "CLM-24004": "partial",
    }
    assert [rc.is_denial for rc in rcs.values()] == [False, True, True, True]  # detection ran at import time
    event = session.execute(select(AuditLog).where(AuditLog.entity_id == batch.id)).scalar_one()
    assert event.details["remittance_claims_matched"] == 4 and "CLM-24001" not in str(event.details)

    # nothing is left to match: re-running is a no-op, as is a further claims import
    assert rematch_unmatched(session) == 0
    later = import_claims_csv(session, CLAIMS_CSV.read_text(encoding="utf-8") + "\n", clinic_id=clinic.id)
    assert later.status == "done" and later.counts["remittance_claims_matched"] == 0
    assert calls == [remittance.id, remittance.id]


def test_audit_event_carries_counts_only(session, clinic):
    batch = _import_both(session, clinic)
    events = session.execute(select(AuditLog).order_by(AuditLog.id)).scalars().all()
    assert [e.action for e in events] == ["import_completed", "import_completed"]
    event = events[-1]
    assert event.actor_type == "system" and event.actor_id == "tester"
    assert event.entity_type == "import_batch" and event.entity_id == batch.id
    assert {k: event.details[k] for k in BASIC_COUNTS} == BASIC_COUNTS
    assert event.details["unmatched_sequences"] == [] and event.details["status"] == "done"
    assert "BRIGHTWATER" not in str(event.details) and "NHP884421001" not in str(event.details)


def test_multi_transaction_file(session, clinic):
    import_claims_csv(session, CLAIMS_CSV, clinic_id=clinic.id)
    batch = import_835(session, MULTI)
    assert batch.status == "done"
    assert batch.counts == {
        "transactions": 2,
        "claims": 2,
        "matched": 2,
        "unmatched": 0,
        "denials": 1,
        "lines": 4,
        "adjustments": 3,
        "outcomes": 0,
    }
    remittances = session.execute(select(Remittance).order_by(Remittance.transaction_index)).scalars().all()
    assert [r.transaction_index for r in remittances] == [0, 1]
    assert remittances[0].file_sha256 == batch.file_sha256
    assert remittances[1].file_sha256 == transaction_sha(batch.file_sha256, 1) == batch.file_sha256
    assert [r.trace_number for r in remittances] == ["NHP2026102200456", "NHP2026102200457"]
    assert [[rc.patient_control_number for rc in r.claims] for r in remittances] == [
        ["CLM-24001"],
        ["CLM-24003"],
    ]
    assert _claim(session, "CLM-24001").status == "paid" and _claim(session, "CLM-24003").status == "partial"
    assert import_835(session, MULTI).status == "duplicate"


def test_payer_resolution_by_identifier_then_name(session, clinic):
    import_claims_csv(session, CLAIMS_CSV, clinic_id=clinic.id)
    northwind = session.execute(select(Payer).where(Payer.payer_id == "NHP001")).scalar_one()

    # identifiers unknown -> exact (case-insensitive) name match
    text = _basic_text().replace("REF*2U*NHP001", "REF*2U*ZZ9999").replace("*XV*NHP001", "*XV*ZZ9999")
    text = text.replace("TRN*1*NHP2026101500123*1123456789", "TRN*1*NHP2026101500124*9999999999")
    batch = import_835(session, text, file_name="renamed-ids.835")
    assert batch.status == "done"
    remittance = session.execute(
        select(Remittance).where(Remittance.import_batch_id == batch.id)
    ).scalar_one()
    assert remittance.payer_id == northwind.id

    # TRN03 alone resolves it when it equals payers.payer_id
    text2 = text.replace("TRN*1*NHP2026101500124*9999999999", "TRN*1*NHP2026101500125*NHP001")
    text2 = text2.replace("N1*PR*NORTHWIND HEALTH PLAN", "N1*PR*SOME OTHER NAME")
    batch2 = import_835(session, text2, file_name="trn-only.835")
    remittance2 = session.execute(
        select(Remittance).where(Remittance.import_batch_id == batch2.id)
    ).scalar_one()
    assert remittance2.payer_id == northwind.id

    # nothing matches -> payer left unresolved, claims still match by control number
    text3 = text.replace("N1*PR*NORTHWIND HEALTH PLAN", "N1*PR*UNKNOWN PLAN")
    batch3 = import_835(session, text3, file_name="unknown-payer.835")
    remittance3 = session.execute(
        select(Remittance).where(Remittance.import_batch_id == batch3.id)
    ).scalar_one()
    assert remittance3.payer_id is None and batch3.counts["matched"] == 4


def test_parse_error_marks_batch_failed(session, clinic):
    batch = import_835(session, "ISA*00*this is not a complete interchange~", file_name="broken.835")
    assert batch.status == "failed" and batch.counts == {"transactions": 0}
    assert batch.error.startswith("parse error:")
    assert _count(session, Remittance) == 0
    event = session.execute(select(AuditLog)).scalar_one()
    assert event.action == "import_failed" and event.entity_id == batch.id
    # a bad file is not a successful import: importing a fixed file afterwards works
    assert import_835(session, BASIC).status == "done"


def test_inline_text_gets_default_file_name(session, clinic):
    batch = import_835(session, _basic_text())
    assert batch.file_name == "inline.835"
    batch2 = import_835(
        session, _basic_text().replace("TRN*1*NHP2026101500123", "TRN*1*OTHER"), file_name="named.835"
    )
    assert batch2.file_name == "named.835" and batch2.status == "done"


# --------------------------------------------------------------------------------------
# Outcomes hook (module built separately)
# --------------------------------------------------------------------------------------


def test_outcomes_hook_called_per_remittance(session, clinic, monkeypatch):
    calls: list[tuple[int, int]] = []
    fake = types.ModuleType("app.outcomes.matching")

    def record_outcomes_for_remittance(sess, remittance):
        assert sess is session
        calls.append((remittance.id, len(remittance.claims)))

    fake.record_outcomes_for_remittance = record_outcomes_for_remittance  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "app.outcomes.matching", fake)
    batch = _import_both(session, clinic)
    remittance = session.execute(select(Remittance)).scalar_one()
    assert calls == [(remittance.id, 4)]
    assert batch.counts["outcomes"] == 0


def test_outcomes_hook_missing_is_skipped(session, clinic, monkeypatch):
    monkeypatch.setitem(sys.modules, "app.outcomes.matching", None)
    batch = _import_both(session, clinic)
    assert batch.status == "done" and batch.counts == BASIC_COUNTS
