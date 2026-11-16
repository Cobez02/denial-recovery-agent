"""Finding identifiers in text for `denial-agent anonymize` and `deanonymize` (docs/SPEC.md section 16).

Everything is compared on a folded form (:func:`fold_map`): accents dropped (``José`` and ``Jose``),
the letters NFKD cannot take apart spelled out (``Æ`` as ``AE``, ``ß`` as ``ss``, ``ø`` as ``o``),
curly apostrophes and dashes made plain, lower case. A :class:`Matcher` keeps patterns in a trie
whose steps are characters, an optional separator (``O'Brien``, ``O Brien``, ``O-Brien`` and
``OBrien``; ``NW-458-049-016`` and ``NW458049016``) and a gap between words (``Smith, John``,
``J. Smith``). A match starts and ends on a word edge, and the longest one at each start wins.

Dates are read with :data:`TEXT_DATE_RE` in the forms notes and letters write them (ISO, M/D/YY,
M-D-YYYY, dotted, month names with or without a period, a comma or an ordinal, D Mon YYYY,
CCYYMMDD) and written back in the same form by :func:`render_date_like`.
"""

from __future__ import annotations

import functools
import re
import unicodedata
from collections.abc import Iterator
from datetime import date
from typing import Any

_SPECIAL: dict[str, str] = {
    "Æ": "AE",
    "æ": "ae",
    "Ø": "O",
    "ø": "o",
    "ß": "ss",
    "ẞ": "SS",
    "Œ": "OE",
    "œ": "oe",
    "Ł": "L",
    "ł": "l",
    "Đ": "D",
    "đ": "d",
    "Ð": "D",
    "ð": "d",
    "Þ": "TH",
    "þ": "th",
    "ı": "i",
    "Ħ": "H",
    "ħ": "h",
    "Ŧ": "T",
    "ŧ": "t",
    "Ŋ": "N",
    "ŋ": "n",
}
_APOSTROPHES = frozenset("’‘ʼ`´ʹ′‛")
# Hyphen, non-breaking hyphen, figure dash, en dash, em dash, horizontal bar and minus sign.
_DASHES = frozenset("\u2010\u2011\u2012\u2013\u2014\u2015\u2212")


@functools.lru_cache(maxsize=8192)
def fold_char(ch: str) -> str:
    """One character in its comparison form (case kept): ``é`` -> ``e``, ``Æ`` -> ``AE``, ``’`` -> ``'``."""
    if ch.isascii():
        return ch
    special = _SPECIAL.get(ch)
    if special is not None:
        return special
    if ch in _APOSTROPHES:
        return "'"
    if ch in _DASHES:
        return "-"
    decomposed = unicodedata.normalize("NFKD", ch)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


@functools.lru_cache(maxsize=8192)
def _fold_lower(ch: str) -> str:
    return "".join(c.lower() if len(c.lower()) == 1 else c for c in fold_char(ch))


def fold(text: str) -> str:
    """``text`` in its comparison form, case kept."""
    return text if text.isascii() else "".join(fold_char(ch) for ch in text)


def fold_map(text: str) -> tuple[str, list[int]]:
    """The folded, lower-cased text and, for each of its characters, the index in ``text`` it comes from."""
    if text.isascii():
        return text.lower(), list(range(len(text)))
    out: list[str] = []
    starts: list[int] = []
    for index, ch in enumerate(text):
        for piece in _fold_lower(ch):
            out.append(piece)
            starts.append(index)
    return "".join(out), starts


def original_span(text: str, starts: list[int], start: int, end: int) -> tuple[int, int]:
    """The span of ``text`` behind the folded span [start, end) (combining marks after it included)."""
    first = starts[start]
    last = starts[end - 1] + 1
    while last < len(text) and not _fold_lower(text[last]):
        last += 1
    return first, last


def name_key(value: str) -> str:
    """Comparison form of a name: folded, letters only, case folded ("O’Brien", "OBRIEN" -> "obrien")."""
    return "".join(ch for ch in fold(value) if ch.isalpha()).casefold()


def alnum_key(value: str) -> str:
    """Comparison form of an identifier: folded, letters and digits only, case folded."""
    return "".join(ch for ch in fold(value) if ch.isalnum()).casefold()


