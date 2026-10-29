"""Alembic migrations (milestone M2): `alembic upgrade head` on a fresh SQLite file matches the models.

The Postgres schema the pilot runs on comes from Alembic alone, so the migrated schema must match
``app.models`` in every respect compare_metadata can see: tables, columns, types, nullability,
indexes, unique constraints and foreign keys. A migration that silently dropped
``uq_remittance_file_transaction`` (835 dedup) or a foreign key would fail here.
"""

from __future__ import annotations

import argparse
import io
from typing import Any

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import UniqueConstraint, create_engine, inspect, text

from app.config import PROJECT_ROOT, get_settings, reset_settings_cache
from app.models import Base


def _config(output_buffer: io.StringIO | None = None) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"), output_buffer=output_buffer)
    cfg.set_main_option("script_location", (PROJECT_ROOT / "app" / "migrations").as_posix())
    return cfg


def _flatten(diffs: list[Any]) -> list[tuple[Any, ...]]:
    """compare_metadata groups some column operations in nested lists; one flat list of ops."""
    flat: list[tuple[Any, ...]] = []
    for diff in diffs:
        if isinstance(diff, list):
            flat.extend(tuple(d) for d in diff)
        else:
            flat.append(tuple(diff))
    return flat


def _describe(op: tuple[Any, ...]) -> str:
    """`op_name table[.column]: detail` for a failure message a reader can act on."""
    name, *rest = op
    parts: list[str] = []
    for item in rest:
        if hasattr(item, "name") and getattr(item, "name", None):
            parts.append(str(item.name))
        elif isinstance(item, str | int | bool) or item is None:
            parts.append(repr(item))
        else:
            parts.append(type(item).__name__)
    return f"{name} {' '.join(parts)}"


def _model_unique_sets(table: Any) -> set[tuple[str, ...]]:
    sets = {
        tuple(c.name for c in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    sets |= {(column.name,) for column in table.columns if column.unique}
    return sets


@pytest.fixture()
def fresh_db_url(tmp_path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'migrate.db').as_posix()}"
    monkeypatch.setenv("DATABASE_URL", url)
    reset_settings_cache()
    assert get_settings().database_url == url
    yield url
    reset_settings_cache()


def test_upgrade_head_matches_models(fresh_db_url):
    command.upgrade(_config(), "head")

    engine = create_engine(fresh_db_url)
    try:
        inspector = inspect(engine)
        tables = set(inspector.get_table_names())
        assert tables - {"alembic_version"} == set(Base.metadata.tables)
        for name, table in Base.metadata.tables.items():
            columns = {c["name"]: c for c in inspector.get_columns(name)}
            assert set(columns) == {c.name for c in table.columns}, name
            for column in table.columns:
                assert columns[column.name]["nullable"] == column.nullable, f"{name}.{column.name}"
                assert columns[column.name]["primary_key"] == (1 if column.primary_key else 0), (
                    f"{name}.{column.name}"
                )
            assert {i["name"] for i in inspector.get_indexes(name)} == {i.name for i in table.indexes}, name
            # Unique constraints (as column sets) and foreign keys (referred table + columns) by hand too:
            # the dialect reports them even where compare_metadata has no name to match on.
            migrated_unique = {tuple(c["column_names"]) for c in inspector.get_unique_constraints(name)}
            assert migrated_unique == _model_unique_sets(table), f"{name}: unique constraints"
            migrated_fks = {
                (fk["referred_table"], tuple(fk["constrained_columns"]), tuple(fk["referred_columns"]))
                for fk in inspector.get_foreign_keys(name)
            }
            model_fks = {
                (fk.column.table.name, (fk.parent.name,), (fk.column.name,)) for fk in table.foreign_keys
            }
            assert migrated_fks == model_fks, f"{name}: foreign keys"
        with engine.connect() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0001"
            ctx = MigrationContext.configure(conn, opts={"compare_type": True, "render_as_batch": True})
            diffs = _flatten(compare_metadata(ctx, Base.metadata))
        # Every operation autogenerate would emit counts: a type change, a dropped constraint or
        # foreign key, a nullability change - not only added/removed tables and columns.
        assert diffs == [], "alembic upgrade head differs from app.models:\n" + "\n".join(
            _describe(op) for op in diffs
        )
    finally:
        engine.dispose()


def test_downgrade_base_leaves_only_version_table(fresh_db_url):
    cfg = _config()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    engine = create_engine(fresh_db_url)
    try:
        assert set(inspect(engine).get_table_names()) <= {"alembic_version"}
        with engine.connect() as conn:
            assert conn.execute(text("SELECT count(*) FROM alembic_version")).scalar_one() == 0
    finally:
        engine.dispose()


def test_upgrade_is_repeatable_after_downgrade(fresh_db_url):
    cfg = _config()
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")
    engine = create_engine(fresh_db_url)
    try:
        assert set(inspect(engine).get_table_names()) - {"alembic_version"} == set(Base.metadata.tables)
    finally:
        engine.dispose()


def test_offline_sql_adds_audit_trigger_only_on_postgres():
    buf = io.StringIO()
    cfg = _config(output_buffer=buf)
    cfg.cmd_opts = argparse.Namespace(x=["url=postgresql+psycopg://denial:pw@localhost:5432/denial"])
    command.upgrade(cfg, "head", sql=True)
    sql = buf.getvalue()
    assert "CREATE TABLE audit_log" in sql
    assert "FUNCTION audit_log_immutable()" in sql
    assert "BEFORE UPDATE ON audit_log" in sql and "BEFORE DELETE ON audit_log" in sql
    assert "NUMERIC(12, 2)" in sql

    buf = io.StringIO()
    cfg = _config(output_buffer=buf)
    cfg.cmd_opts = argparse.Namespace(x=["url=sqlite:///offline-only.db"])
    command.upgrade(cfg, "head", sql=True)
    sql = buf.getvalue()
    assert "CREATE TABLE audit_log" in sql
    assert "audit_log_immutable" not in sql

    buf = io.StringIO()
    cfg = _config(output_buffer=buf)
    cfg.cmd_opts = argparse.Namespace(x=["url=postgresql+psycopg://denial:pw@localhost:5432/denial"])
    command.downgrade(cfg, "0001:base", sql=True)
    sql = buf.getvalue()
    assert "DROP TRIGGER IF EXISTS audit_log_no_update" in sql
    assert "DROP FUNCTION IF EXISTS audit_log_immutable" in sql
    assert "DROP TABLE clinics" in sql
