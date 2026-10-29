"""Prompt registry: versioned prompt files rendered with Jinja2 into `RenderedPrompt` objects.

File layout: `app/llm/prompts/<name>.v<N>.md`

    ---
    name: classify_denial
    version: 1
    max_tokens: 8000
    schema: {...optional JSON schema...}
    ---
    <system template>
    --- user ---
    <user template>

Templates are Jinja2 with StrictUndefined (a missing variable is a bug, not silent text), no
autoescaping (prompts are plain text) and two extra filters: `wrap_untrusted` fences payer
documents and note excerpts as data, `money` formats Decimal/str amounts as `$1,234.56`.

The prompt version string is `<name>.v<N>@<sha256(file bytes)[:8]>`, so any edit to a prompt file
changes the version recorded on model_calls rows and invalidates replay fixtures.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import jinja2
import yaml
from pydantic import BaseModel

from app.redact.injection import wrap_untrusted
from app.schemas import RenderedPrompt

PROMPT_DIR = Path(__file__).resolve().parent / "prompts"
USER_MARKER = "--- user ---"
FRONT_MATTER_MARKER = "---"
DEFAULT_MAX_TOKENS = 2048

_FILE_RE = re.compile(r"^(?P<name>[a-z][a-z0-9_]*)\.v(?P<version>\d+)\.md$")


class PromptError(RuntimeError):
    """Base class for prompt registry errors (messages carry prompt names only)."""


class PromptNotFoundError(PromptError, LookupError):
    pass


class PromptFormatError(PromptError):
    pass


class PromptRenderError(PromptError):
    pass


# --------------------------------------------------------------------------------------
# Jinja environment and filters
# --------------------------------------------------------------------------------------


def money(value: Any) -> str:
    """Format an amount as `$1,234.56` (negative: `-$1,234.56`). Non-numbers are returned as text."""
    if value is None or value == "":
        return ""
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)
    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount):,.2f}"


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if isinstance(obj, (set, frozenset)):
        return sorted(obj, key=str)
    return str(obj)


def to_json(value: Any) -> str:
    return json.dumps(value, default=_json_default, sort_keys=True, ensure_ascii=False)


def _wrap_untrusted_filter(value: Any, source: str = "payer_document", doc_id: Any = "") -> str:
    return wrap_untrusted("" if value is None else str(value), source, doc_id)


def _make_environment() -> jinja2.Environment:
    env = jinja2.Environment(
        loader=jinja2.BaseLoader(),
        undefined=jinja2.StrictUndefined,
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=False,
    )
    env.filters["wrap_untrusted"] = _wrap_untrusted_filter
    env.filters["money"] = money
    env.filters["tojson"] = to_json
    return env


ENVIRONMENT = _make_environment()


# --------------------------------------------------------------------------------------
# Prompt files
# --------------------------------------------------------------------------------------


@dataclass
class PromptSpec:
    name: str
    version: int
    path: Path
    sha8: str
    max_tokens: int
    schema: dict[str, Any] | None
    system_source: str
    user_source: str
    system_template: jinja2.Template
    user_template: jinja2.Template

    @property
    def version_string(self) -> str:
        return f"{self.name}.v{self.version}@{self.sha8}"


def parse_prompt_file(path: Path) -> PromptSpec:
    raw = path.read_bytes()
    sha8 = hashlib.sha256(raw).hexdigest()[:8]
    text = raw.decode("utf-8").replace("\r\n", "\n")
    lines = text.split("\n")
    if not lines or lines[0].strip() != FRONT_MATTER_MARKER:
        raise PromptFormatError(f"{path.name}: prompt files start with a '---' YAML front matter line")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == FRONT_MATTER_MARKER), None)
    if end is None:
        raise PromptFormatError(f"{path.name}: unterminated YAML front matter")
    meta = yaml.safe_load("\n".join(lines[1:end])) or {}
    if not isinstance(meta, dict):
        raise PromptFormatError(f"{path.name}: front matter must be a mapping")

    body = lines[end + 1 :]
    marker = next((i for i, line in enumerate(body) if line.strip() == USER_MARKER), None)
    if marker is None:
        raise PromptFormatError(
            f"{path.name}: missing '{USER_MARKER}' line between system and user templates"
        )
    system_source = "\n".join(body[:marker]).strip("\n")
    user_source = "\n".join(body[marker + 1 :]).strip("\n")

    file_match = _FILE_RE.match(path.name)
    name = str(meta.get("name") or (file_match.group("name") if file_match else path.stem))
    version = int(meta.get("version") or (file_match.group("version") if file_match else 1))
    if file_match and (name != file_match.group("name") or version != int(file_match.group("version"))):
        raise PromptFormatError(f"{path.name}: front matter name/version do not match the file name")
    max_tokens = int(meta.get("max_tokens") or DEFAULT_MAX_TOKENS)
    schema = meta.get("schema")
    if schema is not None and not isinstance(schema, dict):
        raise PromptFormatError(f"{path.name}: schema must be a JSON schema mapping")

    try:
        system_template = ENVIRONMENT.from_string(system_source)
        user_template = ENVIRONMENT.from_string(user_source)
    except jinja2.TemplateSyntaxError as exc:
        raise PromptFormatError(f"{path.name}: template syntax error at line {exc.lineno}") from None

    return PromptSpec(
        name=name,
        version=version,
        path=path,
        sha8=sha8,
        max_tokens=max_tokens,
        schema=schema,
        system_source=system_source,
        user_source=user_source,
        system_template=system_template,
        user_template=user_template,
    )


_SPEC_CACHE: dict[Path, tuple[int, int, PromptSpec]] = {}


def load_prompt_file(path: Path) -> PromptSpec:
    """Parse a prompt file, cached by path + mtime + size."""
    resolved = Path(path).resolve()
    stat = resolved.stat()
    cached = _SPEC_CACHE.get(resolved)
    if cached is not None and cached[0] == stat.st_mtime_ns and cached[1] == stat.st_size:
        return cached[2]
    spec = parse_prompt_file(resolved)
    _SPEC_CACHE[resolved] = (stat.st_mtime_ns, stat.st_size, spec)
    return spec


def prompt_files(directory: str | Path | None = None) -> list[tuple[str, int, Path]]:
    """(name, version, path) for every prompt file, sorted by name then version."""
    base = Path(directory) if directory is not None else PROMPT_DIR
    found: list[tuple[str, int, Path]] = []
    if base.is_dir():
        for path in base.iterdir():
            match = _FILE_RE.match(path.name)
            if match and path.is_file():
                found.append((match.group("name"), int(match.group("version")), path))
    found.sort(key=lambda item: (item[0], item[1]))
    return found


def list_prompts(directory: str | Path | None = None) -> list[PromptSpec]:
    return [load_prompt_file(path) for _, _, path in prompt_files(directory)]


def available_versions(name: str, directory: str | Path | None = None) -> list[int]:
    return [version for n, version, _ in prompt_files(directory) if n == name]


def get_prompt(name: str, *, version: int | None = None, directory: str | Path | None = None) -> PromptSpec:
    candidates = [(v, path) for n, v, path in prompt_files(directory) if n == name]
    if not candidates:
        raise PromptNotFoundError(f"no prompt file for {name!r}")
    if version is None:
        _, path = max(candidates)
    else:
        matches = [path for v, path in candidates if v == version]
        if not matches:
            raise PromptNotFoundError(f"prompt {name!r} has no version {version}")
        path = matches[0]
    return load_prompt_file(path)


def render(
    name: str,
    variables: dict[str, Any] | None,
    *,
    version: int | None = None,
    directory: str | Path | None = None,
) -> RenderedPrompt:
    """Render the highest (or the given) version of a prompt with `variables`."""
    spec = get_prompt(name, version=version, directory=directory)
    context = dict(variables or {})
    try:
        system = spec.system_template.render(context).strip()
        user = spec.user_template.render(context).strip()
    except jinja2.UndefinedError as exc:
        raise PromptRenderError(f"prompt {spec.version_string}: {exc}") from None
    except jinja2.TemplateError as exc:
        raise PromptRenderError(f"prompt {spec.version_string}: {type(exc).__name__}") from None
    return RenderedPrompt(
        name=spec.name,
        version=spec.version_string,
        system=system,
        user=user,
        schema=copy.deepcopy(spec.schema),
        max_tokens=spec.max_tokens,
    )


__all__ = [
    "DEFAULT_MAX_TOKENS",
    "ENVIRONMENT",
    "PROMPT_DIR",
    "USER_MARKER",
    "PromptError",
    "PromptFormatError",
    "PromptNotFoundError",
    "PromptRenderError",
    "PromptSpec",
    "available_versions",
    "get_prompt",
    "list_prompts",
    "load_prompt_file",
    "money",
    "parse_prompt_file",
    "prompt_files",
    "render",
    "to_json",
]