# --------------------------------------------------------------------------------------
# The trie
# --------------------------------------------------------------------------------------

OPTIONAL = "\x01"  # one optional separator between two parts
GAP = "\x02"  # one to four separators between two words
_ACCEPT = "\x00"
_STEP_MARKS = frozenset({OPTIONAL, GAP, _ACCEPT})
NAME_SEPARATORS = frozenset(" '-")
ID_SEPARATORS = frozenset(" -.")
GAP_CHARACTERS = frozenset(" \t\r\n,.")
MAX_GAP = 4


class Matcher:
    """Patterns (steps: characters, :data:`OPTIONAL`, :data:`GAP`) and what each stands for."""

    def __init__(self, separators: frozenset[str] = NAME_SEPARATORS) -> None:
        self._root: dict[str, Any] = {}
        self._separators = separators
        self.size = 0

    def __bool__(self) -> bool:
        return self.size > 0

    def add(self, steps: str, payload: Any) -> None:
        if not steps or steps[0] in _STEP_MARKS or steps[-1] in _STEP_MARKS:
            return
        node = self._root
        for step in steps:
            node = node.setdefault(step, {})
        payloads = node.setdefault(_ACCEPT, [])
        if payload not in payloads:
            payloads.append(payload)
            self.size += 1

    def find(self, low: str) -> list[tuple[int, int, list[Any]]]:
        """(start, end, payloads) of the longest match at every word start of the folded text."""
        found: list[tuple[int, int, list[Any]]] = []
        root = self._root
        if not root:
            return found
        for index, ch in enumerate(low):
            if ch not in root or (index > 0 and low[index - 1].isalnum()):
                continue
            best = self._longest(low, index)
            if best is not None:
                found.append((index, best[0], best[1]))
        return found

    def _longest(self, low: str, start: int) -> tuple[int, list[Any]] | None:
        length = len(low)
        best_end = -1
        best: list[Any] | None = None
        stack: list[tuple[dict[str, Any], int]] = [(self._root, start)]
        seen: set[tuple[int, int]] = set()
        while stack:
            node, position = stack.pop()
            marker = (id(node), position)
            if marker in seen:
                continue
            seen.add(marker)
            accepted = node.get(_ACCEPT)
            if (
                accepted is not None
                and position > best_end
                and (position == length or not low[position].isalnum())
            ):
                best_end, best = position, accepted
            if position < length:
                ch = low[position]
                if ch not in _STEP_MARKS:
                    child = node.get(ch)
                    if child is not None:
                        stack.append((child, position + 1))
            optional = node.get(OPTIONAL)
            if optional is not None:
                stack.append((optional, position))
                if position < length and low[position] in self._separators:
                    stack.append((optional, position + 1))
            gap = node.get(GAP)
            if gap is not None:
                end = position
                while end < length and end - position < MAX_GAP and low[end] in GAP_CHARACTERS:
                    end += 1
                    stack.append((gap, end))
        return None if best is None else (best_end, best)


def _camel_parts(word: str) -> list[str]:
    """``McDonald`` -> Mc, Donald; ``OBellweather`` -> O, Bellweather; all-caps words stay whole."""
    if word.isupper() or word.islower():
        return [word]
    parts: list[str] = []
    current = ""
    for index, ch in enumerate(word):
        nxt = word[index + 1] if index + 1 < len(word) else ""
        if current and ch.isupper() and (current[-1].islower() or (nxt and nxt.islower())):
            parts.append(current)
            current = ""
        current += ch
    if current:
        parts.append(current)
    return parts


def name_steps(value: str) -> str:
    """Steps for a name as written: its parts (split at spaces, hyphens, apostrophes and inner
    capitals) joined by an optional separator, so every way of joining them matches."""
    parts: list[str] = []
    for word in re.split(r"[\s'\u2019\-\u2010\u2011\u2012\u2013\u2014]+", fold(value)):
        for part in _camel_parts(word):
            letters = "".join(ch for ch in part if ch.isalnum()).lower()
            if letters:
                parts.append(letters)
    return OPTIONAL.join(parts)


def words_steps(*words: str) -> str:
    """Steps for words that follow each other with any word gap between them."""
    return GAP.join(step for step in (name_steps(word) for word in words) if step)


