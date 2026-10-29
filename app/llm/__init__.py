"""Model access: client protocol, Anthropic/stub/replay/recording clients, prompt registry, guard.

Only `app.llm.guard` may call a client. Submodules are imported lazily so that importing
`app.llm` does not pull in SQLAlchemy models or the Anthropic SDK.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from app.llm.base import LLMClient

_LAZY: dict[str, str] = {
    "AnthropicClient": "app.llm.clients",
    "LLMTransportError": "app.llm.clients",
    "RecordingClient": "app.llm.clients",
    "ReplayClient": "app.llm.clients",
    "StubClient": "app.llm.clients",
    "ConfigurationError": "app.llm.factory",
    "get_client": "app.llm.factory",
    "GuardedResult": "app.llm.guard",
    "LLMFailedError": "app.llm.guard",
    "LLMRefusedError": "app.llm.guard",
    "guarded_call": "app.llm.guard",
    "make_guard_call": "app.llm.guard",
    "list_prompts": "app.llm.prompts",
    "render": "app.llm.prompts",
}


def __getattr__(name: str) -> Any:
    module_name = _LAZY.get(name)
    if module_name is None:
        raise AttributeError(f"module 'app.llm' has no attribute {name!r}")
    return getattr(import_module(module_name), name)


__all__ = ["LLMClient", *sorted(_LAZY)]
