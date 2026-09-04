from __future__ import annotations

from app.storage.db import _postgres_engine_kwargs
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
