"""Guard against drift between the two schema sources of truth.

The runtime SQLite schema (``app/storage/db.py`` raw DDL) and the Alembic
migrations (used for Postgres) are maintained independently. This test applies
both to scratch SQLite databases and asserts they produce identical tables and
columns, so a change made to one but not the other fails CI instead of silently
diverging in production.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from app.runtime_settings import AppSettings
from app.storage.db import Database

ROOT = Path(__file__).resolve().parents[1]


def _schema(db_path: str) -> dict[str, list[str]]:
    con = sqlite3.connect(db_path)
    try:
        tables = sorted(
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name != 'alembic_version'"
            )
        )
        return {
            table: sorted(row[1] for row in con.execute(f"PRAGMA table_info({table})"))
            for table in tables
        }
    finally:
        con.close()


def _runtime_schema(tmp_path: Path) -> dict[str, list[str]]:
    path = tmp_path / "runtime.db"
    Database(AppSettings(database_url=f"sqlite:///{path}")).initialize()
    return _schema(str(path))


def _alembic_schema(tmp_path: Path) -> dict[str, list[str]]:
    path = tmp_path / "alembic.db"
    env = {**os.environ, "DATABASE_URL": f"sqlite:///{path}"}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(f"alembic upgrade head failed:\n{result.stdout}\n{result.stderr}")
    return _schema(str(path))


def test_runtime_and_migration_tables_match(tmp_path: Path) -> None:
    runtime = _runtime_schema(tmp_path)
    alembic = _alembic_schema(tmp_path)
    only_runtime = sorted(set(runtime) - set(alembic))
    only_alembic = sorted(set(alembic) - set(runtime))
    assert not only_runtime, f"tables in runtime DDL but missing from migrations: {only_runtime}"
    assert not only_alembic, f"tables in migrations but missing from runtime DDL: {only_alembic}"


def test_runtime_and_migration_columns_match(tmp_path: Path) -> None:
    runtime = _runtime_schema(tmp_path)
    alembic = _alembic_schema(tmp_path)
    drift = {
        table: {"runtime": cols, "alembic": alembic.get(table)}
        for table, cols in runtime.items()
        if cols != alembic.get(table)
    }
    assert not drift, f"column drift between runtime DDL and migrations: {drift}"
