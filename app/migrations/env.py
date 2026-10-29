"""Alembic environment.

The database URL comes from app.config.Settings (DATABASE_URL), never from alembic.ini, so one set
of migration files serves SQLite (local runs, tests) and PostgreSQL 16 (Docker). SQLite runs in
batch mode so later ALTER TABLE migrations work there too. Postgres-only objects (the audit_log
append-only trigger) are guarded by dialect inside the revision files.

Overrides, in priority order: `-x url=<sqlalchemy url>` on the command line, a non-empty
`sqlalchemy.url` in the ini / Config object, then DATABASE_URL via Settings.
"""

from __future__ import annotations

import logging
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool
from sqlalchemy.engine import Connection

# Make the project importable whatever the current directory is. alembic.ini's prepend_sys_path
# cannot express a Windows path (it is split on ":" and spaces), so the env does it itself.
_PROJECT_ROOT = str(Path(__file__).resolve().parents[2])
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from app import models  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import resolve_sqlite_url  # noqa: E402

config = context.config

# Only configure logging from the ini when nothing else has (pytest, uvicorn and the CLI configure
# logging themselves; re-configuring would drop their handlers and the PHI-masking filter).
if config.config_file_name is not None and not logging.getLogger().handlers:
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = models.Base.metadata


def get_url() -> str:
    """Resolve the database URL (see module docstring for precedence)."""
    x_args = context.get_x_argument(as_dictionary=True)
    url = (x_args.get("url") or "").strip()
    if not url:
        url = (config.get_main_option("sqlalchemy.url") or "").strip()
    if not url:
        url = get_settings().database_url
    return resolve_sqlite_url(url)


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a live connection (`alembic upgrade head --sql`)."""
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=_is_sqlite(url),
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_with_connection(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=connection.dialect.name == "sqlite",
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations on a live connection.

    A caller may hand in an existing connection via `config.attributes["connection"]` (used by
    tests and programmatic upgrades); otherwise a throw-away NullPool engine is created.
    """
    connection = config.attributes.get("connection")
    if connection is not None:
        _run_with_connection(connection)
        return
    engine = create_engine(get_url(), poolclass=pool.NullPool)
    try:
        with engine.connect() as conn:
            _run_with_connection(conn)
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