def id_steps(value: str, *, separators_inside: bool) -> str:
    """Steps for an identifier: its letters and digits, with an optional separator between any two
    of them when ``separators_inside`` (``NW-458-049-016``)."""
    key = alnum_key(value)
    return OPTIONAL.join(key) if separators_inside else key


# --------------------------------------------------------------------------------------
# Dates
# --------------------------------------------------------------------------------------

MONTH_NAMES: tuple[str, ...] = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
_MONTH_WORDS = sorted(
    {*MONTH_NAMES, *(m[:3] for m in MONTH_NAMES), "Sept"}, key=lambda word: (-len(word), word)
)
_MONTHS = "|".join(_MONTH_WORDS)
TEXT_DATE_RE = re.compile(
    r"(?<![\w/.-])(?:"
    r"(?P<y1>\d{4})(?P<s1>[-/.])(?P<m1>\d{1,2})(?P=s1)(?P<d1>\d{1,2})"
    r"|(?P<m2>\d{1,2})(?P<s2>[-/.])(?P<d2>\d{1,2})(?P=s2)(?P<y2>\d{4}|\d{2})"
    rf"|(?P<mon3>{_MONTHS})(?P<dot3>\.?)(?P<sp3>\s+)(?P<d3>\d{{1,2}})(?P<ord3>st|nd|rd|th)?"
    r"(?P<c3>,?)(?P<sp3b>\s+)(?P<y3>\d{4})"
    rf"|(?P<d4>\d{{1,2}})(?P<ord4>st|nd|rd|th)?(?P<s4>(?:\s+of\s+|[\s-]))(?P<mon4>{_MONTHS})(?P<dot4>\.?)"
    r"(?P<c4>,?)(?P<s4b>[\s-])(?P<y4>\d{4})"
    r"|(?P<c8>\d{8})"
    r")(?![\w/-]|\.\d)",
    re.IGNORECASE,
)
_FULL_MONTHS = frozenset(month.lower() for month in MONTH_NAMES)


def _year(text: str) -> int:
    value = int(text)
    if len(text) == 2:
        return 2000 + value if value < 70 else 1900 + value
    return value


def _month_index(word: str) -> int:
    lowered = word.lower()
    if lowered == "sept":
        return 9
    for index, month in enumerate(MONTH_NAMES):
        if lowered in (month.lower(), month[:3].lower()):
            return index + 1
    raise ValueError("not a month")


def parse_date_match(match: re.Match[str]) -> date | None:
    """The date a :data:`TEXT_DATE_RE` match spells, or None (``02/30/2026`` is not a date)."""
    g = match.groupdict()
    try:
        if g["y1"] is not None:
            return date(int(g["y1"]), int(g["m1"]), int(g["d1"]))
        if g["m2"] is not None:
            return date(_year(g["y2"]), int(g["m2"]), int(g["d2"]))
        if g["mon3"] is not None:
            return date(int(g["y3"]), _month_index(g["mon3"]), int(g["d3"]))
        if g["mon4"] is not None:
            return date(int(g["y4"]), _month_index(g["mon4"]), int(g["d4"]))
        digits = g["c8"]
        if 1900 <= int(digits[:4]) <= 2099:
            try:
                return date(int(digits[:4]), int(digits[4:6]), int(digits[6:]))
            except ValueError:
                pass
        return date(int(digits[4:]), int(digits[:2]), int(digits[2:4]))
    except (ValueError, OverflowError):
        return None


def _padded(*fields: str, default: bool) -> bool:
    """Write day and month with a leading zero? Yes when the original did anywhere, no when it
    wrote a one-digit field; when it cannot tell (12/25), ``default``."""
    if any(len(f) == 2 and f.startswith("0") for f in fields):
        return True
    if any(len(f) == 1 for f in fields):
        return False
    return default


def _number(value: int, padded: bool) -> str:
    return f"{value:02d}" if padded else str(value)


def _ordinal(day: int) -> str:
    if 10 <= day % 100 <= 20:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")


def _case_like(template: str, word: str) -> str:
    letters = [ch for ch in template if ch.isalpha()]
    if letters and all(ch.isupper() for ch in letters):
        return word.upper()
    if letters and all(ch.islower() for ch in letters):
        return word.lower()
    return word


