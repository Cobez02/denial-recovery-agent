"""Client selection from settings (`LLM_PROVIDER`): stub | replay | anthropic."""

from __future__ import annotations

import os
from pathlib import Path

from app.config import PROJECT_ROOT, Settings, get_settings
from app.llm.base import LLMClient
from app.llm.clients import AnthropicClient, RecordingClient, ReplayClient, StubClient

API_KEY_ENV = "ANTHROPIC_API_KEY"


class ConfigurationError(RuntimeError):
    """The selected provider cannot be built from the current settings."""


def replay_directory(settings: Settings) -> Path:
    path = Path(settings.llm_replay_dir)
    return path if path.is_absolute() else PROJECT_ROOT / path


def get_client(settings: Settings | None = None) -> LLMClient:
    """Build the model client for `settings.llm_provider`.

    `anthropic` requires ANTHROPIC_API_KEY in the process environment (read by the SDK itself) or
    in the git-ignored .env file (passed explicitly); otherwise a ConfigurationError is raised.
    With `llm_record` the real client is wrapped in a RecordingClient that writes replay fixtures.
    """
    settings = settings or get_settings()
    provider = settings.llm_provider
    if provider == "stub":
        return StubClient()
    if provider == "replay":
        return ReplayClient(replay_directory(settings))
    if provider == "anthropic":
        env_key = os.environ.get(API_KEY_ENV)
        api_key = None if env_key else settings.anthropic_api_key
        if not env_key and not api_key:
            raise ConfigurationError(
                f"LLM_PROVIDER=anthropic requires {API_KEY_ENV} in the environment (or .env); it is not set"
            )
        client: LLMClient = AnthropicClient(settings, api_key=api_key)
        if settings.llm_record:
            client = RecordingClient(client, replay_directory(settings))
        return client
    raise ConfigurationError(f"unknown LLM_PROVIDER {provider!r}")


__all__ = ["API_KEY_ENV", "ConfigurationError", "get_client", "replay_directory"]
