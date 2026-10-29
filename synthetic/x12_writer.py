"""Minimal X12 835 (005010X221A1) writer.

Used by `synthetic.generate` (one remittance per payer) and `synthetic.followup` (later remittances
that pay or deny submitted fixes). It emits exactly the segments docs/SPEC.md section 4.2 lists:
fixed-width ISA envelope (106 characters including the terminator), GS/GE and ST/SE with correct
counts and control numbers, '*' element separator, ':' sub-element separator, '^' repetition
separator and '~' segment terminator followed by a newline.

Claim dict shape (keys are optional unless marked required):

    patient_control_number      str      CLP01 (required): the clinic's claim control number
    status_code                 str      CLP02: 1 primary, 2 secondary, 4 denied, 22 reversal (default "1")
    charge                      Decimal  CLP03 total billed
    paid                        Decimal  CLP04 total paid
    patient_responsibility      Decimal  CLP05
    filing_indicator            str      CLP06 (default "12")
    payer_claim_control_number  str      CLP07
    facility_type_code          str      CLP08 (default "11")
    frequency_code              str      CLP09 (default "1")
    patient_last_name, patient_first_name, member_id       NM1*QC (member id with qualifier MI)
    rendering_provider          {"last_name", "first_name", "npi"}  NM1*82 (optional)
    received_date               date     DTM*050
    statement_from, statement_to date    DTM*232 / DTM*233
    adjustments                 list[(group, carc, amount)]  claim-level CAS
    rarcs                       list[str]  claim-level LQ*HE, written BEFORE the SVC loops so a
                                           state-machine parser attributes them to the claim
    lines                       list of line dicts:
        procedure (required), modifiers (list), charge, paid, units, service_date (DTM*472),
        line_item_control_number (REF*6R), allowed (Decimal | None -> AMT*B6),
        adjustments [(group, carc, amount)] (line-level CAS), rarcs [str] (line-level LQ*HE)

Payer dict: name, payer_id (REF*2U), tax_id (TRN03 = "1" + tax id digits), address
{line1, city, state, zip}, contact_name, phone.  Payee dict: name, npi (N104 with XX), tax_id
(REF*TJ), address {line1, city, state, zip}.

Balancing rule honoured by callers: CLP03 - CLP04 == sum of every CAS amount on the claim
(claim level plus line level), and BPR02 == sum of CLP04.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date
from decimal import Decimal
from typing import Any

ELEMENT_SEP = "*"
SUBELEMENT_SEP = ":"
REPETITION_SEP = "^"
SEGMENT_TERM = "~"
LINE_END = "\n"
ISA_LENGTH = 106
VERSION_ID = "005010X221A1"
CENT = Decimal("0.01")
MAX_CAS_TRIPLETS = 6

Adjustment = tuple[str, str, Decimal | int | str]


class Raw(str):
    """A pre-built element (e.g. an SVC composite) that must not be sanitized."""


def format_amount(value: Decimal | int | float | str) -> str:
    """X12 numeric: no thousands separators, at most two decimals, no trailing zeros ('150', '12.5', '-7')."""
    amount = Decimal(str(value)).quantize(CENT)
    text = f"{amount:f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("", "-0"):
        text = "0"
    return text


def x12_date(value: date) -> str:
    return value.strftime("%Y%m%d")


def _clean(value: Any) -> str:
    """Render one element; separators and line breaks inside free text become spaces."""
    if value is None:
        return ""
    if isinstance(value, Raw):
        return str(value)
    if isinstance(value, Decimal):
        return format_amount(value)
    if isinstance(value, date):
        return x12_date(value)
    text = str(value)
    for ch in (ELEMENT_SEP, SUBELEMENT_SEP, REPETITION_SEP, SEGMENT_TERM, "\r", "\n", "\t"):
        text = text.replace(ch, " ")
    return " ".join(text.split())


def segment(*elements: Any) -> str:
    """Join elements with '*', drop trailing empty elements, append '~'."""
    parts = [_clean(e) for e in elements]
    while parts and parts[-1] == "":
        parts.pop()
    return ELEMENT_SEP.join(parts) + SEGMENT_TERM


def _pad(value: str, width: int) -> str:
    text = _clean(value)[:width]
    return text.ljust(width)


def build_isa(
    *,
    sender_id: str,
    receiver_id: str,
    interchange_date: date,
    interchange_control_number: int,
    interchange_time: str = "0800",
    usage: str = "P",
) -> str:
    """Fixed-width ISA: 105 characters plus the '~' terminator."""
    icn = int(interchange_control_number)
    if not 0 <= icn <= 999_999_999:
        raise ValueError("interchange control number must fit in nine digits")
    elements = [
        "ISA",
        "00",
        " " * 10,
        "00",
        " " * 10,
        "ZZ",
        _pad(sender_id, 15),
        "ZZ",
        _pad(receiver_id, 15),
        interchange_date.strftime("%y%m%d"),
        str(interchange_time)[:4].rjust(4, "0"),
        REPETITION_SEP,
        "00501",
        f"{icn:09d}",
        "0",
        usage[:1] or "P",
        SUBELEMENT_SEP,
    ]
    isa = ELEMENT_SEP.join(elements) + SEGMENT_TERM
    if len(isa) != ISA_LENGTH:  # pragma: no cover - guarded by the fixed widths above
        raise AssertionError(f"ISA length {len(isa)} != {ISA_LENGTH}")
    return isa


def cas_segments(adjustments: Iterable[Adjustment]) -> list[str]:
    """One CAS per run of the same group code, at most six reason/amount pairs each."""
    items = [(str(g), str(c), Decimal(str(a))) for g, c, a in adjustments]
    out: list[str] = []
    index = 0
    while index < len(items):
        group = items[index][0]
        chunk: list[tuple[str, str, Decimal]] = []
        while index < len(items) and items[index][0] == group and len(chunk) < MAX_CAS_TRIPLETS:
            chunk.append(items[index])
            index += 1
        elements: list[Any] = ["CAS", group]
        for _, carc, amount in chunk:
            elements.extend([carc, format_amount(amount), ""])  # CAS04 quantity left empty
        out.append(segment(*elements))
    return out


def _digits(value: Any) -> str:
    return "".join(ch for ch in str(value or "") if ch.isdigit())


def _address_segments(address: dict[str, Any] | None) -> list[str]:
    if not address:
        return []
    segs: list[str] = []
    if address.get("line1"):
        segs.append(segment("N3", address.get("line1"), address.get("line2")))
    if address.get("city") or address.get("state") or address.get("zip"):
        segs.append(
            segment(
                "N4",
                address.get("city"),
                address.get("state"),
                _digits(address.get("zip")) or address.get("zip"),
            )
        )
    return segs


def _name_upper(value: Any) -> str:
    return _clean(value).upper()


def claim_segments(claim: dict[str, Any]) -> list[str]:
    """The 2100 loop (CLP ...) with its 2110 service loops."""
    pcn = claim.get("patient_control_number")
    if not pcn:
        raise ValueError("claim dict needs patient_control_number")
    segs: list[str] = [
        segment(
            "CLP",
            pcn,
            str(claim.get("status_code", "1")),
            format_amount(claim.get("charge", 0)),
            format_amount(claim.get("paid", 0)),
            format_amount(claim.get("patient_responsibility", 0)),
            str(claim.get("filing_indicator", "12")),
            claim.get("payer_claim_control_number"),
            str(claim.get("facility_type_code", "11")),
            str(claim.get("frequency_code", "1")),
        )
    ]
    segs.extend(cas_segments(claim.get("adjustments") or []))
    segs.append(
        segment(
            "NM1",
            "QC",
            "1",
            _name_upper(claim.get("patient_last_name")),
            _name_upper(claim.get("patient_first_name")),
            "",
            "",
            "",
            "MI" if claim.get("member_id") else "",
            claim.get("member_id"),
        )
    )
    rendering = claim.get("rendering_provider") or {}
    if rendering.get("npi"):
        segs.append(
            segment(
                "NM1",
                "82",
                "1",
                _name_upper(rendering.get("last_name")),
                _name_upper(rendering.get("first_name")),
                "",
                "",
                "",
                "XX",
                rendering.get("npi"),
            )
        )
    if claim.get("received_date"):
        segs.append(segment("DTM", "050", claim["received_date"]))
    if claim.get("statement_from"):
        segs.append(segment("DTM", "232", claim["statement_from"]))
    if claim.get("statement_to"):
        segs.append(segment("DTM", "233", claim["statement_to"]))
    # Claim-level remark codes precede the service loops on purpose (see module docstring).
    for rarc in claim.get("rarcs") or []:
        segs.append(segment("LQ", "HE", rarc))
    for line in claim.get("lines") or []:
        segs.extend(service_segments(line))
    return segs


def service_segments(line: dict[str, Any]) -> list[str]:
    """The 2110 loop (SVC ...)."""
    procedure = line.get("procedure")
    if not procedure:
        raise ValueError("line dict needs procedure")
    modifiers = [m for m in (line.get("modifiers") or []) if m]
    composite = Raw(SUBELEMENT_SEP.join(["HC", _clean(procedure), *[_clean(m) for m in modifiers[:4]]]))
    units = line.get("units", 1)
    segs: list[str] = [
        segment(
            "SVC",
            composite,
            format_amount(line.get("charge", 0)),
            format_amount(line.get("paid", 0)),
            "",
            format_amount(units) if units not in (None, "") else "1",
        )
    ]
    if line.get("service_date"):
        segs.append(segment("DTM", "472", line["service_date"]))
    segs.extend(cas_segments(line.get("adjustments") or []))
    if line.get("line_item_control_number"):
        segs.append(segment("REF", "6R", line["line_item_control_number"]))
    if line.get("allowed") is not None:
        segs.append(segment("AMT", "B6", format_amount(line["allowed"])))
    for rarc in line.get("rarcs") or []:
        segs.append(segment("LQ", "HE", rarc))
    return segs


def transaction_segments(
    *,
    payer: dict[str, Any],
    payee: dict[str, Any],
    claims: Sequence[dict[str, Any]],
    payment_date: date,
    production_date: date,
    trace_number: str,
    receiver_id: str,
    transaction_control_number: int = 1,
    payment_method: str | None = None,
) -> list[str]:
    """ST ... SE for one 835 transaction (one payment from one payer)."""
    tcn = f"{int(transaction_control_number):04d}"
    total = sum((Decimal(str(c.get("paid", 0))) for c in claims), Decimal("0")).quantize(CENT)
    method = payment_method or ("ACH" if total > 0 else "NON")
    handling = "I" if total > 0 else "H"
    segs: list[str] = [segment("ST", "835", tcn)]
    bpr: list[Any] = ["BPR", handling, format_amount(total), "C", method] + [""] * 12
    bpr[16] = payment_date  # BPR16 check issue / EFT effective date
    segs.append(segment(*bpr))
    payer_tax = _digits(payer.get("tax_id")) or _digits(payer.get("payer_id")).rjust(9, "0")
    segs.append(segment("TRN", "1", trace_number, f"1{payer_tax}"))
    segs.append(segment("REF", "EV", receiver_id))
    segs.append(segment("DTM", "405", production_date))
    segs.append(segment("N1", "PR", _name_upper(payer.get("name"))))
    segs.extend(_address_segments(payer.get("address")))
    segs.append(segment("REF", "2U", payer.get("payer_id")))
    if payer.get("contact_name") or payer.get("phone"):
        segs.append(
            segment(
                "PER",
                "BL",
                _name_upper(payer.get("contact_name") or "PROVIDER SERVICES"),
                "TE",
                _digits(payer.get("phone")),
            )
        )
    segs.append(segment("N1", "PE", _name_upper(payee.get("name")), "XX", payee.get("npi")))
    segs.extend(_address_segments(payee.get("address")))
    if payee.get("tax_id"):
        segs.append(segment("REF", "TJ", _digits(payee["tax_id"])))
    segs.append(segment("LX", "1"))
    for claim in claims:
        segs.extend(claim_segments(claim))
    segs.append(segment("SE", str(len(segs) + 1), tcn))
    return segs


def envelope(
    transactions: Sequence[Sequence[str]],
    *,
    sender_id: str,
    receiver_id: str,
    interchange_date: date,
    interchange_control_number: int,
    group_control_number: int | None = None,
    interchange_time: str = "0800",
) -> str:
    """Wrap ST..SE transaction segment lists in ISA/GS ... GE/IEA and render the file text."""
    icn = int(interchange_control_number)
    gcn = int(group_control_number) if group_control_number is not None else icn
    segs: list[str] = [
        build_isa(
            sender_id=sender_id,
            receiver_id=receiver_id,
            interchange_date=interchange_date,
            interchange_control_number=icn,
            interchange_time=interchange_time,
        ),
        segment(
            "GS",
            "HP",
            sender_id,
            receiver_id,
            interchange_date,
            str(interchange_time)[:4].rjust(4, "0"),
            str(gcn),
            "X",
            VERSION_ID,
        ),
    ]
    for transaction in transactions:
        segs.extend(transaction)
    segs.append(segment("GE", str(len(transactions)), str(gcn)))
    segs.append(segment("IEA", "1", f"{icn:09d}"))
    return LINE_END.join(segs) + LINE_END


def build_835(
    *,
    payer: dict[str, Any],
    payee: dict[str, Any],
    claims: Sequence[dict[str, Any]],
    payment_date: date,
    production_date: date,
    trace_number: str,
    interchange_control_number: int,
    sender_id: str,
    receiver_id: str,
    payment_method: str | None = None,
    transaction_control_number: int = 1,
    group_control_number: int | None = None,
    interchange_time: str = "0800",
) -> str:
    """One complete 835 file: one interchange, one functional group, one transaction."""
    transaction = transaction_segments(
        payer=payer,
        payee=payee,
        claims=claims,
        payment_date=payment_date,
        production_date=production_date,
        trace_number=trace_number,
        receiver_id=receiver_id,
        transaction_control_number=transaction_control_number,
        payment_method=payment_method,
    )
    return envelope(
        [transaction],
        sender_id=sender_id,
        receiver_id=receiver_id,
        interchange_date=production_date,
        interchange_control_number=interchange_control_number,
        group_control_number=group_control_number,
        interchange_time=interchange_time,
    )


def build_835_multi(
    *,
    transactions: Sequence[dict[str, Any]],
    interchange_control_number: int,
    sender_id: str,
    receiver_id: str,
    interchange_date: date,
    group_control_number: int | None = None,
    interchange_time: str = "0800",
) -> str:
    """One interchange holding several ST/SE transactions (e.g. one payment per payer).

    Each transaction dict carries the keyword arguments of `transaction_segments` except
    `receiver_id` and `transaction_control_number`, which are filled in here.
    """
    built: list[list[str]] = []
    for index, tx in enumerate(transactions, start=1):
        built.append(transaction_segments(receiver_id=receiver_id, transaction_control_number=index, **tx))
    return envelope(
        built,
        sender_id=sender_id,
        receiver_id=receiver_id,
        interchange_date=interchange_date,
        interchange_control_number=interchange_control_number,
        group_control_number=group_control_number,
        interchange_time=interchange_time,
    )


def split_segments(text: str) -> list[list[str]]:
    """Tiny tokenizer for tests and sanity checks: segments split on '~', elements on '*'."""
    out: list[list[str]] = []
    for raw in text.split(SEGMENT_TERM):
        raw = raw.strip("\r\n")
        if raw:
            out.append(raw.split(ELEMENT_SEP))
    return out


__all__ = [
    "ELEMENT_SEP",
    "ISA_LENGTH",
    "LINE_END",
    "SEGMENT_TERM",
    "SUBELEMENT_SEP",
    "VERSION_ID",
    "Raw",
    "build_835",
    "build_835_multi",
    "build_isa",
    "cas_segments",
    "claim_segments",
    "envelope",
    "format_amount",
    "segment",
    "service_segments",
    "split_segments",
    "transaction_segments",
    "x12_date",
]