def _month_word(template: str, month: int) -> str:
    if template.lower() == "sept" and month == 9:
        return template
    full = MONTH_NAMES[month - 1]
    return _case_like(template, full if template.lower() in _FULL_MONTHS else full[:3])


def render_date_like(match: re.Match[str], value: date) -> str:
    """``value`` written the way the match wrote its date (separators, padding, month word, case)."""
    g = match.groupdict()
    if g["y1"] is not None:  # year first: ISO pads unless the original did not
        sep, pad = g["s1"], _padded(g["m1"], g["d1"], default=True)
        return f"{value.year}{sep}{_number(value.month, pad)}{sep}{_number(value.day, pad)}"
    if g["m2"] is not None:
        sep, pad = g["s2"], _padded(g["m2"], g["d2"], default=False)
        year = f"{value.year % 100:02d}" if len(g["y2"]) == 2 else str(value.year)
        return f"{_number(value.month, pad)}{sep}{_number(value.day, pad)}{sep}{year}"
    if g["mon3"] is not None:
        ordinal = _case_like(g["ord3"], _ordinal(value.day)) if g["ord3"] else ""
        day = _number(value.day, g["d3"].startswith("0"))
        return (
            f"{_month_word(g['mon3'], value.month)}{g['dot3']}{g['sp3']}{day}"
            f"{ordinal}{g['c3']}{g['sp3b']}{value.year}"
        )
    if g["mon4"] is not None:
        ordinal = _case_like(g["ord4"], _ordinal(value.day)) if g["ord4"] else ""
        day = _number(value.day, g["d4"].startswith("0"))
        return (
            f"{day}{ordinal}{g['s4']}{_month_word(g['mon4'], value.month)}"
            f"{g['dot4']}{g['c4']}{g['s4b']}{value.year}"
        )
    digits = g["c8"]
    if 1900 <= int(digits[:4]) <= 2099 and parse_date_match(match) == _ccyymmdd_date(digits):
        return value.strftime("%Y%m%d")
    return value.strftime("%m%d%Y")


def _ccyymmdd_date(digits: str) -> date | None:
    try:
        return date(int(digits[:4]), int(digits[4:6]), int(digits[6:]))
    except ValueError:
        return None


def date_matches(text: str, *, compact: bool = True) -> Iterator[tuple[re.Match[str], date]]:
    """Every date written in ``text`` with its value; CCYYMMDD / MMDDYYYY runs only when ``compact``."""
    for match in TEXT_DATE_RE.finditer(text):
        if not compact and match.group("c8") is not None:
            continue
        value = parse_date_match(match)
        if value is not None:
            yield match, value


def date_values(match: re.Match[str], value: date) -> list[date]:
    """The date a match spells and, for a two-digit year, the same day a century away ("7/14/61"
    is 2061 by the usual rule, and 1961 for a date of birth)."""
    year = match.group("y2")
    if year is None or len(year) != 2:
        return [value]
    other = value.year - 100 if value.year >= 2000 else value.year + 100
    try:
        return [value, value.replace(year=other)]
    except ValueError:  # 29 February of a year that is not a leap year
        return [value]


DOB_CONTEXT_RE = re.compile(
    r"(?:\bd\.?\s?o\.?\s?b\b\.?|date\s+of\s+birth|birth\s*date|birthday|\bborn\b|\bbirth\b|\bb\.?d\.?\b)"
    r"[\s:#=(),.-]{0,6}(?:on\s+|is\s+)?$",
    re.IGNORECASE,
)


def in_dob_context(text: str, start: int) -> bool:
    """A date at ``start`` is labelled as a date of birth by the words just before it."""
    line_start = text.rfind("\n", 0, start) + 1
    return DOB_CONTEXT_RE.search(text[max(line_start, start - 40) : start]) is not None


__all__ = [
    "GAP",
    "ID_SEPARATORS",
    "NAME_SEPARATORS",
    "OPTIONAL",
    "TEXT_DATE_RE",
    "Matcher",
    "alnum_key",
    "date_matches",
    "date_values",
    "fold",
    "fold_char",
    "fold_map",
    "id_steps",
    "in_dob_context",
    "name_key",
    "name_steps",
    "original_span",
    "parse_date_match",
    "render_date_like",
    "words_steps",
]
