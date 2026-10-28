"""Model client protocol. Only app.llm.guard may call a client; nothing else touches a model.

Implementations live in app/llm/clients.py: AnthropicClient (real, plain Messages API only),
StubClient (deterministic, offline), ReplayClient (fixtures), RecordingClient (wraps another client).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.schemas import LLMResult, RenderedPrompt


@runtime_checkable
class LLMClient(Protocol):
    name: str
    model_id: str

    def complete(self, rendered: RenderedPrompt) -> LLMResult:
        """Run one prompt. When `rendered.schema_` is set, `content` must be a dict matching it;
        otherwise `content` is the response text. Never raise on a model refusal: return
        status=refusal. Raise only on transport/configuration errors."""
        ...
