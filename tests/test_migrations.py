"""Alembic migrations (milestone M2): `alembic upgrade head` on a fresh SQLite file matches the models."""

from __future__ import annotations

import argparse
import io

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

from app.config import PROJECT_ROOT, get_settings, reset_settings_cache
from app.models import Base

STRUCTURAL_OPS = {"add_table", "remove_table", "add_column", "remove_column"}


def _config(output_buffer: io.StringIO | None = None) -> Config:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"), output_buffer=output_buffer)
    cfg.set_main_option("script_location", (PROJECT_ROOT / "app" / "migrations").as_posix())
    return cfg


def _op_names(diffs) -> list[str]:
    names: list[str] = []
    for diff in diffs:
        if isinstance(diff, list):
            names.extend(d[0] for d in diff)
        else:
            names.append(diff[0])
    return names


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
        with engine.connect() as conn:
            assert conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == "0001"
            ctx = MigrationContext.configure(conn, opts={"compare_type": True, "render_as_batch": True})
            diffs = compare_metadata(ctx, Base.metadata)
        assert [op for op in _op_names(diffs) if op in STRUCTURAL_OPS] == []
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
