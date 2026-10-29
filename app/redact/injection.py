"""Prompt-injection detector for untrusted text (payer letters, portal text, note excerpts).

Documents are data, never instructions. `detect_injection` flags phrases that address an
assistant, ask for an action or try to change the task; `wrap_untrusted` fences a document in
`<untrusted_document>` tags so the prompt templates can present it as quoted data, with any tag
that could close the fence early escaped and any prompt-section marker (`--- user ---`,
`--- narrative ---`, `--- end narrative ---`: a run of three or more hyphens) neutralised by
`neutralize_section_markers`, so untrusted text can never forge a section boundary of the prompt
or of the letter it is quoted in.

The patterns are deliberately conservative: a normal denial letter quoting CARC/RARC codes,
policy language, deadlines and contact details must produce no findings, because a false
positive flags a work item and a false negative is caught by the model-side instruction to
treat everything inside the tags as data.
"""

from __future__ import annotations

import re
from html import escape

from app.schemas import InjectionFinding

SNIPPET_MAX_LENGTH = 120

_F = re.IGNORECASE

# (name, compiled regex). Whitespace inside phrases is matched with \s+ so "ignore   previous
# instructions" and mixed case are caught.
PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "ignore_instructions",
        re.compile(
            r"\b(?:ignore|disregard|forget)\s+"
            r"(?:(?:all|any|the|these|those|my|your|our|previous|prior|above|earlier|preceding|other)\s+){0,3}"
            r"(?:instructions?|directions?|guidelines?|rules)\b",
            _F,
        ),
    ),
    ("disregard_instructions", re.compile(r"\bdisregard\b[^.\n]{0,40}?\binstructions?\b", _F)),
    ("you_are_now", re.compile(r"\byou\s+are\s+now\b", _F)),
    ("system_prompt", re.compile(r"\bsystem\s+prompt\b", _F)),
    ("as_an_ai", re.compile(r"\bas\s+an\s+ai\b", _F)),
    (
        "approve_claim",
        re.compile(
            r"(?<!able\sto\s)(?<!cannot\s)(?<!can't\s)(?<!won't\s)(?<!not\s)(?<!never\s)"
            r"(?<!decline\sto\s)(?<!declined\sto\s)(?<!refuse\sto\s)(?<!refused\sto\s)"
            r"\bapprove\s+(?:this|the)\s+(?:claim|appeal|payment|request)\b",
            _F,
        ),
    ),
    ("mark_as_paid", re.compile(r"\bmark\b.{0,40}?\bas\s+(?:paid|approved|resolved)\b", _F)),
    ("do_not_flag", re.compile(r"\b(?:do\s+not|don'?t|never)\s+flag\b", _F)),
    ("assistant_role", re.compile(r"(?<![A-Za-z])assistant\s*:", _F)),
    ("chatml_marker", re.compile(r"<\|im_(?:start|end)\|>", _F)),
    ("markdown_system_header", re.compile(r"#{3,}\s*(?:system|instruction)", _F)),
    ("output_the_following", re.compile(r"\boutput\s+the\s+following\b", _F)),
    (
        "respond_with",
        re.compile(
            r"\brespond\s+only\s+with\b"
            r"|\bonly\s+respond\s+with\b"
            r"|\brespond\s+with\s+(?:only\b|just\b|exactly\b|the\s+following\b"
            r"|the\s+(?:word|text|json|category|value|string|phrase)\b|json\b|a\s+json\b|category\b"
            r"|[\"'{\[`])",
            _F,
        ),
    ),
    ("new_instructions", re.compile(r"\bnew\s+instructions\b", _F)),
    (
        "override",
        re.compile(
            r"\boverride\b(?:\s+\w+){0,3}?\s+"
            r"(?:instructions?|rules?|guidelines?|prompt|settings?|classification|category|decision|task)\b",
            _F,
        ),
    ),
    ("base64_blob", re.compile(r"[A-Za-z0-9+/]{80,}={0,2}")),
]

_CLOSE_TAG = re.compile(r"</\s*untrusted_document", re.IGNORECASE)
_OPEN_TAG = re.compile(r"<\s*untrusted_document", re.IGNORECASE)
# A prompt-section marker is a line (or line end) made of three or more hyphens around a label.
# Every such run is spaced out ("- - -") so the marker token no longer exists in the text.
_SECTION_MARKER = re.compile(r"-{3,}")


def detect_injection(text: str | None) -> list[InjectionFinding]:
    """Return every suspicious phrase in `text`, sorted by position."""
    if not text:
        return []
    source = str(text)
    findings: list[InjectionFinding] = []
    for name, pattern in PATTERNS:
        for match in pattern.finditer(source):
            snippet = match.group(0)
            if len(snippet) > SNIPPET_MAX_LENGTH:
                snippet = snippet[: SNIPPET_MAX_LENGTH - 3] + "..."
            findings.append(
                InjectionFinding(pattern=name, snippet=snippet, start=match.start(), end=match.end())
            )
    findings.sort(key=lambda f: (f.start, f.end, f.pattern))
    return findings


def is_suspicious(text: str | None) -> bool:
    return bool(detect_injection(text))


def neutralize_section_markers(text: str | None) -> str:
    """Space out every run of three or more hyphens (``---`` -> ``- - -``).

    Prompt files and the draft_appeal prompt delimit their sections with ``--- <label> ---`` lines,
    and the stub client reads the narrative back from between such markers. A note or letter that
    carries the same token could therefore end a section early or plant its own "narrative"; after
    this rewrite the text still reads the same but no longer contains the marker token. Idempotent.
    """
    if not text:
        return ""
    return _SECTION_MARKER.sub(lambda m: " ".join("-" * len(m.group(0))), str(text))


def wrap_untrusted(text: str | None, source: str, doc_id: str | int | None) -> str:
    """Fence untrusted text as data. Any tag that could close the fence early is escaped and every
    prompt-section marker is neutralised (see `neutralize_section_markers`)."""
    body = neutralize_section_markers(text)
    body = _CLOSE_TAG.sub("&lt;/untrusted_document", body)
    body = _OPEN_TAG.sub("&lt;untrusted_document", body)
    source_attr = escape(str(source), quote=True)
    id_attr = escape("" if doc_id is None else str(doc_id), quote=True)
    return f'<untrusted_document source="{source_attr}" id="{id_attr}">\n{body}\n</untrusted_document>'


__all__ = ["PATTERNS", "detect_injection", "is_suspicious", "neutralize_section_markers", "wrap_untrusted"]
