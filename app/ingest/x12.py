"""X12 tokenizer (docs/SPEC.md section 6).

Delimiters are read from the fixed-width ISA segment: the element separator is the character
right after ``ISA`` (offset 3), the sub-element separator is ISA16 (offset 104) and the segment
terminator is the character after it (offset 105). Segment terminators may be followed by
``\\r\\n`` or ``\\n``; both are tolerated, as is whitespace (or a BOM) around the interchange.

Composite elements (``HC:99213:25``) are left as strings in ``Segment.elements``; callers break
them apart with :func:`split_composite` using ``Delimiters.sub_element``.

Error messages name positions only, never element values: an 835 is full of identifiers.
"""

from __future__ import annotations

from dataclasses import dataclass

ISA_SEGMENT_LENGTH = 106  # fixed width including the segment terminator
ISA_ELEMENT_COUNT = 16

_LEADING_NOISE = "﻿ \t\r\n"


class X12Error(ValueError):
    """Raised when an interchange envelope cannot be read or a numeric element is malformed."""


@dataclass(frozen=True)
class Delimiters:
    element: str = "*"
    sub_element: str = ":"
    segment: str = "~"
    repetition: str = "^"  # ISA11 in 005010


@dataclass
class Segment:
    """One X12 segment. ``elements[0]`` is the segment id; ``raw`` is the segment without terminator."""

    id: str
    elements: list[str]
    raw: str

    def element(self, index: int) -> str | None:
        """Element at ``index`` (0 is the segment id), stripped; None when missing or empty."""
        if 0 <= index < len(self.elements):
            value = self.elements[index].strip()
            return value or None
        return None

    def __len__(self) -> int:
        return len(self.elements)


def read_delimiters(text: str) -> Delimiters:
    """Derive the delimiters from the ISA segment at the start of ``text``."""
    body = text.lstrip(_LEADING_NOISE)
    if not body.startswith("ISA"):
        raise X12Error("interchange does not start with an ISA segment")
    if len(body) < ISA_SEGMENT_LENGTH:
        raise X12Error("ISA segment is shorter than the fixed 106 characters")
    element = body[3]
    # ISA has 16 elements. Walk to the 16th separator so a non-fixed-width ISA still works; for a
    # well-formed segment the separator sits at offset 103, ISA16 at 104 and the terminator at 105.
    position = 3
    for _ in range(ISA_ELEMENT_COUNT - 1):
        position = body.find(element, position + 1)
        if position < 0:
            raise X12Error("ISA segment does not have 16 elements")
    if position + 2 >= len(body):
        raise X12Error("ISA segment ends before the segment terminator")
    sub_element = body[position + 1]
    segment = body[position + 2]
    repetition = body[82] if len(body) > 82 else "^"
    if len({element, sub_element, segment}) != 3:
        raise X12Error("ISA delimiters are not distinct")
    return Delimiters(element=element, sub_element=sub_element, segment=segment, repetition=repetition)


def tokenize(text: str, delimiters: Delimiters | None = None) -> list[Segment]:
    """Split an interchange into segments. Empty chunks (trailing newlines, blank lines) are skipped."""
    delims = delimiters or read_delimiters(text)
    body = text.strip(_LEADING_NOISE)
    segments: list[Segment] = []
    for chunk in body.split(delims.segment):
        raw = chunk.strip()
        if not raw:
            continue
        elements = raw.split(delims.element)
        segments.append(Segment(id=elements[0].strip(), elements=elements, raw=raw))
    return segments


def split_composite(value: str | None, sep: str) -> list[str]:
    """Break a composite element into its stripped parts; an empty value yields an empty list."""
    if not value:
        return []
    return [part.strip() for part in value.split(sep)]


__all__ = [
    "Delimiters",
    "ISA_SEGMENT_LENGTH",
    "Segment",
    "X12Error",
    "read_delimiters",
    "split_composite",
    "tokenize",
]
