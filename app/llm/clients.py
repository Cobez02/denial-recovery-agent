"""Model clients. Only `app.llm.guard` may call them (docs/SPEC.md section 8).

- `AnthropicClient`: the real client on the Anthropic Python SDK, plain Messages API only
  (no batches, files or code execution: those are outside the BAA). Pinned model id, adaptive
  thinking, effort from settings, JSON-schema structured output when the prompt has a schema,
  streaming with `get_final_message()` above STREAM_THRESHOLD output tokens. Never logs prompt
  or response text; transport failures become `LLMTransportError` with the exception class name
  and status code only.
- `StubClient`: deterministic and offline. Maps the first CARC on the `reason_codes:` line to a
  category for `classify_denial`; echoes the narrative for `draft_appeal`.
- `ReplayClient`: serves fixtures from a directory keyed by sha256(name, version, user)[:16].
- `RecordingClient`: wraps another client and writes those fixtures (content and metadata only).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from app.config import Settings
from app.enums import DenialCategory, ModelCallStatus
from app.redact.injection import detect_injection
from app.schemas import LLMResult, LLMUsage, RenderedPrompt

log = logging.getLogger(__name__)

STREAM_THRESHOLD = 8000  # max_tokens above this use the streaming API with get_final_message()
SERVER_FALLBACK_BETA = "server-side-fallback-2026-07-01"
FIXTURE_KEY_LENGTH = 16


class LLMTransportError(RuntimeError):
    """A network or API-status failure. The message names the SDK exception and status code only."""

    def __init__(self, exception_name: str, status_code: int | None = None) -> None:
        self.exception_name = exception_name
        self.status_code = status_code
        super().__init__(f"{exception_name} status={status_code}")


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------------------
# Anthropic
# --------------------------------------------------------------------------------------


class AnthropicClient:
    """Plain Messages API client. Construct only through app.llm.factory.get_client."""

    name = "anthropic"

    def __init__(self, settings: Settings, *, api_key: str | None = None) -> None:
        import anthropic  # imported here so the SDK is only loaded when this provider is selected

        self._anthropic = anthropic
        self.model_id = settings.llm_model
        self._effort = settings.llm_effort
        self._server_fallbacks = bool(settings.llm_server_fallbacks)
        kwargs: dict[str, Any] = {
            "max_retries": settings.llm_max_retries,
            "timeout": settings.llm_timeout_seconds,
        }
        if api_key:  # only when the key came from .env rather than the process environment
            kwargs["api_key"] = api_key
        self._client = anthropic.Anthropic(**kwargs)
        self._transport_errors: tuple[type[BaseException], ...] = (
            anthropic.APIStatusError,
            anthropic.APIConnectionError,
        )

    def build_request(self, rendered: RenderedPrompt) -> dict[str, Any]:
        output_config: dict[str, Any] = {"effort": self._effort}
        if rendered.schema_ is not None:
            output_config["format"] = {"type": "json_schema", "schema": rendered.schema_}
        return {
            "model": self.model_id,
            "max_tokens": rendered.max_tokens,
            "system": [{"type": "text", "text": rendered.system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": rendered.user}],
            "thinking": {"type": "adaptive"},
            "output_config": output_config,
        }

    def _send(self, request: dict[str, Any], *, stream: bool) -> Any:
        if self._server_fallbacks:
            api = self._client.beta.messages
            request = {**request, "betas": [SERVER_FALLBACK_BETA], "fallbacks": "default"}
        else:
            api = self._client.messages
        if stream:
            with api.stream(**request) as message_stream:
                return message_stream.get_final_message()
        return api.create(**request)

    def complete(self, rendered: RenderedPrompt) -> LLMResult:
        request = self.build_request(rendered)
        started = time.perf_counter()
        try:
            message = self._send(request, stream=rendered.max_tokens > STREAM_THRESHOLD)
        except self._transport_errors as exc:
            status_code = getattr(exc, "status_code", None)
            log.warning(
                "model call failed prompt=%s version=%s error=%s status=%s",
                rendered.name,
                rendered.version,
                type(exc).__name__,
                status_code,
            )
            raise LLMTransportError(type(exc).__name__, status_code) from None
        latency_ms = int((time.perf_counter() - started) * 1000)
        result = self._parse(message, rendered, latency_ms)
        log.debug(
            "model call prompt=%s version=%s model=%s status=%s stop=%s in=%d out=%d cache_read=%d latency_ms=%d request_id=%s",
            rendered.name,
            rendered.version,
            result.model_id,
            result.status,
            result.stop_reason,
            result.usage.input_tokens,
            result.usage.output_tokens,
            result.usage.cache_read_tokens,
            result.latency_ms,
            result.request_id,
        )
        return result

    def _parse(self, message: Any, rendered: RenderedPrompt, latency_ms: int) -> LLMResult:
        blocks = getattr(message, "content", None) or []
        text = "".join(str(getattr(b, "text", "")) for b in blocks if getattr(b, "type", None) == "text")
        stop_reason = getattr(message, "stop_reason", None)
        usage_obj = getattr(message, "usage", None)
        usage = LLMUsage(
            input_tokens=_as_int(getattr(usage_obj, "input_tokens", 0)),
            output_tokens=_as_int(getattr(usage_obj, "output_tokens", 0)),
            cache_read_tokens=_as_int(getattr(usage_obj, "cache_read_input_tokens", 0)),
            cache_creation_tokens=_as_int(getattr(usage_obj, "cache_creation_input_tokens", 0)),
        )
        request_id = getattr(message, "_request_id", None)
        model_id = getattr(message, "model", None) or self.model_id
        has_schema = rendered.schema_ is not None

        status = ModelCallStatus.OK
        error: str | None = None
        content: dict[str, Any] | str = text
        if stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            category = getattr(details, "category", None) if details is not None else None
            status, content, error = ModelCallStatus.REFUSAL, "", (str(category) if category else "refusal")
        elif stop_reason == "max_tokens":
            status, error = ModelCallStatus.ERROR, "max_tokens"
            content = {} if has_schema else text
        elif has_schema:
            try:
                parsed = json.loads(text)
            except (TypeError, ValueError):
                parsed = None
            if isinstance(parsed, dict):
                content = parsed
            else:
                status, error, content = ModelCallStatus.ERROR, "invalid_json", {}

        return LLMResult(
            content=content,
            model_id=str(model_id),
            prompt_name=rendered.name,
            prompt_version=rendered.version,
            usage=usage,
            request_id=str(request_id) if request_id else None,
            stop_reason=str(stop_reason) if stop_reason else None,
            status=status,
            latency_ms=latency_ms,
            error=error,
        )


# --------------------------------------------------------------------------------------
# Stub
# --------------------------------------------------------------------------------------

CARC_CATEGORY_TABLE: dict[str, DenialCategory] = {
    **dict.fromkeys(("197", "198", "15", "62", "39"), DenialCategory.AUTHORIZATION),
    **dict.fromkeys(("4", "182"), DenialCategory.MODIFIER),
    "18": DenialCategory.DUPLICATE,
    "29": DenialCategory.TIMELY_FILING,
    **dict.fromkeys(("50", "56", "167", "11", "150", "151"), DenialCategory.MEDICAL_NECESSITY),
    **dict.fromkeys(("97", "234", "236", "231", "59"), DenialCategory.BUNDLING),
    **dict.fromkeys(("22", "23"), DenialCategory.COORDINATION_OF_BENEFITS),
    **dict.fromkeys(("26", "27", "31", "32", "33", "140", "177", "200"), DenialCategory.ELIGIBILITY),
    **dict.fromkeys(("16", "125", "226", "227", "252", "206"), DenialCategory.MISSING_INFORMATION),
}

_REASON_CODES_LINE = re.compile(r"^reason_codes:[ \t]*(.*)$", re.MULTILINE)
_GROUPED_CARC = re.compile(r"\b(?:CO|PR|OA|PI|CR)\s*-?\s*(\d{1,3})\b", re.IGNORECASE)
_BARE_CARC = re.compile(r"(?<![A-Za-z\d])(\d{1,3})(?![\d])")
_NARRATIVE = re.compile(r"--- narrative ---[ \t]*\n(.*?)\n[ \t]*--- end narrative ---", re.DOTALL)

NARRATIVE_START = "--- narrative ---"
NARRATIVE_END = "--- end narrative ---"


def first_carc(reason_codes_line: str) -> str | None:
    """The first CARC on a `reason_codes:` value such as 'CO-197, CO-16/N54' or '197'."""
    match = _GROUPED_CARC.search(reason_codes_line)
    if match:
        return match.group(1)
    match = _BARE_CARC.search(reason_codes_line)
    return match.group(1) if match else None


def stub_category_for(carc: str | None) -> DenialCategory:
    if carc is None:
        return DenialCategory.OTHER
    return CARC_CATEGORY_TABLE.get(carc.lstrip("0") or "0", DenialCategory.OTHER)


def extract_narrative(user_text: str) -> str:
    match = _NARRATIVE.search(user_text)
    return match.group(1).strip() if match else ""


class StubClient:
    """Deterministic, offline client used by the test suite and demos."""

    name = "stub"

    def __init__(self, model_id: str = "stub") -> None:
        self.model_id = model_id

    def complete(self, rendered: RenderedPrompt) -> LLMResult:
        content: dict[str, Any] | str
        if rendered.schema_ is not None and rendered.name == "classify_denial":
            content = self._classify(rendered)
        elif rendered.name == "draft_appeal":
            content = extract_narrative(rendered.user)
        else:
            content = {} if rendered.schema_ is not None else ""
        rendered_out = json.dumps(content, sort_keys=True) if isinstance(content, dict) else content
        usage = LLMUsage(
            input_tokens=max(1, (len(rendered.system) + len(rendered.user)) // 4),
            output_tokens=max(1, len(rendered_out) // 4),
        )
        return LLMResult(
            content=content,
            model_id=self.model_id,
            prompt_name=rendered.name,
            prompt_version=rendered.version,
            usage=usage,
            request_id=None,
            stop_reason="end_turn",
            status=ModelCallStatus.OK,
            latency_ms=0,
        )

    @staticmethod
    def _classify(rendered: RenderedPrompt) -> dict[str, Any]:
        line_match = _REASON_CODES_LINE.search(rendered.user)
        carc = first_carc(line_match.group(1)) if line_match else None
        category = stub_category_for(carc)
        injection_detected = "<untrusted_document" in rendered.user and bool(detect_injection(rendered.user))
        rationale = (
            f"stub: mapped from CARC {carc}" if carc is not None else "stub: no CARC found in reason_codes"
        )
        return {
            "category": category.value,
            "confidence": 0.6,
            "rationale": rationale,
            "injection_detected": injection_detected,
            "secondary_categories": [],
        }


# --------------------------------------------------------------------------------------
# Replay / Recording
# --------------------------------------------------------------------------------------


def fixture_key(rendered: RenderedPrompt) -> str:
    payload = f"{rendered.name}\n{rendered.version}\n{rendered.user}".encode()
    return hashlib.sha256(payload).hexdigest()[:FIXTURE_KEY_LENGTH]


def fixture_filename(rendered: RenderedPrompt) -> str:
    return f"{rendered.name}.{fixture_key(rendered)}.json"


class ReplayClient:
    """Serves recorded responses from `directory`; raises LookupError (key + prompt name) when missing."""

    name = "replay"

    def __init__(self, directory: str | Path, *, model_id: str = "replay") -> None:
        self.directory = Path(directory)
        self.model_id = model_id

    @staticmethod
    def fixture_key(rendered: RenderedPrompt) -> str:
        return fixture_key(rendered)

    def fixture_path(self, rendered: RenderedPrompt) -> Path:
        return self.directory / fixture_filename(rendered)

    def complete(self, rendered: RenderedPrompt) -> LLMResult:
        key = fixture_key(rendered)
        candidates = (self.directory / f"{rendered.name}.{key}.json", self.directory / f"{key}.json")
        path = next((p for p in candidates if p.is_file()), None)
        if path is None:
            raise LookupError(f"no replay fixture {key} for prompt {rendered.name}")
        data = json.loads(path.read_text(encoding="utf-8"))
        has_schema = rendered.schema_ is not None
        content: Any = data.get("content")
        if has_schema and isinstance(content, str):
            try:
                content = json.loads(content)
            except ValueError:
                content = {}
        if content is None:
            content = {} if has_schema else ""
        stop_reason = data.get("stop_reason")
        status_value = data.get("status") or ("refusal" if stop_reason == "refusal" else "ok")
        return LLMResult(
            content=content,
            model_id=str(data.get("model_id") or self.model_id),
            prompt_name=rendered.name,
            prompt_version=rendered.version,
            usage=LLMUsage.model_validate(data.get("usage") or {}),
            request_id=data.get("request_id"),
            stop_reason=stop_reason,
            status=ModelCallStatus(status_value),
            latency_ms=_as_int(data.get("latency_ms")),
            error=data.get("error"),
        )


class RecordingClient:
    """Calls `inner` and writes a replay fixture for every call (content and metadata only)."""

    def __init__(self, inner: Any, directory: str | Path) -> None:
        self.inner = inner
        self.directory = Path(directory)
        self.name = f"recording:{getattr(inner, 'name', 'unknown')}"

    @property
    def model_id(self) -> str:
        return str(getattr(self.inner, "model_id", "unknown"))

    def complete(self, rendered: RenderedPrompt) -> LLMResult:
        result = self.inner.complete(rendered)
        self.directory.mkdir(parents=True, exist_ok=True)
        path = self.directory / fixture_filename(rendered)
        payload = {
            "prompt_name": rendered.name,
            "prompt_version": rendered.version,
            "model_id": result.model_id,
            "stop_reason": result.stop_reason,
            "status": str(result.status),
            "error": result.error,
            "usage": result.usage.model_dump(),
            "content": result.content,
        }
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        log.debug("recorded model call fixture prompt=%s key=%s", rendered.name, fixture_key(rendered))
        return result


__all__ = [
    "CARC_CATEGORY_TABLE",
    "NARRATIVE_END",
    "NARRATIVE_START",
    "SERVER_FALLBACK_BETA",
    "STREAM_THRESHOLD",
    "AnthropicClient",
    "LLMTransportError",
    "RecordingClient",
    "ReplayClient",
    "StubClient",
    "extract_narrative",
    "first_carc",
    "fixture_filename",
    "fixture_key",
    "stub_category_for",
]
