"""X12 835 (005010X221A1) parser -> ``schemas.Parsed835`` (docs/SPEC.md sections 4.2 and 6).

One ``Parsed835`` per ST/SE transaction set. Mapping, in file order:

* envelope: ISA06 sender, ISA08 receiver, ISA09 interchange date, ISA13 control number, GS06, ST02
* BPR01/02/03/04/16 and TRN02/03 -> ``payment``; DTM*405 -> production date
* N1*PR (+ N3, N4, REF*2U, PER) -> ``payer``; N1*PE (+ N3, N4, REF*TJ) -> ``payee``
* CLP -> one ``ParsedClaimPayment``; claim-level CAS, NM1*QC/IL/82, DTM*050/232/233, AMT*AU,
  other REF qualifiers into ``references``; LQ*HE before the first SVC -> claim remark codes.
  ``member_id`` is NM1*QC's NM109 under any identification qualifier a payer uses for a member
  (``MEMBER_ID_QUALIFIERS``: MI, MR Medicaid, HN Medicare HICN, II, 34), else NM1*IL's NM109
* MOA03-07 / MIA05,20-23 (where 5010 carries claim-level remark codes) -> claim remark codes,
  after any LQ*HE seen before the first SVC; the other MOA/MIA amounts are not modelled
* SVC -> one ``ParsedServicePayment``; DTM*472 (or DTM*150/151), line CAS, REF*6R, AMT*B6, LQ*HE
* PLB -> one ``ParsedProviderAdjustment`` per reason/amount pair

Every segment that does not feed a field (REF*EV, CUR, TS3, QTY, unknown ids ...) is kept verbatim
in ``unparsed_segments`` and never raises. Missing optional elements are tolerated; the only hard
failures are a bad envelope and a malformed or non-finite numeric element (``X12Error`` naming the
segment id and element index, never the value).
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

from app.ingest.dates import dtm_dates, parse_ccyymmdd, parse_yymmdd
from app.ingest.x12 import Delimiters, Segment, X12Error, read_delimiters, split_composite, tokenize
from app.schemas import (
    Parsed835,
    ParsedAdjustment,
    ParsedClaimPayment,
    ParsedPayee,
    ParsedPayer,
    ParsedPayment,
    ParsedProviderAdjustment,
    ParsedServicePayment,
)

# Parser states: which loop the next N3/N4/REF/DTM/PER/AMT/CAS/LQ segment belongs to.
_HEADER = "header"
_PAYER = "payer"
_PAYEE = "payee"
_OTHER_N1 = "other_n1"
_LX = "lx"
_CLAIM = "claim"
_SERVICE = "service"

_ZERO = Decimal("0")
_ONE = Decimal("1")

# CAS carries up to six (reason, amount, quantity) triplets starting at CAS02.
_CAS_TRIPLET_STARTS = (2, 5, 8, 11, 14, 17)
# PLB carries up to six (reason composite, amount) pairs starting at PLB03.
_PLB_PAIR_STARTS = (3, 5, 7, 9, 11, 13)
# Claim-level remark codes in 5010 sit in MOA (professional: MOA03-07) or MIA (institutional:
# MIA05 and MIA20-23); the remaining MOA/MIA elements are amounts and indicators not modelled here.
_MOA_REMARK_INDEXES = (3, 4, 5, 6, 7)
_MIA_REMARK_INDEXES = (5, 20, 21, 22, 23)
# NM108 qualifiers under which NM109 identifies the member: MI member id, MR Medicaid recipient id,
# HN Medicare HICN, II standard unique health identifier, 34 social security number.
MEMBER_ID_QUALIFIERS: frozenset[str] = frozenset({"MI", "MR", "HN", "II", "34"})


def _decimal(segment: Segment, index: int, default: Decimal | None) -> Decimal | None:
    value = segment.element(index)
    if value is None:
        return default
    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise X12Error(f"{segment.id} element {index:02d} is not a valid number") from exc
    if not number.is_finite():  # "NaN" / "Infinity" parse as Decimals but cannot be stored or compared
        raise X12Error(f"{segment.id} element {index:02d} is not a finite number")
    return number


def _money(segment: Segment, index: int) -> Decimal:
    value = _decimal(segment, index, _ZERO)
    assert value is not None
    return value


def _part(parts: list[str], index: int) -> str | None:
    """Composite part at ``index``; None when the composite is too short or the part is empty."""
    if index < len(parts) and parts[index]:
        return parts[index]
    return None


def parse_cas(segment: Segment) -> list[ParsedAdjustment]:
    """CAS01 group code followed by up to six reason/amount/quantity triplets."""
    group = segment.element(1) or ""
    adjustments: list[ParsedAdjustment] = []
    for start in _CAS_TRIPLET_STARTS:
        carc = segment.element(start)
        if carc is None:
            continue
        adjustments.append(
            ParsedAdjustment(
                group_code=group,
                carc=carc,
                amount=_money(segment, start + 1),
                quantity=_decimal(segment, start + 2, None),
            )
        )
    return adjustments


def parse_plb(segment: Segment, delimiters: Delimiters) -> list[ParsedProviderAdjustment]:
    provider_id = segment.element(1) or ""
    fiscal_period = parse_ccyymmdd(segment.element(2))
    adjustments: list[ParsedProviderAdjustment] = []
    for start in _PLB_PAIR_STARTS:
        reason_composite = segment.element(start)
        amount_text = segment.element(start + 1)
        if reason_composite is None and amount_text is None:
            continue
        parts = split_composite(reason_composite, delimiters.sub_element)
        adjustments.append(
            ParsedProviderAdjustment(
                provider_id=provider_id,
                fiscal_period_date=fiscal_period,
                reason_code=_part(parts, 0),
                reference=_part(parts, 1),
                amount=_money(segment, start + 1),
            )
        )
    return adjustments


class _TransactionBuilder:
    """Accumulates one ST..SE transaction set into a Parsed835."""

    def __init__(self, envelope: dict[str, object], delimiters: Delimiters) -> None:
        self.result = Parsed835(**envelope)  # type: ignore[arg-type]
        self.delimiters = delimiters
        self.state = _HEADER
        self.claim: ParsedClaimPayment | None = None
        self.service: ParsedServicePayment | None = None
        self.segment_count = 0

    # -- driver -------------------------------------------------------------------------------

    def handle(self, segment: Segment) -> None:
        self.segment_count += 1
        if not self._dispatch(segment):
            self.result.unparsed_segments.append(segment.raw)

    def finish(self) -> Parsed835:
        self.result.segment_count = self.segment_count
        return self.result

    def _dispatch(self, seg: Segment) -> bool:
        sid = seg.id
        if sid == "ST":
            self.result.transaction_control_number = seg.element(2)
            return True
        if sid == "SE":
            return True
        if sid == "BPR":
            return self._bpr(seg)
        if sid == "TRN":
            self.result.payment.trace_number = seg.element(2)
            self.result.payment.payer_identifier = seg.element(3)
            return True
        if sid == "N1":
            return self._n1(seg)
        if sid == "N3":
            return self._n3(seg)
        if sid == "N4":
            return self._n4(seg)
        if sid == "PER":
            return self._per(seg)
        if sid == "REF":
            return self._ref(seg)
        if sid == "DTM":
            return self._dtm(seg)
        if sid == "LX":
            self.state = _LX
            return True
        if sid == "CLP":
            return self._clp(seg)
        if sid == "CAS":
            return self._cas(seg)
        if sid == "NM1":
            return self._nm1(seg)
        if sid == "AMT":
            return self._amt(seg)
        if sid in ("MOA", "MIA"):
            return self._moa_mia(seg)
        if sid == "SVC":
            return self._svc(seg)
        if sid == "LQ":
            return self._lq(seg)
        if sid == "PLB":
            self.result.provider_adjustments.extend(parse_plb(seg, self.delimiters))
            return True
        return False

    # -- header ---------------------------------------------------------------------------------

    def _bpr(self, seg: Segment) -> bool:
        payment: ParsedPayment = self.result.payment
        payment.transaction_handling_code = seg.element(1)
        payment.amount = _money(seg, 2)
        payment.credit_debit = seg.element(3)
        payment.method = seg.element(4)
        payment.payment_date = parse_ccyymmdd(seg.element(16))
        return True

    def _n1(self, seg: Segment) -> bool:
        entity = seg.element(1)
        if entity == "PR":
            self.state = _PAYER
            payer: ParsedPayer = self.result.payer
            payer.name = seg.element(2)
            if seg.element(4) is not None:
                payer.payer_id = seg.element(4)
            return True
        if entity == "PE":
            self.state = _PAYEE
            payee: ParsedPayee = self.result.payee
            payee.name = seg.element(2)
            if seg.element(3) == "XX":
                payee.npi = seg.element(4)
            return True
        self.state = _OTHER_N1
        return False

    def _n3(self, seg: Segment) -> bool:
        if self.state == _PAYER:
            self.result.payer.address_line1 = seg.element(1)
            return True
        if self.state == _PAYEE:
            self.result.payee.address_line1 = seg.element(1)
            return True
        return False

    def _n4(self, seg: Segment) -> bool:
        target: ParsedPayer | ParsedPayee
        if self.state == _PAYER:
            target = self.result.payer
        elif self.state == _PAYEE:
            target = self.result.payee
        else:
            return False
        target.city = seg.element(1)
        target.state = seg.element(2)
        target.zip = seg.element(3)
        return True

    def _per(self, seg: Segment) -> bool:
        if self.state != _PAYER:
            return False
        if self.result.payer.contact is None:
            self.result.payer.contact = seg.element(2)
        return True

    def _ref(self, seg: Segment) -> bool:
        qualifier = seg.element(1)
        value = seg.element(2)
        if self.state == _PAYER and qualifier == "2U":
            self.result.payer.payer_id = value
            return True
        if self.state == _PAYEE and qualifier == "TJ":
            self.result.payee.tax_id = value
            return True
        if self.state == _CLAIM and self.claim is not None:
            if qualifier is not None and value is not None:
                self.claim.references[qualifier] = value
            return True
        if self.state == _SERVICE and self.service is not None and qualifier == "6R":
            self.service.line_item_control_number = value
            return True
        return False

    def _dtm(self, seg: Segment) -> bool:
        qualifier = seg.element(1)
        start, end = dtm_dates(seg)
        if self.state == _HEADER and qualifier == "405":
            self.result.payment.production_date = start
            return True
        if self.state == _CLAIM and self.claim is not None:
            if qualifier == "050":
                self.claim.claim_received_date = start
            elif qualifier == "232":
                self.claim.statement_from = start
            elif qualifier == "233":
                self.claim.statement_to = end or start
            else:
                return False
            return True
        if self.state == _SERVICE and self.service is not None:
            if qualifier == "472":
                self.service.service_date = start
            elif qualifier == "150":
                if self.service.service_date is None:
                    self.service.service_date = start
            elif qualifier != "151":
                return False
            return True
        return False

    # -- claim (2100) -------------------------------------------------------------------------

    def _clp(self, seg: Segment) -> bool:
        claim = ParsedClaimPayment(
            sequence=len(self.result.claims) + 1,
            patient_control_number=seg.element(1) or "",
            claim_status_code=seg.element(2) or "",
            charge_amount=_money(seg, 3),
            paid_amount=_money(seg, 4),
            patient_responsibility=_money(seg, 5),
            filing_indicator=seg.element(6),
            payer_claim_control_number=seg.element(7),
            facility_type_code=seg.element(8),
            frequency_code=seg.element(9),
        )
        self.result.claims.append(claim)
        self.claim = claim
        self.service = None
        self.state = _CLAIM
        return True

    def _cas(self, seg: Segment) -> bool:
        if self.state == _SERVICE and self.service is not None:
            self.service.adjustments.extend(parse_cas(seg))
            return True
        if self.state == _CLAIM and self.claim is not None:
            self.claim.adjustments.extend(parse_cas(seg))
            return True
        return False

    def _nm1(self, seg: Segment) -> bool:
        if self.claim is None or self.state not in (_CLAIM, _SERVICE):
            return False
        entity = seg.element(1)
        if entity == "QC":
            self.claim.patient_last_name = seg.element(3)
            self.claim.patient_first_name = seg.element(4)
            if self._member_identifier(seg) is not None:
                self.claim.member_id = self._member_identifier(seg)
            return True
        if entity == "IL":
            self.claim.insured_last_name = seg.element(3)
            self.claim.insured_first_name = seg.element(4)
            if self.claim.member_id is None:  # a dependent's claim may carry the id on the subscriber only
                self.claim.member_id = self._member_identifier(seg)
            return True
        if entity == "82":
            if seg.element(8) == "XX":
                self.claim.rendering_provider_npi = seg.element(9)
            return True
        return False

    @staticmethod
    def _member_identifier(seg: Segment) -> str | None:
        """NM109 when NM108 is a member-id qualifier (``MEMBER_ID_QUALIFIERS``)."""
        if seg.element(8) in MEMBER_ID_QUALIFIERS:
            return seg.element(9)
        return None

    def _amt(self, seg: Segment) -> bool:
        qualifier = seg.element(1)
        if self.state == _CLAIM and self.claim is not None and qualifier == "AU":
            self.claim.coverage_amount = _money(seg, 2)
            return True
        if self.state == _SERVICE and self.service is not None and qualifier == "B6":
            self.service.allowed_amount = _money(seg, 2)
            return True
        return False

    def _moa_mia(self, seg: Segment) -> bool:
        """Claim-level remark codes carried by MOA/MIA (loop 2100, so only before the first SVC)."""
        if self.state != _CLAIM or self.claim is None:
            return False
        indexes = _MOA_REMARK_INDEXES if seg.id == "MOA" else _MIA_REMARK_INDEXES
        for index in indexes:
            code = seg.element(index)
            if code is not None:
                self.claim.remark_codes.append(code)
        return True

    def _lq(self, seg: Segment) -> bool:
        if seg.element(1) != "HE":
            return False
        code = seg.element(2)
        if self.state == _SERVICE and self.service is not None:
            if code is not None:
                self.service.remark_codes.append(code)
            return True
        if self.state == _CLAIM and self.claim is not None:
            if code is not None:
                self.claim.remark_codes.append(code)
            return True
        return False

    # -- service (2110) -----------------------------------------------------------------------

    def _svc(self, seg: Segment) -> bool:
        if self.claim is None:
            return False
        sub = self.delimiters.sub_element
        composite = split_composite(seg.element(1), sub)
        original = split_composite(seg.element(6), sub)
        units = _decimal(seg, 5, _ONE)
        service = ParsedServicePayment(
            line_number=len(self.claim.services) + 1,
            qualifier=_part(composite, 0),
            procedure_code=_part(composite, 1),
            modifiers=[m for m in composite[2:6] if m],
            charge_amount=_money(seg, 2),
            paid_amount=_money(seg, 3),
            revenue_code=seg.element(4),
            units=units if units is not None else _ONE,
            original_procedure_code=_part(original, 1),
        )
        self.claim.services.append(service)
        self.service = service
        self.state = _SERVICE
        return True


def parse_835(text: str) -> list[Parsed835]:
    """Parse one interchange into one ``Parsed835`` per ST/SE transaction set."""
    delimiters = read_delimiters(text)
    envelope: dict[str, object] = {}
    results: list[Parsed835] = []
    builder: _TransactionBuilder | None = None

    for seg in tokenize(text, delimiters):
        sid = seg.id
        if sid == "ISA":
            envelope["sender_id"] = seg.element(6)
            envelope["receiver_id"] = seg.element(8)
            envelope["interchange_date"] = parse_yymmdd(seg.element(9))
            envelope["interchange_control_number"] = seg.element(13)
            continue
        if sid == "GS":
            envelope["group_control_number"] = seg.element(6)
            continue
        if sid in ("GE", "IEA"):
            if builder is not None:  # transaction set without an SE: close it anyway
                results.append(builder.finish())
                builder = None
            continue
        if sid == "ST":
            if builder is not None:
                results.append(builder.finish())
            builder = _TransactionBuilder(envelope, delimiters)
            builder.handle(seg)
            continue
        if builder is None:
            # A segment outside any ST/SE has nothing to attach to; the envelope is not a transaction.
            continue
        builder.handle(seg)
        if sid == "SE":
            results.append(builder.finish())
            builder = None

    if builder is not None:
        results.append(builder.finish())
    if not results:
        raise X12Error("interchange contains no ST transaction set")
    return results


__all__ = ["MEMBER_ID_QUALIFIERS", "parse_835", "parse_cas", "parse_plb"]
