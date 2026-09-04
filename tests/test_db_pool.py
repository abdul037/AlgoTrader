from __future__ import annotations

import pytest

from app.storage.db import Database, _postgres_engine_kwargs
from tests.conftest import make_settings


def test_postgres_engine_pool_is_bounded_by_default(tmp_path) -> None:
    # The Postgres pool must stay small so old+new instances during a rolling
    # deploy don't exhaust a small Supabase connection limit (the production
    # ECHECKOUTTIMEOUT that blocked deploys). Defaults cap each instance at ~5.
    kwargs = _postgres_engine_kwargs(make_settings(tmp_path))

    assert kwargs["pool_size"] == 3
    assert kwargs["max_overflow"] == 2  # -> at most 5 connections per instance
    assert kwargs["pool_pre_ping"] is True
    assert kwargs["pool_recycle"] >= 60
    assert kwargs["pool_timeout"] >= 1


def test_postgres_engine_pool_is_env_tunable(tmp_path) -> None:
    settings = make_settings(tmp_path)
    settings.db_pool_size = 8
    settings.db_pool_max_overflow = 4
    settings.db_pool_recycle_seconds = 900

    kwargs = _postgres_engine_kwargs(settings)

    assert kwargs["pool_size"] == 8
    assert kwargs["max_overflow"] == 4
    assert kwargs["pool_recycle"] == 900


def test_postgres_engine_pool_floors_pathological_values(tmp_path) -> None:
    settings = make_settings(tmp_path)
    settings.db_pool_size = -5  # negative -> floored to 1, never a deadlocking pool
    settings.db_pool_recycle_seconds = 1  # too-short -> floored to a sane minimum

    kwargs = _postgres_engine_kwargs(settings)

    assert kwargs["pool_size"] == 1
    assert kwargs["pool_recycle"] == 60


def test_initialize_retries_transient_pool_exhaustion(tmp_path, monkeypatch) -> None:
    # A transiently exhausted Postgres pool at boot (e.g. rolling-deploy overlap)
    # must NOT hard-crash startup: initialize retries until a connection frees.
    settings = make_settings(tmp_path)
    settings.db_connect_max_attempts = 4
    db = Database(settings)
    db.is_sqlite = False  # exercise the Postgres retry path
    monkeypatch.setattr("app.storage.db.time.sleep", lambda _s: None)

    calls = {"n": 0}

    def flaky_create_schema() -> None:
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("ECHECKOUTTIMEOUT")

    monkeypatch.setattr(db, "_create_schema", flaky_create_schema)

    db.initialize()

    assert calls["n"] == 3  # failed twice, succeeded on the third attempt


def test_initialize_reraises_after_max_attempts(tmp_path, monkeypatch) -> None:
    settings = make_settings(tmp_path)
    settings.db_connect_max_attempts = 3
    db = Database(settings)
    db.is_sqlite = False
    monkeypatch.setattr("app.storage.db.time.sleep", lambda _s: None)
    monkeypatch.setattr(
        db, "_create_schema", lambda: (_ for _ in ()).throw(RuntimeError("ECHECKOUTTIMEOUT"))
    )

    with pytest.raises(RuntimeError):
        db.initialize()
