"""Database engine and session management.

SQLite is used by the test suite and for a zero-dependency local run; PostgreSQL 16 runs in Docker.
Models are dialect-neutral so both work from the same code.
"""

from __future__ import annotations

from collections.abc import Generator, Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.config import PROJECT_ROOT, get_settings

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _sqlite_pragmas(dbapi_connection, _record) -> None:  # pragma: no cover - trivial
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    # Overwrite freed bytes on every DELETE so a removed row (a patient, a note) is not left
    # readable in the file; `purge-clinic` additionally runs VACUUM once the rows are gone.
    cursor.execute("PRAGMA secure_delete=ON")
    cursor.close()


def resolve_sqlite_url(url: str) -> str:
    """Make relative SQLite paths resolve against the project root, not the CWD."""
    if url.startswith("sqlite:///./"):
        rel = url[len("sqlite:///./") :]
        target = PROJECT_ROOT / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{target.as_posix()}"
    if url.startswith("sqlite:///") and url not in ("sqlite://", "sqlite:///:memory:"):
        Path(url[len("sqlite:///") :]).parent.mkdir(parents=True, exist_ok=True)
    return url


def make_engine(database_url: str | None = None) -> Engine:
    settings = get_settings()
    url = database_url or settings.database_url
    kwargs: dict = {"future": True, "pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        url = resolve_sqlite_url(url)
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        event.listen(engine, "connect", _sqlite_pragmas)
    return engine


def get_engine() -> Engine:
    global _engine, _SessionLocal
    if _engine is None:
        _engine = make_engine()
        _SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)
    return _engine


def configure_engine(engine: Engine) -> None:
    """Replace the process-wide engine (used by tests and the CLI)."""
    global _engine, _SessionLocal
    _engine = engine
    _SessionLocal = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)


def dispose_engine() -> None:
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None


def session_factory() -> sessionmaker[Session]:
    get_engine()
    assert _SessionLocal is not None
    return _SessionLocal


@contextmanager
def session_scope() -> Iterator[Session]:
    """Commit on success, roll back on error."""
    session = session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Generator[Session, None, None]:
    """FastAPI dependency."""
    session = session_factory()()
    try:
        yield session
    finally:
        session.close()


def init_db(engine: Engine | None = None) -> None:
    """Create all tables from the models (tests and quick local start). Production uses Alembic."""
    from app import models  # noqa: F401 - registers models on Base

    models.Base.metadata.create_all(engine or get_engine())
