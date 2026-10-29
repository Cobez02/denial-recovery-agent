"""Application settings. Values come from the environment or a git-ignored .env file.

Secrets (SECRET_KEY, ANTHROPIC_API_KEY) are never logged. The Anthropic SDK reads
ANTHROPIC_API_KEY from the environment itself; it is listed here only so .env works.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"

# Values of SECRET_KEY that must never sign a session cookie outside the test suite.
PLACEHOLDER_SECRET_KEYS = frozenset(
    {"change-me", "changeme", "secret", "change-me-in-.env-this-default-is-not-secret"}
)
MIN_SECRET_KEY_LENGTH = 24


def is_placeholder_secret(value: str | None) -> bool:
    """True for an empty, placeholder or too-short SECRET_KEY (see create_app and `denial-agent serve`)."""
    key = (value or "").strip()
    return not key or key.lower() in PLACEHOLDER_SECRET_KEYS or len(key) < MIN_SECRET_KEY_LENGTH


class Settings(BaseSettings):
    # The .env file is the project's own, wherever the process was started from.
    model_config = SettingsConfigDict(env_file=str(ENV_FILE), env_file_encoding="utf-8", extra="ignore")

    app_env: Literal["local", "test", "docker"] = "local"
    secret_key: str = Field(default="change-me-in-.env-this-default-is-not-secret", repr=False)
    database_url: str = "sqlite:///./data/denial_agent.db"
    redis_url: str = "redis://127.0.0.1:6379/0"
    job_backend: Literal["inline", "rq"] = "inline"

    # Model access. The model id is pinned on purpose: prompt regression tests run before any change.
    llm_provider: Literal["stub", "anthropic", "replay"] = "stub"
    llm_model: str = "claude-opus-5-5"
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    llm_max_retries: int = 2
    llm_timeout_seconds: float = 120.0
    llm_server_fallbacks: bool = False  # opt-in; a fallback model would break the pinned-model rule
    llm_record: bool = False  # when true the anthropic client also writes replay fixtures
    llm_replay_dir: str = "tests/fixtures/llm"
    anthropic_api_key: str | None = Field(default=None, repr=False)
    classify_model_threshold: float = 0.75
    model_enabled_for_drafting: bool = True

    auth_required: bool = True
    session_cookie_secure: bool = False
    session_max_age_seconds: int = 8 * 3600
    login_max_failures: int = 5
    login_lockout_minutes: int = 15

    data_dir: str = "./data"
    export_dir: str = "./data/exports"
    log_level: str = "INFO"
    deadline_warn_days: str = "30,14"
    clinic_id: int | None = None
    default_timely_filing_days: int = 90
    default_corrected_claim_days: int = 90
    default_appeal_days: int = 180

    @field_validator("deadline_warn_days")
    @classmethod
    def _validate_warn_days(cls, v: str) -> str:
        days = [int(x) for x in v.split(",") if x.strip()]
        if not days:
            raise ValueError("deadline_warn_days must list at least one integer")
        return ",".join(str(d) for d in sorted(days, reverse=True))

    @property
    def warn_days(self) -> list[int]:
        return [int(x) for x in self.deadline_warn_days.split(",")]

    @property
    def data_path(self) -> Path:
        p = Path(self.data_dir)
        return p if p.is_absolute() else PROJECT_ROOT / p

    @property
    def export_path(self) -> Path:
        p = Path(self.export_dir)
        return p if p.is_absolute() else PROJECT_ROOT / p

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    if os.environ.get("APP_ENV", "").lower() == "test":
        return Settings(_env_file=None)  # the suite is hermetic: no operator .env leaks into tests
    return Settings()


def reset_settings_cache() -> None:
    """Tests call this after changing environment variables."""
    get_settings.cache_clear()
