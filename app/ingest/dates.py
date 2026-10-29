"""Date helpers for X12 values. Every parser returns None instead of raising on bad input, so a
malformed optional date never aborts an import."""

from __future__ import annotations

from datetime import date

from app.ingest.x12 import Segment


def parse_ccyymmdd(value: str | None) -> date | None:
    """``20260804`` -> date(2026, 8, 4); anything else -> None."""
    if not value:
        return None
    v = value.strip()
    if len(v) != 8 or not v.isdigit():
        return None
    try:
        return date(int(v[:4]), int(v[4:6]), int(v[6:8]))
    except ValueError:
        return None


def parse_yymmdd(value: str | None) -> date | None:
    """ISA09 style ``261015`` -> date(2026, 10, 15). Two-digit years below 70 are 20xx, others 19xx."""
    if not value:
        return None
    v = value.strip()
    if len(v) != 6 or not v.isdigit():
        return None
    yy = int(v[:2])
    year = 2000 + yy if yy < 70 else 1900 + yy
    try:
        return date(year, int(v[2:4]), int(v[4:6]))
    except ValueError:
        return None


def parse_date_range(value: str | None) -> tuple[date | None, date | None]:
    """``CCYYMMDD-CCYYMMDD`` (RD8) -> (start, end); a single CCYYMMDD -> (d, d)."""
    if not value:
        return None, None
    v = value.strip()
    if "-" in v:
        start, _, end = v.partition("-")
        return parse_ccyymmdd(start), parse_ccyymmdd(end)
    d = parse_ccyymmdd(v)
    return d, d


def dtm_dates(segment: Segment) -> tuple[date | None, date | None]:
    """Dates carried by a DTM segment: DTM02 (D8), or DTM05/DTM06 when the date sits in a period."""
    single = parse_ccyymmdd(segment.element(2))
    if single is not None:
        return single, single
    if segment.element(5) in {"RD8", "D8"}:
        return parse_date_range(segment.element(6))
    return None, None


def format_ccyymmdd(value: date | None) -> str:
    return value.strftime("%Y%m%d") if value else ""


__all__ = ["dtm_dates", "format_ccyymmdd", "parse_ccyymmdd", "parse_date_range", "parse_yymmdd"]
