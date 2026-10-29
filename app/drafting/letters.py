"""Plain-text letter templates (Jinja2) in app/templates/letters/.

`base.txt.j2` is the letter frame. Each category has `narrative_<category>.txt.j2` with two blocks,
`narrative` and `request`; `narrative_generic.txt.j2` covers categories without their own file. The
blocks are rendered separately so the model may rewrite the narrative paragraph only.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from app.enums import DenialCategory

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "letters"
BASE_TEMPLATE = "base.txt.j2"
GENERIC_NARRATIVE = "narrative_generic.txt.j2"

_BLANK_LINES = re.compile(r"\n{3,}")
_TRAILING_SPACES = re.compile(r"[ \t]+\n")


@lru_cache(maxsize=1)
def get_environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=False,
        undefined=StrictUndefined,
    )


def narrative_template_name(category: DenialCategory | str) -> str:
    """'narrative_<category>.txt.j2' when it exists, else the generic narrative."""
    name = f"narrative_{DenialCategory(category).value}.txt.j2"
    return name if (TEMPLATE_DIR / name).is_file() else GENERIC_NARRATIVE


def available_narratives() -> list[str]:
    return sorted(p.name for p in TEMPLATE_DIR.glob("narrative_*.txt.j2"))


def render_template(template_name: str, variables: dict[str, Any]) -> str:
    return clean_text(get_environment().get_template(template_name).render(**variables))


def render_block(template_name: str, block: str, variables: dict[str, Any]) -> str:
    """Render one `{% block %}` of a template on its own."""
    template = get_environment().get_template(template_name)
    try:
        block_fn = template.blocks[block]
    except KeyError as exc:
        raise KeyError(f"template {template_name} has no block {block!r}") from exc
    context = template.new_context(variables)
    return clean_text("".join(block_fn(context)))


def clean_text(text: str) -> str:
    """Normalize line endings, strip trailing spaces, collapse runs of blank lines, end with one newline."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _TRAILING_SPACES.sub("\n", text)
    text = _BLANK_LINES.sub("\n\n", text)
    return text.strip() + "\n" if text.strip() else ""


__all__ = [
    "BASE_TEMPLATE",
    "GENERIC_NARRATIVE",
    "TEMPLATE_DIR",
    "available_narratives",
    "clean_text",
    "get_environment",
    "narrative_template_name",
    "render_block",
    "render_template",
]
