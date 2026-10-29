"""The guard: the ONLY path from this application to a model (docs/SPEC.md section 8).

`guarded_call` runs, in order:
1. `redact(variables, registry)`: every registry identifier becomes a `[[KIND_n]]` token.
2. `render(prompt_name, redacted)`: the prompt file's templates produce system + user text.
3. `mask_generic`: SSNs, e-mails, phone numbers and DOB-adjacent dates the registry could not
   know about are masked in place (defence in depth, not restorable).
4. `scan(system) + scan(user)` with the registry: any finding raises `RedactionError` and records
   an audit event `model_call_blocked` (counts only). Nothing is sent.
5. `client.complete(rendered)`. A client exception (transport, configuration, missing replay
   fixture) is recorded as a `model_calls` row with status `error` plus an audit event
   `model_call_failed` (exception class name and status code only) and re-raised.
6. Tokens inside the returned content are restored (dict values or string).
7. A `model_calls` row (counts, hashes, ids) and an audit event `model_call` are written.

Nothing here logs or stores prompt or response text.
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app import audit
from app.enums import ActorType, ModelCallStatus
from app.llm.base import LLMClient
from app.llm.prompts import render
from app.models import ModelCall
from app.redact.registry import IdentifierRegistry
from app.redact.scanner import RedactionError, mask_generic, scan
from app.redact.tokenizer import redact, restore
from app.schemas import LLMResult, RedactionMap, RenderedPrompt

log = logging.getLogger(__name__)

GuardCall = Callable[..., dict[str, Any] | str]


@dataclass
class GuardedResult:
    content: dict[str, Any] | str
    llm_result: LLMResult
    redaction_map: RedactionMap
    model_call_id: int | None


class LLMCallError(RuntimeError):
    """Base for guard-call failures. Carries the status (and error code) only."""

    def __init__(
        self, status: ModelCallStatus | str, error: str | None = None, prompt_name: str = ""
    ) -> None:
        self.status = ModelCallStatus(status)
        self.error = error
        self.prompt_name = prompt_name
        detail = f" error={error}" if error else ""
        super().__init__(
            f"model call {prompt_name or 'prompt'} ended with status={self.status.value}{detail}"
        )


class LLMRefusedError(LLMCallError):
    """The model refused (stop_reason refusal)."""


class LLMFailedError(LLMCallError):
    """The model call ended with status error (invalid_json, max_tokens, ...)."""


def _actor_id(client: LLMClient) -> str:
    return f"llm:{getattr(client, 'name', 'unknown')}"


def _count_kinds(findings: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.kind] = counts.get(finding.kind, 0) + 1
    return counts


def payload_sha256(rendered: RenderedPrompt) -> str:
    return hashlib.sha256((rendered.system + rendered.user).encode("utf-8")).hexdigest()


def _record_failure(
    session: Session | None,
    client: LLMClient,
    rendered: RenderedPrompt,
    exc: BaseException,
    redaction_map: RedactionMap,
    *,
    purpose: str,
    work_item_id: int | None,
    latency_ms: int,
) -> None:
    """The client raised (transport error, configuration, missing replay fixture): keep a trace.

    Writes a `model_calls` row with status `error` and an audit event `model_call_failed` that
    carry the exception class name and HTTP status code only, then returns so the caller can
    re-raise. A recording problem is logged, never raised over the original failure.
    """
    error = str(getattr(exc, "exception_name", None) or type(exc).__name__)
    status_code = getattr(exc, "status_code", None)
    if not isinstance(status_code, int):
        status_code = None
    log.warning(
        "model call failed prompt=%s version=%s error=%s status_code=%s work_item_id=%s",
        rendered.name,
        rendered.version,
        error,
        status_code,
        work_item_id,
    )
    if session is None:
        return
    try:
        row = ModelCall(
            prompt_name=rendered.name,
            prompt_version=rendered.version,
            model_id=str(getattr(client, "model_id", "unknown")),
            purpose=purpose or rendered.name,
            work_item_id=work_item_id,
            latency_ms=latency_ms,
            redaction_counts=dict(redaction_map.counts),
            status=str(ModelCallStatus.ERROR),
            error=error[:80],
            payload_sha256=payload_sha256(rendered),
        )
        session.add(row)
        session.flush()
        audit.record(
            session,
            actor_type=ActorType.AGENT,
            actor_id=_actor_id(client),
            action="model_call_failed",
            entity_type="work_item",
            entity_id=work_item_id,
            details={
                "prompt": rendered.name,
                "version": rendered.version,
                "purpose": purpose,
                "error": error,
                "status_code": status_code,
                "redaction_counts": dict(redaction_map.counts),
                "model_call_id": row.id,
            },
        )
    except Exception as record_exc:  # noqa: BLE001 - the caller re-raises the original failure
        log.warning(
            "model call failure not recorded prompt=%s error=%s", rendered.name, type(record_exc).__name__
        )


def guarded_call(
    session: Session | None,
    client: LLMClient,
    prompt_name: str,
    variables: dict[str, Any],
    registry: IdentifierRegistry | None,
    *,
    purpose: str,
    work_item_id: int | None = None,
    restore_tokens: bool = True,
    schema: dict[str, Any] | None = None,
) -> GuardedResult:
    """Redact -> render -> scan -> call -> restore -> record. See the module docstring."""
    registry = registry if registry is not None else IdentifierRegistry()
    redacted_variables, redaction_map = redact(variables, registry)
    rendered = render(prompt_name, redacted_variables)
    if schema is not None:
        rendered = rendered.model_copy(update={"schema_": schema})

    system_text, system_masked = mask_generic(rendered.system)
    user_text, user_masked = mask_generic(rendered.user)
    if system_masked or user_masked:
        rendered = rendered.model_copy(update={"system": system_text, "user": user_text})
        for kind, n in list(system_masked.items()) + list(user_masked.items()):
            redaction_map.counts[kind] = redaction_map.counts.get(kind, 0) + n

    findings = scan(rendered.system, registry, root="system") + scan(rendered.user, registry, root="user")
    if findings:
        counts = _count_kinds(findings)
        log.warning(
            "model call blocked prompt=%s version=%s findings=%d kinds=%s",
            rendered.name,
            rendered.version,
            len(findings),
            sorted(counts),
        )
        if session is not None:
            audit.record(
                session,
                actor_type=ActorType.AGENT,
                actor_id=_actor_id(client),
                action="model_call_blocked",
                entity_type="work_item",
                entity_id=work_item_id,
                details={
                    "prompt": rendered.name,
                    "version": rendered.version,
                    "purpose": purpose,
                    "findings": len(findings),
                    "finding_counts": counts,
                    "redaction_counts": dict(redaction_map.counts),
                },
            )
        raise RedactionError(findings)

    started = time.perf_counter()
    try:
        result = client.complete(rendered)
    except Exception as exc:
        _record_failure(
            session,
            client,
            rendered,
            exc,
            redaction_map,
            purpose=purpose,
            work_item_id=work_item_id,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
        raise
    content: dict[str, Any] | str = result.content
    if restore_tokens:
        content = restore(content, redaction_map)

    model_call_id: int | None = None
    if session is not None:
        row = ModelCall(
            prompt_name=rendered.name,
            prompt_version=rendered.version,
            model_id=result.model_id,
            purpose=purpose or rendered.name,
            work_item_id=work_item_id,
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            cache_read_tokens=result.usage.cache_read_tokens,
            latency_ms=result.latency_ms,
            redaction_counts=dict(redaction_map.counts),
            request_id=result.request_id,
            status=str(result.status),
            error=str(result.error)[:80] if result.error else None,
            payload_sha256=payload_sha256(rendered),
        )
        session.add(row)
        session.flush()
        model_call_id = row.id
        audit.record(
            session,
            actor_type=ActorType.AGENT,
            actor_id=_actor_id(client),
            action="model_call",
            entity_type="work_item",
            entity_id=work_item_id,
            details={
                "prompt": rendered.name,
                "version": rendered.version,
                "model": result.model_id,
                "status": str(result.status),
                "error": result.error,
                "input_tokens": result.usage.input_tokens,
                "output_tokens": result.usage.output_tokens,
                "redaction_counts": dict(redaction_map.counts),
                "model_call_id": model_call_id,
                "purpose": purpose,
            },
        )
    log.info(
        "model call prompt=%s status=%s model_call_id=%s work_item_id=%s",
        rendered.name,
        result.status,
        model_call_id,
        work_item_id,
    )
    return GuardedResult(
        content=content, llm_result=result, redaction_map=redaction_map, model_call_id=model_call_id
    )


def make_guard_call(
    session: Session | None,
    client: LLMClient,
    registry: IdentifierRegistry | None,
    *,
    work_item_id: int | None = None,
    purpose: str = "",
) -> GuardCall:
    """Adapter for the classifier and drafter: `call(prompt_name, variables, schema=None) -> content`.

    Raises LLMRefusedError / LLMFailedError (status only) instead of returning refusal/error results.
    """

    def call(
        prompt_name: str, variables: dict[str, Any], schema: dict[str, Any] | None = None
    ) -> dict[str, Any] | str:
        result = guarded_call(
            session,
            client,
            prompt_name,
            variables,
            registry,
            purpose=purpose or prompt_name,
            work_item_id=work_item_id,
            schema=schema,
        )
        status = result.llm_result.status
        if status == ModelCallStatus.REFUSAL:
            raise LLMRefusedError(status, result.llm_result.error, prompt_name)
        if status == ModelCallStatus.ERROR:
            raise LLMFailedError(status, result.llm_result.error, prompt_name)
        return result.content

    return call


__all__ = [
    "GuardCall",
    "GuardedResult",
    "LLMCallError",
    "LLMFailedError",
    "LLMRefusedError",
    "guarded_call",
    "make_guard_call",
    "payload_sha256",
]
