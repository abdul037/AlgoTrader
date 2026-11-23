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
    assert apply_postgres_upgrades(db) == ["idx_queue_unique_open_per_symbol", "idx_run_logs_event_created"]
    statement = db.statements[0]
    assert "CREATE UNIQUE INDEX IF NOT EXISTS idx_queue_unique_open_per_symbol" in statement
    assert "ON execution_queue(symbol) WHERE status IN ('queued','processing')" in statement
    assert db.transactions == len(POSTGRES_UPGRADES)  # one transaction per upgrade


def test_run_logs_are_indexed_by_event_type_and_time() -> None:
    """2026-11-23: the Live Scorecard reads run_logs by event type; without this index each read
    scanned the whole table and the page's query passed its statement timeout."""

    db = _Database()
    apply_postgres_upgrades(db)
    assert db.statements[1] == (
        "CREATE INDEX IF NOT EXISTS idx_run_logs_event_created ON run_logs(event_type, created_at)"
    )
    # Idempotent and non-unique: safe on a database that already has it (production does).
    assert "IF NOT EXISTS" in db.statements[1] and "UNIQUE" not in db.statements[1]


def test_a_failed_upgrade_never_raises() -> None:
    assert apply_postgres_upgrades(_Database(fail=True)) == []
