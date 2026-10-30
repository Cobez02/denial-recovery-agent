"""Shared fixtures.

Every test runs against a fresh SQLite database with the stub model client and inline jobs, so the
suite needs no Docker, Redis or API key. Fixtures that modules are expected to use:

- `settings`       : the active Settings (env already pointed at the temp DB)
- `engine`/`session`: SQLAlchemy engine and a session bound to the temp DB (tables created)
- `app`/`client`   : FastAPI app + TestClient; `client` is logged in through the real password+TOTP flow
- `reviewer`       : the User the client is logged in as (plus `reviewer_password`, `reviewer_totp`)
- `clinic`         : a Clinic row with id stored in settings.clinic_id
- `synthetic_batch`: generated synthetic files in tmp_path (claims_export.csv, notes.csv,
                     payer_documents.csv, remit_*.835, labels.json, manifest.json)
- `captured_llm_payloads`: list of RenderedPrompt objects sent to the model client during the test
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# Every environment variable Settings reads: cleared first so an operator's shell or .env never
# changes what the suite tests (get_settings also skips the .env file when APP_ENV=test).
SETTINGS_ENV_VARS = (
    "APP_ENV",
    "SECRET_KEY",
    "DATABASE_URL",
    "REDIS_URL",
    "JOB_BACKEND",
    "LLM_PROVIDER",
    "LLM_MODEL",
    "LLM_EFFORT",
    "LLM_MAX_RETRIES",
    "LLM_TIMEOUT_SECONDS",
    "LLM_SERVER_FALLBACKS",
    "LLM_RECORD",
    "LLM_REPLAY_DIR",
    "ANTHROPIC_API_KEY",
    "CLASSIFY_MODEL_THRESHOLD",
    "MODEL_ENABLED_FOR_DRAFTING",
    "AUTH_REQUIRED",
    "SESSION_COOKIE_SECURE",
    "SESSION_MAX_AGE_SECONDS",
    "LOGIN_MAX_FAILURES",
    "LOGIN_LOCKOUT_MINUTES",
    "DATA_DIR",
    "EXPORT_DIR",
    "LOG_LEVEL",
    "DEADLINE_WARN_DAYS",
    "CLINIC_ID",
    "DEFAULT_TIMELY_FILING_DAYS",
    "DEFAULT_CORRECTED_CLAIM_DAYS",
    "DEFAULT_APPEAL_DAYS",
)


def _set_test_env(tmp_path: Path) -> None:
    for name in SETTINGS_ENV_VARS:
        os.environ.pop(name, None)
    os.environ["APP_ENV"] = "test"
    os.environ["DATABASE_URL"] = f"sqlite:///{(tmp_path / 'test.db').as_posix()}"
    os.environ["LLM_PROVIDER"] = "stub"
    os.environ["JOB_BACKEND"] = "inline"
    os.environ["SECRET_KEY"] = "test-secret-key-not-for-production"
    os.environ["AUTH_REQUIRED"] = "true"
    os.environ["DATA_DIR"] = str(tmp_path / "data")
    os.environ["EXPORT_DIR"] = str(tmp_path / "data" / "exports")
    os.environ["LOG_LEVEL"] = "INFO"
    os.environ.pop("ANTHROPIC_API_KEY", None)


set_test_env = _set_test_env  # public name for module-scoped runs (tests/helpers.py)


@pytest.fixture()
def settings(tmp_path: Path):
    from app.config import get_settings, reset_settings_cache
    from app.db import dispose_engine

    _set_test_env(tmp_path)
    reset_settings_cache()
    dispose_engine()
    s = get_settings()
    yield s
    dispose_engine()
    reset_settings_cache()


@pytest.fixture()
def engine(settings):
    from app.db import configure_engine, init_db, make_engine

    eng = make_engine(settings.database_url)
    configure_engine(eng)
    init_db(eng)
    yield eng
    eng.dispose()


@pytest.fixture()
def session(engine) -> Iterator:
    from app.db import session_factory

    s = session_factory()()
    try:
        yield s
        s.rollback()
    finally:
        s.close()


@pytest.fixture()
def clinic(session, settings):
    from app.models import Clinic

    c = Clinic(name="Test Chiropractic Clinic", npi="1234567893", tax_id="12-3456789", pm_system="TestPM")
    session.add(c)
    session.commit()
    settings.clinic_id = c.id
    return c


@pytest.fixture()
def app(engine, settings):
    from app.main import create_app

    return create_app()


@pytest.fixture()
def reviewer_password() -> str:
    return "correct horse battery staple 42!"


@pytest.fixture()
def reviewer(session, reviewer_password):
    """Created through app.auth.service so the real hashing/TOTP code is exercised."""
    from app.auth.service import create_user

    user = create_user(session, email="reviewer@example.test", password=reviewer_password, role="reviewer")
    session.commit()
    return user


@pytest.fixture()
def client(app, reviewer, reviewer_password):
    """TestClient logged in via POST /login then POST /login/mfa with a valid TOTP code."""
    import pyotp
    from fastapi.testclient import TestClient

    c = TestClient(app, base_url="http://testserver", follow_redirects=False)
    r = c.post("/login", data={"email": reviewer.email, "password": reviewer_password})
    assert r.status_code == 303 and r.headers["location"].endswith("/login/mfa"), r.text
    code = pyotp.TOTP(reviewer.totp_secret).now()
    r = c.post("/login/mfa", data={"code": code})
    assert r.status_code == 303 and r.headers["location"].endswith("/queue"), r.text
    return c


@pytest.fixture()
def anon_client(app):
    from fastapi.testclient import TestClient

    return TestClient(app, base_url="http://testserver", follow_redirects=False)


@pytest.fixture(scope="session")
def synthetic_batch_dir(tmp_path_factory) -> Path:
    """200-claim synthetic batch generated once per test session (deterministic seed)."""
    from synthetic.generate import generate

    out = tmp_path_factory.mktemp("synthetic")
    generate(seed=20261028, n_claims=200, out_dir=out)
    return out


@pytest.fixture()
def synthetic_batch(synthetic_batch_dir: Path) -> Path:
    return synthetic_batch_dir


@pytest.fixture()
def captured_llm_payloads(monkeypatch):
    """Capture every RenderedPrompt that reaches the model client (after redaction)."""
    from app.llm import clients

    captured: list = []
    original = clients.StubClient.complete

    def _capture(self, rendered):
        captured.append(rendered)
        return original(self, rendered)

    monkeypatch.setattr(clients.StubClient, "complete", _capture)
    return captured
