"""Postgres boot upgrades (2026-10-10): the per-symbol open-queue unique index, applied in
its own transaction and never fatal."""

from __future__ import annotations

from contextlib import contextmanager

from app.storage.pg_upgrades import POSTGRES_UPGRADES, apply_postgres_upgrades


class _Database:
    def __init__(self, fail: bool = False):
        self.statements: list[str] = []
        self.transactions = 0
        self.fail = fail

    @contextmanager
    def connect(self):
        self.transactions += 1
        database = self

        class _Connection:
            def execute(self, statement, params=()):
                if database.fail:
                    raise RuntimeError("could not create unique index")
                database.statements.append(statement)

        yield _Connection()


def test_unique_open_queue_index_matches_sqlite() -> None:
    db = _Database()
    assert apply_postgres_upgrades(db) == ["idx_queue_unique_open_per_symbol"]
    (statement,) = db.statements
    assert "CREATE UNIQUE INDEX IF NOT EXISTS idx_queue_unique_open_per_symbol" in statement
    assert "ON execution_queue(symbol) WHERE status IN ('queued','processing')" in statement
    assert db.transactions == len(POSTGRES_UPGRADES)  # one transaction per upgrade


def test_a_failed_upgrade_never_raises() -> None:
    assert apply_postgres_upgrades(_Database(fail=True)) == []
