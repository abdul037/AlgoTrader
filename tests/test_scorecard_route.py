"""The Live Scorecard served by the bot (operator 2026-11-07): private key, read-only, one SQL."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import app.dashboard.scorecard as scorecard
from app.main import create_app
from tests.conftest import make_settings


def _client(tmp_path: Path, **values) -> TestClient:
    return TestClient(create_app(settings=make_settings(tmp_path, **values), enable_background_jobs=False))


@pytest.fixture(autouse=True)
def _fresh_cache():
    scorecard._cache.update(at=0.0, body=None)
    yield
    scorecard._cache.update(at=0.0, body=None)


def test_off_without_a_token(tmp_path: Path) -> None:
    client = _client(tmp_path)
    assert client.get("/scorecard").status_code == 404
    assert client.get("/scorecard/data", params={"key": ""}).status_code == 404


def test_the_page_needs_the_right_key(tmp_path: Path) -> None:
    client = _client(tmp_path, scorecard_token="s3cret-key")
    assert client.get("/scorecard", params={"key": "wrong"}).status_code == 404
    assert client.get("/scorecard/data", params={"key": "wrong"}).status_code == 403
    page = client.get("/scorecard", params={"key": "s3cret-key"})
    assert page.status_code == 200 and "AlgoBot Live Scorecard" in page.text
    assert page.text.startswith("<!doctype html>") and page.headers["cache-control"] == "no-store"
    assert "startHosted" in page.text  # the page polls the bot when served here


def test_the_data_needs_postgres(tmp_path: Path) -> None:
    client = _client(tmp_path, scorecard_token="k")
    assert client.get("/scorecard/data", params={"key": "k"}).status_code == 503  # tests run on SQLite


def test_the_sql_is_the_pages_own_query() -> None:
    sql = scorecard.scorecard_sql()
    assert sql.startswith("select json_build_object('now', now()")
    assert "from runtime_state where state_key='etoro_live:scorecard'" in sql
    assert '\\"' not in sql and "like '%\"paper_only\": true%'" in sql  # JS escapes undone
    assert sql.rstrip().endswith("as d;")


class _Conn:
    def __init__(self, calls, row):
        self.calls, self.row = calls, row

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def exec_driver_sql(self, sql):
        self.calls.append(sql)
        return SimpleNamespace(fetchone=lambda: self.row)

    def rollback(self):
        self.calls.append("ROLLBACK")


def test_postgres_runs_read_only_and_caches(tmp_path: Path) -> None:
    app = create_app(settings=make_settings(tmp_path, scorecard_token="k"), enable_background_jobs=False)
    calls: list[str] = []
    app.state.db = SimpleNamespace(is_sqlite=False, _engine=SimpleNamespace(connect=lambda: _Conn(calls, ('{"now": "t"}',))))
    client = TestClient(app)
    first = client.get("/scorecard/data", params={"key": "k"})
    assert first.status_code == 200 and first.json() == {"d": {"now": "t"}}
    assert calls[0] == "SET TRANSACTION READ ONLY" and calls[1].startswith("SET LOCAL statement_timeout")
    assert calls[2] == scorecard.scorecard_sql() and calls[-1] == "ROLLBACK"
    assert client.get("/scorecard/data", params={"key": "k"}).json() == {"d": {"now": "t"}}
    assert len(calls) == 4  # the second read came from the 10-second cache


def test_the_repo_page_matches_the_published_one() -> None:
    page = scorecard.PAGE_PATH.read_text(encoding="utf-8")
    assert "const SQL = " in page and "function startHosted()" in page
