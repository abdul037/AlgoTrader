"""Scan-decision writes: one-round-trip create() and the batched no_signal rows.

Both are pure speed changes, so every test here runs the replaced code (copied
below as the reference oracle) next to the new code and asserts the same rows,
ids, returned records, reads and errors.

Most tests run on raw SQLite and on SQLite behind the SQLAlchemy wrapper that
production Postgres uses. Set TEST_POSTGRES_URL (e.g.
postgresql+psycopg://postgres@127.0.0.1:5432/postgres) to run them against a real
Postgres too; each test works in its own throwaway schema, dropped afterwards.
"""

from __future__ import annotations

import itertools
import json
import os
import sqlite3
import sys
import threading
import uuid
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import numpy as np
import pydantic
import pytest
from sqlalchemy import create_engine, text

import app.utils.time as time_utils
from app.live_signal_schema import MarketQuote
from app.models.screener import ScanDecisionRecord, ScreenerRunResponse
from app.screener import scan_promotion
from app.screener.scan_promotion import _weak_valid_count_rows, _weak_valid_daily_count
from app.screener.service import MarketScreenerService
from app.storage import scan_decisions as scan_decisions_module
from app.storage.db import Database, _postgres_engine_kwargs, _SqlAlchemyConnection
from app.storage.repositories import ScanDecisionRepository
from tests.conftest import make_settings
from tests.test_screener_service import (
    FakeBacktestRepository,
    FakeMarketDataEngine,
    FakeRunLogRepository,
    FakeSignalStateRepository,
    FakeTelegramNotifier,
    _frame,
)

_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL", "")
PATHS = [
    "sqlite3",
    "sqlalchemy",
    pytest.param(
        "postgres",
        marks=pytest.mark.skipif(not _POSTGRES_URL, reason="set TEST_POSTGRES_URL to run against a real Postgres"),
    ),
]
_WAIT = 10.0  # seconds; only reached when a thread is wrongly blocked


# --------------------------------------------------------------------------- #
# Reference oracle: the create() this change replaced, verbatim (INSERT, then a
# SELECT of the new row through get()).
# --------------------------------------------------------------------------- #
def _legacy_create(
    repository: ScanDecisionRepository,
    *,
    scan_task: str,
    symbol: str,
    strategy_name: str,
    timeframe: str,
    status: str,
    final_score: float | None,
    alert_eligible: bool,
    freshness: str | None,
    reason_codes: list[str],
    rejection_reasons: list[str],
    payload: dict[str, Any] | None = None,
) -> ScanDecisionRecord:
    created_at = scan_decisions_module.utc_now().isoformat()
    with repository.db.connect() as connection:
        cursor = connection.execute(
            """
            INSERT INTO scan_decisions (
                scan_task, symbol, strategy_name, timeframe, status, final_score,
                alert_eligible, freshness, reason_codes_json, rejection_reasons_json,
                payload_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                scan_task,
                symbol.upper(),
                strategy_name,
                timeframe,
                status,
                final_score,
                1 if alert_eligible else 0,
                freshness,
                json.dumps(reason_codes),
                json.dumps(rejection_reasons),
                json.dumps(payload or {}),
                created_at,
            ),
        )
        record_id = int(cursor.lastrowid)
    with repository.db.connect() as connection:
        row = connection.execute("SELECT * FROM scan_decisions WHERE id = ?", (record_id,)).fetchone()
    if row is None:
        raise KeyError(f"Scan decision {record_id} not found")
    return ScanDecisionRepository._row_to_model(row)


class LegacyScanDecisionRepository(ScanDecisionRepository):
    """Every write is the old immediate INSERT + SELECT; nothing is ever queued."""

    def create(self, **fields: Any) -> ScanDecisionRecord:
        return _legacy_create(self, **fields)

    def create_deferred(self, **fields: Any) -> None:
        _legacy_create(self, **fields)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
class _WrappedSqliteDatabase(Database):
    """SQLite behind the SQLAlchemy connection wrapper that production Postgres uses
    (``?`` -> named binds, the RETURNING handling, mapping rows)."""

    def __init__(self, settings: Any):
        super().__init__(settings)
        self._wrapped_engine = create_engine(self.url)

    @contextmanager
    def connect(self):
        with self._wrapped_engine.begin() as connection:
            yield _SqlAlchemyConnection(connection)


_POSTGRES_SCHEMAS: list[tuple[Database, str]] = []


class _PostgresSchemaDatabase(Database):
    """The production Postgres path (engine options included) in a throwaway schema."""

    def __init__(self, settings: Any, schema: str):
        super().__init__(settings)
        kwargs = _postgres_engine_kwargs(settings)
        connect_args = dict(kwargs["connect_args"])
        connect_args["options"] = f"{connect_args['options']} -c search_path={schema}"
        kwargs["connect_args"] = connect_args
        assert self._engine is not None
        self._engine.dispose()
        self._engine = create_engine(self.url, **kwargs)


@pytest.fixture(autouse=True)
def _drop_postgres_schemas():
    yield
    while _POSTGRES_SCHEMAS:
        db, schema = _POSTGRES_SCHEMAS.pop()
        assert db._engine is not None
        db._engine.dispose()
        admin = create_engine(_POSTGRES_URL)
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


def _database(directory, path: str) -> Database:
    directory.mkdir(parents=True, exist_ok=True)
    if path == "postgres":
        schema = f"scan_decisions_test_{uuid.uuid4().hex[:12]}"
        admin = create_engine(_POSTGRES_URL)
        with admin.begin() as connection:
            connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        admin.dispose()
        db: Database = _PostgresSchemaDatabase(make_settings(directory, database_url=_POSTGRES_URL), schema)
        _POSTGRES_SCHEMAS.append((db, schema))
        db.initialize()
        return db
    settings = make_settings(directory)
    Database(settings).initialize()
    return _WrappedSqliteDatabase(settings) if path == "sqlalchemy" else Database(settings)


class _StatementLog:
    """Record every statement a Database runs (whitespace-normalised), with its thread.

    ``before_connect[thread name]`` runs as that thread opens a connection, and an
    INSERT run by a thread in ``failing_inserts`` raises after it is recorded.
    """

    def __init__(self, db: Database):
        self.statements: list[str] = []
        self.by_thread: list[tuple[str, str]] = []
        self.before_connect: dict[str, Any] = {}
        self.failing_inserts: set[str] = set()
        original = db.connect
        log = self

        class _Recording:
            def __init__(self, connection: Any):
                self._connection = connection

            def execute(self, sql: str, params: Any = ()) -> Any:
                statement = " ".join(sql.split())
                thread = threading.current_thread().name
                log.statements.append(statement)
                log.by_thread.append((thread, statement))
                if thread in log.failing_inserts and statement.startswith("INSERT"):
                    raise sqlite3.OperationalError("database is locked")
                return self._connection.execute(sql, params)

        @contextmanager
        def connect():
            hook = log.before_connect.get(threading.current_thread().name)
            if hook is not None:
                hook()
            with original() as connection:
                yield _Recording(connection)

        db.connect = connect  # type: ignore[method-assign]

    def thread_statements(self, thread: str) -> list[str]:
        return [statement for name, statement in self.by_thread if name == thread]


class _Clock:
    """A ticking utc_now for every app module, so two runs stamp identical times."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch):
        self.start = datetime(2026, 4, 13, 14, 30, 0, 123456, tzinfo=UTC)
        self.reset()
        self._lock = threading.Lock()
        original = time_utils.utc_now
        for name, module in list(sys.modules.items()):
            if (name == "app" or name.startswith("app.")) and getattr(module, "utc_now", None) is original:
                monkeypatch.setattr(module, "utc_now", self)

    def reset(self) -> None:
        self._ticks = itertools.count()

    def __call__(self) -> datetime:
        with self._lock:
            return self.start + timedelta(milliseconds=next(self._ticks))


def _canonical(value: Any) -> Any:
    """Exact, NaN-safe form: every leaf as (type, repr), so 5 != 5.0 and nan == nan."""

    if isinstance(value, ScanDecisionRecord):
        return _canonical({name: getattr(value, name) for name in ScanDecisionRecord.model_fields})
    if isinstance(value, dict):
        return {repr(key): _canonical(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return (type(value).__name__, repr(value))


def _table(db: Database) -> list[Any]:
    with db.connect() as connection:
        rows = connection.execute("SELECT * FROM scan_decisions ORDER BY id").fetchall()
    return [_canonical(dict(row)) for row in rows]


def _raw_count(db: Database) -> int:
    """Rows committed to the database, read past the repository (no flush, no hooks)."""

    if db.is_sqlite:
        connection = sqlite3.connect(db.path)
        try:
            return int(connection.execute("SELECT COUNT(*) FROM scan_decisions").fetchone()[0])
        finally:
            connection.close()
    assert db._engine is not None
    with db._engine.connect() as connection:
        return int(connection.execute(text("SELECT COUNT(*) FROM scan_decisions")).scalar_one())


def _outcome(call: Any) -> tuple[str, Any]:
    try:
        return ("ok", call())
    except Exception as exc:  # noqa: BLE001 - compared against the oracle's outcome
        return ("raised", type(exc).__name__)


def _fields(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "scan_task": "swing_scan",
        "symbol": "nvda",
        "strategy_name": "momentum_breakout",
        "timeframe": "1d",
        "status": "no_signal",
        "final_score": None,
        "alert_eligible": False,
        "freshness": None,
        "reason_codes": ["no_strategy_signal"],
        "rejection_reasons": ["no_strategy_signal"],
        "payload": {"market_data_status": {"verified": True, "quote_age_seconds": 30.0}},
    }
    fields.update(overrides)
    return fields


CREATE_CASES: list[dict[str, Any]] = [
    _fields(),
    _fields(
        symbol="brk.b",
        strategy_name="rsi_vwap_ema_confluence",
        status="candidate",
        final_score=71.25,
        alert_eligible=True,
        freshness="fresh",
        reason_codes=["market_data_verified", "café ✓ 日本 🚀"],
        rejection_reasons=[],
        payload={
            "metadata": {"source": "supervised_weak_valid", "supervised_weak_valid_promoted_to_candidate": True},
            "note": 'Zürich – “quotes” \\ back\\slash \u0000 nul \n newline',
            "nested": {"a": [1, 2.5, None, True, {"b": [], "c": {"d": "é"}}]},
        },
    ),
    _fields(
        status="rejected",
        final_score=float("nan"),
        payload={"score": float("nan"), "upper": float("inf"), "lower": float("-inf"), "none": None},
    ),
    _fields(final_score=5, payload=None),
    _fields(final_score=np.float64(0.1), payload={"tuple": (1, 2), 3: "int key", "big": 2**70, "empty": {}}),
    _fields(
        timeframe="1h",
        status="suppressed",
        final_score=float("inf"),
        alert_eligible=1,
        freshness="repeated_upgraded",
        reason_codes=[],
        rejection_reasons=["recent_alert_without_material_score_improvement"] * 40,
        payload={"long": "x" * 20000, "negative_zero": -0.0, "tiny": 5e-324},
    ),
    _fields(final_score=-0.0, freshness="", reason_codes=["Ω"], payload={}),
    _fields(final_score=1e308, payload={"list": [[[]]], "false": False, "zero": 0}),
]


def _storable_cases(path: str) -> list[dict[str, Any]]:
    """CREATE_CASES that both paths store: Postgres REAL (float4) rejects 1e308 on
    the old and the new path alike, which the P2 test checks."""

    return [case for case in CREATE_CASES if not (path == "postgres" and case["final_score"] == 1e308)]


# --------------------------------------------------------------------------- #
# P2: create() returns the stored row without a follow-up SELECT
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", PATHS)
def test_create_returns_the_stored_row_without_a_follow_up_select(tmp_path, monkeypatch, path) -> None:
    clock = _Clock(monkeypatch)
    legacy = LegacyScanDecisionRepository(_database(tmp_path / "legacy", path))
    new_db = _database(tmp_path / "new", path)
    repository = ScanDecisionRepository(new_db)
    log = _StatementLog(new_db)

    stored = 0
    for case in CREATE_CASES:
        clock.reset()
        legacy_outcome = _outcome(lambda case=case: _canonical(legacy.create(**case)))
        clock.reset()
        before = len(log.statements)
        outcome = _outcome(lambda case=case: _canonical(repository.create(**case)))

        # One statement, no re-read.
        statements = log.statements[before:]
        assert len(statements) == 1
        assert statements[0].startswith("INSERT INTO scan_decisions")
        assert statements[0].endswith("RETURNING *")
        # Same record the old INSERT + SELECT returned (id, created_at, payload, types),
        # or the same error...
        assert outcome == legacy_outcome
        if outcome[0] == "ok":
            stored += 1
            # ...and the same as a fresh read of the row.
            assert outcome[1] == _canonical(repository.get(int(outcome[1]["'id'"][1])))

    assert stored >= len(CREATE_CASES) - 1
    assert _table(new_db) == _table(legacy.db)


# --------------------------------------------------------------------------- #
# P6: no_signal rows are queued for the scan and written once per symbol
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", PATHS)
def test_deferred_rows_and_ids_match_one_at_a_time_writes(tmp_path, monkeypatch, path) -> None:
    clock = _Clock(monkeypatch)
    calls: list[tuple[str, dict[str, Any]]] = []
    for index, case in enumerate(_storable_cases(path) * 3):
        symbol = ["aaa", "AAA", "bbb", "bbb", "ccc"][index % 5]  # runs of one symbol, then the next
        calls.append(("create" if index % 7 == 3 else "create_deferred", {**case, "symbol": symbol}))

    def run(repository: ScanDecisionRepository) -> list[Any]:
        clock.reset()
        returned = []
        with repository.deferred_writes():
            for method, fields in calls:
                result = getattr(repository, method)(**fields)
                returned.append(None if result is None else _canonical(result))
        return returned

    legacy = LegacyScanDecisionRepository(_database(tmp_path / "legacy", path))
    repository = ScanDecisionRepository(_database(tmp_path / "new", path))
    legacy_returned = run(legacy)
    returned = run(repository)

    assert _table(repository.db) == _table(legacy.db)
    # Immediate creates return exactly the old record, ids included.
    for (method, _fields_used), old, new in zip(calls, legacy_returned, returned, strict=True):
        if method == "create":
            assert new == old


def test_create_deferred_outside_a_scan_writes_at_once(tmp_path, monkeypatch) -> None:
    clock = _Clock(monkeypatch)
    legacy = LegacyScanDecisionRepository(_database(tmp_path / "legacy", "sqlite3"))
    repository = ScanDecisionRepository(_database(tmp_path / "new", "sqlite3"))
    log = _StatementLog(repository.db)
    for target in (legacy, repository):
        clock.reset()
        for index, case in enumerate(CREATE_CASES):
            assert target.create_deferred(**case) is None
            assert _raw_count(target.db) == index + 1  # written by the call, nothing queued
    assert len(log.statements) == len(CREATE_CASES)
    assert _table(repository.db) == _table(legacy.db)


def test_every_read_in_the_scan_sees_rows_queued_before_it(tmp_path, monkeypatch) -> None:
    _Clock(monkeypatch)
    db = _database(tmp_path, "sqlite3")
    repository = ScanDecisionRepository(db)

    with repository.deferred_writes():
        repository.create_deferred(**_fields(strategy_name="a"))
        repository.create_deferred(**_fields(strategy_name="b"))
        assert _raw_count(db) == 0  # queued, not yet written
        assert [item.strategy_name for item in repository.list(limit=10)] == ["b", "a"]
        assert _raw_count(db) == 2

        repository.create_deferred(**_fields(strategy_name="c", status="watchlist"))
        latest = repository.get_latest(symbol="NVDA", strategy_name="c", timeframe="1d", since_minutes=30)
        assert latest is not None and latest.status == "watchlist"

        repository.create_deferred(**_fields(strategy_name="d"))
        assert repository.get(4).strategy_name == "d"

        repository.create_deferred(**_fields(strategy_name="e"))
        record = repository.create(**_fields(strategy_name="f"))
        assert record.id == 6  # the queued row was written first, keeping call order
        assert _raw_count(db) == 6


def test_queued_rows_are_written_when_the_next_symbol_starts(tmp_path, monkeypatch) -> None:
    _Clock(monkeypatch)
    db = _database(tmp_path, "sqlite3")
    repository = ScanDecisionRepository(db)
    log = _StatementLog(db)

    with repository.deferred_writes():
        for name in ("a", "b", "c"):
            repository.create_deferred(**_fields(symbol="aaa", strategy_name=name))
        assert _raw_count(db) == 0
        repository.create_deferred(**_fields(symbol="AAA", strategy_name="d"))  # same symbol, any case
        assert _raw_count(db) == 0
        repository.create_deferred(**_fields(symbol="bbb", strategy_name="a"))
        assert _raw_count(db) == 4  # AAA's four rows, in one statement
        assert len(log.statements) == 1
        assert log.statements[0].count("(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)") == 4
        repository.flush()
        repository.flush()  # nothing queued: no statement
        assert _raw_count(db) == 5
        assert len(log.statements) == 2
    assert len(log.statements) == 2  # the scan's end had nothing left to write


@pytest.mark.parametrize("path", PATHS)
def test_a_large_queue_is_written_in_bounded_chunks(tmp_path, monkeypatch, path) -> None:
    clock = _Clock(monkeypatch)
    legacy = LegacyScanDecisionRepository(_database(tmp_path / "legacy", path))
    repository = ScanDecisionRepository(_database(tmp_path / "new", path))
    log = _StatementLog(repository.db)
    for target in (legacy, repository):
        clock.reset()
        with target.deferred_writes():
            for index in range(203):
                target.create_deferred(**_fields(strategy_name=f"spec_{index}", payload={"i": index}))
    assert _raw_count(repository.db) == 203
    assert [statement.count("(?, ?, ?") for statement in log.statements] == [80, 80, 43]
    assert _table(repository.db) == _table(legacy.db)


def test_create_still_raises_before_writing_an_unserialisable_payload(tmp_path, monkeypatch) -> None:
    _Clock(monkeypatch)
    db = _database(tmp_path, "sqlite3")
    repository = ScanDecisionRepository(db)
    with pytest.raises(TypeError):
        repository.create(**_fields(payload={"bad": object()}))
    with repository.deferred_writes(), pytest.raises(TypeError):
        repository.create_deferred(**_fields(payload={"bad": object()}))
    assert _raw_count(db) == 0


def test_payload_is_serialised_when_queued_not_when_written(tmp_path, monkeypatch) -> None:
    _Clock(monkeypatch)
    repository = ScanDecisionRepository(_database(tmp_path, "sqlite3"))
    payload: dict[str, Any] = {"market_data_status": {"verified": True}}
    with repository.deferred_writes():
        repository.create_deferred(**_fields(payload=payload))
        payload["market_data_status"]["verified"] = False  # the scan reuses and changes dicts
    assert repository.list()[0].payload == {"market_data_status": {"verified": True}}


def test_a_stored_row_the_record_rejects_still_raises_in_the_scan(tmp_path, monkeypatch) -> None:
    """The old create() re-read every row through ScanDecisionRecord; a queued row is
    parsed from what the database stored, so the same value still raises (row stored)."""

    clock = _Clock(monkeypatch)
    legacy = LegacyScanDecisionRepository(_database(tmp_path / "legacy", "sqlite3"))
    repository = ScanDecisionRepository(_database(tmp_path / "new", "sqlite3"))
    bad = _fields(final_score=np.float32(0.5))  # SQLite stores a numpy float32 as a BLOB
    clock.reset()
    with pytest.raises(pydantic.ValidationError):
        legacy.create(**bad)
    clock.reset()
    with pytest.raises(pydantic.ValidationError), repository.deferred_writes():
        repository.create_deferred(**bad)  # queued without error...
    # ...and raised by the scan's write, with the row stored as before.
    assert _raw_count(repository.db) == 1
    assert _table(repository.db) == _table(legacy.db)


def test_an_old_sqlite_is_refused_with_a_clear_message(tmp_path, monkeypatch) -> None:
    db = _database(tmp_path, "sqlite3")
    monkeypatch.setattr(scan_decisions_module.sqlite3, "sqlite_version_info", (3, 31, 1))
    with pytest.raises(RuntimeError, match=r"SQLite 3\.35\+"):
        ScanDecisionRepository(db)


def test_moved_repository_is_still_importable_from_repositories() -> None:
    assert ScanDecisionRepository is scan_decisions_module.ScanDecisionRepository


# --------------------------------------------------------------------------- #
# A failed write raises in the scan that queued the rows, and never loses them
# silently
# --------------------------------------------------------------------------- #
@contextmanager
def _broken_connections(db: Database, *, times: int | None = None):
    """The next ``times`` connections (all, if None) fail as a database outage would."""

    original = db.connect
    remaining = {"n": times}

    @contextmanager
    def connect():
        if remaining["n"] is None or remaining["n"] > 0:
            if remaining["n"] is not None:
                remaining["n"] -= 1
            raise sqlite3.OperationalError("database is locked")
        with original() as connection:
            yield connection

    db.connect = connect  # type: ignore[method-assign]
    try:
        yield
    finally:
        db.connect = original  # type: ignore[method-assign]


def test_a_failed_write_keeps_the_rows_for_the_scans_next_write(tmp_path, monkeypatch) -> None:
    clock = _Clock(monkeypatch)
    legacy = LegacyScanDecisionRepository(_database(tmp_path / "legacy", "sqlite3"))
    repository = ScanDecisionRepository(_database(tmp_path / "new", "sqlite3"))
    clock.reset()
    for name in ("a", "b", "c"):
        legacy.create_deferred(**_fields(strategy_name=name))

    clock.reset()
    with repository.deferred_writes():
        for name in ("a", "b", "c"):
            repository.create_deferred(**_fields(strategy_name=name))
        with _broken_connections(repository.db, times=1):
            # A reader in the scan that never raises (the weak-valid daily count, which
            # fails closed to the daily limit) triggers the write, which fails: nothing
            # raised, nothing written...
            assert _weak_valid_daily_count(SimpleNamespace(scan_decisions=repository)) == 4
        assert _raw_count(repository.db) == 0
    # ...but the rows stayed queued, and the scan's own next write stored them.
    assert _table(repository.db) == _table(legacy.db)


def test_a_write_that_keeps_failing_raises_in_the_scan_and_ends_with_it(tmp_path, monkeypatch) -> None:
    _Clock(monkeypatch)
    db = _database(tmp_path, "sqlite3")
    repository = ScanDecisionRepository(db)
    service = _scan_service(tmp_path, repository, _RecordingLearning())
    finished = ScreenerRunResponse(
        generated_at="2026-04-13T14:30:00+00:00",
        universe_name="test",
        timeframes=["1d"],
        evaluated_symbols=1,
        evaluated_strategy_runs=1,
    )

    def scan_whose_writes_fail(scan_service, **_kwargs):
        decisions = scan_service.scan_decisions
        decisions.create_deferred(**_fields(symbol="AAPL", strategy_name="a"))
        decisions.create_deferred(**_fields(symbol="AAPL", strategy_name="b"))
        with suppress(Exception):  # a reader that swallows the failed write
            decisions.list(limit=5000)
        return finished  # the scan itself completes

    monkeypatch.setattr("app.screener.service.scan_universe", scan_whose_writes_fail)
    with _broken_connections(db), pytest.raises(sqlite3.OperationalError):
        service.scan_universe(scan_task="premarket_scan")  # the scan's last write raised
    assert _raw_count(db) == 0

    # The failed rows ended with their scan: the next scan in this thread writes only its own.
    def scan_that_works(scan_service, **_kwargs):
        scan_service.scan_decisions.create_deferred(**_fields(symbol="MSFT", strategy_name="c"))
        return finished

    monkeypatch.setattr("app.screener.service.scan_universe", scan_that_works)
    assert service.scan_universe(scan_task="premarket_scan") is finished
    assert [(item.symbol, item.strategy_name) for item in repository.list()] == [("MSFT", "c")]


def test_a_scan_that_raises_still_writes_its_queue_and_keeps_its_error(tmp_path, monkeypatch) -> None:
    _Clock(monkeypatch)
    db = _database(tmp_path, "sqlite3")
    repository = ScanDecisionRepository(db)
    service = _scan_service(tmp_path, repository, _RecordingLearning())

    def failing_scan(scan_service, **_kwargs):
        scan_service.scan_decisions.create_deferred(**_fields())
        raise RuntimeError("scan blew up")

    monkeypatch.setattr("app.screener.service.scan_universe", failing_scan)
    with pytest.raises(RuntimeError, match="scan blew up"):
        service.scan_universe(scan_task="premarket_scan")
    assert _raw_count(db) == 1

    # A write that fails as well never replaces the scan's own error.
    with _broken_connections(db), pytest.raises(RuntimeError, match="scan blew up"):
        service.scan_universe(scan_task="premarket_scan")
    assert _raw_count(db) == 1


# --------------------------------------------------------------------------- #
# Each scan's queue belongs to its own thread
# --------------------------------------------------------------------------- #
def _run_threads(*targets: tuple[str, Any]) -> dict[str, BaseException]:
    errors: dict[str, BaseException] = {}

    def wrap(name: str, target: Any):
        def run() -> None:
            try:
                target()
            except BaseException as exc:  # noqa: BLE001 - reported to the test
                errors[name] = exc

        return run

    threads = [threading.Thread(target=wrap(name, target), name=name) for name, target in targets]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3 * _WAIT)
        assert not thread.is_alive(), f"{thread.name} is stuck"
    return errors


def test_two_scans_in_two_threads_write_and_fail_only_their_own_rows(tmp_path, monkeypatch) -> None:
    _Clock(monkeypatch)
    db = _database(tmp_path, "sqlite3")
    repository = ScanDecisionRepository(db)
    log = _StatementLog(db)
    log.failing_inserts.add("scan-b")  # scan B's writes fail; scan A's work
    a_queued, b_done = threading.Event(), threading.Event()
    waited: list[bool] = []

    def scan_a() -> None:
        with repository.deferred_writes():
            repository.create_deferred(**_fields(symbol="AAPL", strategy_name="a0"))
            repository.create_deferred(**_fields(symbol="AAPL", strategy_name="a1"))
            a_queued.set()
            waited.append(b_done.wait(_WAIT))
            repository.create_deferred(**_fields(symbol="AAPL", strategy_name="a2"))

    def scan_b() -> None:
        waited.append(a_queued.wait(_WAIT))
        try:
            with repository.deferred_writes():
                repository.create_deferred(**_fields(symbol="MSFT", strategy_name="b0"))
        finally:
            b_done.set()

    errors = _run_threads(("scan-a", scan_a), ("scan-b", scan_b))

    assert waited == [True, True]
    assert "scan-a" not in errors  # A never sees B's failure...
    assert isinstance(errors.get("scan-b"), sqlite3.OperationalError)  # ...B gets its own
    # B only ever tried to write its own row; A wrote its three rows itself, in one
    # statement.
    assert [statement.count("(?, ?, ?") for statement in log.thread_statements("scan-b")] == [1]
    assert [statement.count("(?, ?, ?") for statement in log.thread_statements("scan-a")] == [3]
    assert [(item.id, item.strategy_name) for item in reversed(repository.list())] == [
        (1, "a0"),
        (2, "a1"),
        (3, "a2"),
    ]


def test_a_reader_in_another_thread_never_writes_or_waits_for_a_scans_rows(tmp_path, monkeypatch) -> None:
    clock = _Clock(monkeypatch)
    legacy = LegacyScanDecisionRepository(_database(tmp_path / "legacy", "sqlite3"))
    db = _database(tmp_path / "new", "sqlite3")
    repository = ScanDecisionRepository(db)
    log = _StatementLog(db)
    queued, first_read_done, reader_connecting, scan_done = (threading.Event() for _ in range(4))
    waited: list[bool] = []
    reads: list[list[str]] = []
    names = [f"s{index}" for index in range(5)]

    def scan() -> None:
        with repository.deferred_writes():
            for name in names[:3]:
                repository.create_deferred(**_fields(strategy_name=name))
            queued.set()
            waited.append(first_read_done.wait(_WAIT))
            waited.append(reader_connecting.wait(_WAIT))
            # The reader is now inside its read; the scan keeps queueing and writes
            # without waiting for it.
            for name in names[3:]:
                repository.create_deferred(**_fields(strategy_name=name))
        scan_done.set()

    def reader() -> None:
        waited.append(queued.wait(_WAIT))
        reads.append([item.strategy_name for item in repository.list(limit=50)])
        first_read_done.set()

        def hold_the_connection() -> None:
            reader_connecting.set()
            waited.append(scan_done.wait(_WAIT))

        log.before_connect["reader"] = hold_the_connection
        reads.append([item.strategy_name for item in repository.list(limit=50)])

    clock.reset()
    errors = _run_threads(("scan", scan), ("reader", reader))
    clock.reset()
    with legacy.deferred_writes():
        for name in names:
            legacy.create_deferred(**_fields(strategy_name=name))

    assert errors == {}
    assert waited == [True, True, True, True]  # nobody waited out a timeout
    # The reader only read: it saw the scan's rows once the scan wrote them.
    assert all(statement.startswith("SELECT") for statement in log.thread_statements("reader"))
    assert reads == [[], list(reversed(names))]
    # Neither lost nor reordered: the same rows and ids as one-at-a-time writes.
    assert _table(db) == _table(legacy.db)


# --------------------------------------------------------------------------- #
# P6 end to end: a whole scan writes and reads exactly what it did before
# --------------------------------------------------------------------------- #
class _RecordingLearning:
    """Learning double: records what capture_scan_decision received (ids included)."""

    def __init__(self) -> None:
        self.captured: list[Any] = []

    def capture_scan_decision(self, record: Any, *, signal: Any | None = None) -> None:
        self.captured.append(_canonical(record))

    def apply_to_snapshot(self, snapshot: Any) -> Any:
        return snapshot


def _recording_reads(repository: ScanDecisionRepository, reads: list[Any]) -> None:
    for name in ("list", "get_latest", "get"):
        original = getattr(repository, name)

        def wrapper(*args: Any, __original=original, __name=name, **kwargs: Any) -> Any:
            result = __original(*args, **kwargs)
            shown = [result] if not isinstance(result, list) else result
            reads.append((__name, repr(sorted(kwargs.items())), [None if r is None else _canonical(r) for r in shown]))
            return result

        setattr(repository, name, wrapper)


def _recording_count_reads(monkeypatch: pytest.MonkeyPatch, reads: list[Any]) -> None:
    """Record the weak-valid daily count's read as what the count can use.

    On the real repository the count reads today's candidate rows past list(); the
    oracle class still calls list(limit=5000), which here holds every row. Both runs
    record one entry: the rows whose status lowers to "candidate" and whose created_at
    is today or later, with every field, so rows, ids and order still compare.
    """

    def wrapper(repository: Any, today: Any) -> Any:
        mark = len(reads)
        rows = _weak_valid_count_rows(repository, today)
        del reads[mark:]  # the oracle's list(limit=5000) inside it is this same read
        usable = [r for r in rows if r.status.lower() == "candidate" and r.created_at >= today.isoformat()]
        reads.append(("weak_valid_count_rows", "", [_canonical(item) for item in usable]))
        return rows

    monkeypatch.setattr(scan_promotion, "_weak_valid_count_rows", wrapper)


def _scan_service(
    tmp_path, repository: ScanDecisionRepository, learning: _RecordingLearning, **settings: Any
) -> MarketScreenerService:
    trend = _frame([100 + (index * 0.8) for index in range(120)] + [198, 202, 206, 211, 218])
    trend.loc[trend.index[-1], "volume"] = trend["volume"].tail(20).mean() * 2.0
    hourly = _frame([50 + (index * 0.4) for index in range(80)], step="1h")
    flat = _frame([100.0 + (0.05 if index % 2 else 0.0) for index in range(125)])
    frames = {
        ("NVDA", "1d"): trend,
        ("NVDA", "1h"): hourly,
        ("FLAT", "1d"): flat,
        ("FLAT", "1h"): flat,
    }
    quotes = {
        "NVDA": MarketQuote(symbol="NVDA", bid=218.0, ask=218.1, last_execution=218.05, timestamp="2026-04-11T10:00:00Z"),
        "FLAT": MarketQuote(symbol="FLAT", bid=100.0, ask=100.1, last_execution=100.05, timestamp="2026-04-11T10:00:00Z"),
    }
    backtest_summary = {
        "strategy_name": "momentum_breakout",
        "completed_at": "2026-04-10T12:00:00Z",
        "out_of_sample": True,
        "metrics": {
            "number_of_trades": 28,
            "profit_factor": 1.9,
            "annualized_return_pct": 18.5,
            "max_drawdown_pct": 16.0,
            "win_rate": 58.0,
            "out_of_sample": True,
        },
        "trades": [{"pnl_usd": 120.0, "pnl_pct": 1.4}, {"pnl_usd": -45.0, "pnl_pct": -0.5}],
    }
    return MarketScreenerService(
        settings=make_settings(
            tmp_path,
            market_universe_symbols=["NVDA", "FLAT"],
            screener_default_timeframes=["1d", "1h"],
            screener_active_strategy_names=["all"],
            screener_spec_coverage_mode="scheduled_all",
            screener_min_confidence=0.4,
            screener_min_score_improvement_for_repeat=5.0,
            screener_max_extension_atr_multiple=10.0,
            **settings,
        ),
        market_data_engine=FakeMarketDataEngine(frames, quotes),
        signal_state_repository=FakeSignalStateRepository(),
        run_log_repository=FakeRunLogRepository(),
        backtest_repository=FakeBacktestRepository(summary=backtest_summary),
        scan_decision_repository=repository,
        telegram_notifier=FakeTelegramNotifier(),
        learning_service=learning,
    )


@pytest.mark.parametrize("path", PATHS)
def test_whole_scans_write_and_read_exactly_as_before(tmp_path, monkeypatch, path) -> None:
    clock = _Clock(monkeypatch)

    def run(name: str, repository_class: type[ScanDecisionRepository]) -> dict[str, Any]:
        clock.reset()
        db = _database(tmp_path / name, path)
        repository = repository_class(db)
        log = _StatementLog(db)
        reads: list[Any] = []
        _recording_reads(repository, reads)
        _recording_count_reads(monkeypatch, reads)
        learning = _RecordingLearning()
        # Market data is fetched in the scan's own thread (no timeout worker), so a
        # read made as each fetch starts is a read in the middle of the scan.
        service = _scan_service(tmp_path / name, repository, learning, screener_market_data_timeout_seconds=0.0)
        fetches: list[Any] = []
        get_history = service.market_data.get_history

        scan_thread = threading.current_thread()

        def get_history_reading_mid_scan(symbol: str, *args: Any, **kwargs: Any) -> Any:
            if threading.current_thread() is not scan_thread:  # e.g. intelligence's timeout worker
                return get_history(symbol, *args, **kwargs)
            # Rows written when the scan starts a symbol (its first timeframe's fetch),
            # then a read whose result depends on the rows queued just before it.
            starts_symbol = symbol in ("NVDA", "FLAT") and kwargs.get("timeframe") == "1d"
            committed = _raw_count(db) if starts_symbol else None
            window = [_canonical(item) for item in repository.list(limit=7)]
            fetches.append((symbol, kwargs.get("timeframe"), committed, window))
            return get_history(symbol, *args, **kwargs)

        service.market_data.get_history = get_history_reading_mid_scan
        responses = [
            service.scan_universe(scan_task="premarket_scan").model_dump_json(),
            service.scan_universe(scan_task="premarket_scan").model_dump_json(),
            service.scan_universe(symbols=["FLAT", "NVDA"], scan_task="manual_scan").model_dump_json(),
        ]
        inserts = [statement for statement in log.statements if statement.startswith("INSERT")]
        return {
            "statements": len(log.statements),
            "table": _table(db),
            "reads": reads,
            "fetches": fetches,
            "captured": learning.captured,
            "responses": responses,
            "signal_states": [item.model_dump_json() for item in service.signal_states.items],
            "run_logs": json.dumps(service.logs.items, sort_keys=True, default=repr),
            "inserts": len(inserts),
            "rows_per_insert": max(statement.count("(?, ?, ?") for statement in inserts),
        }

    legacy = run("legacy", LegacyScanDecisionRepository)
    new = run("new", ScanDecisionRepository)

    statuses = {row["'status'"][1] for row in new["table"]}
    assert {"'no_signal'", "'candidate'"} <= statuses  # the scenario hits both kinds of write
    assert any(name == "get_latest" for name, _, _ in new["reads"])
    count_reads = [rows for name, _, rows in new["reads"] if name == "weak_valid_count_rows"]
    assert len(count_reads) == 3 and count_reads[-1]  # one per scan; later scans see earlier candidates
    assert len(new["table"]) > 100
    starts = [committed for _, _, committed, _ in new["fetches"] if committed is not None]
    assert len(starts) == 6 and starts[0] == 0 and all(starts[1:])  # 3 scans x 2 symbols
    for key in ("table", "reads", "fetches", "captured", "responses", "signal_states", "run_logs"):
        assert new[key] == legacy[key], key
    # And it is faster: no re-reads, many rows per INSERT, far fewer statements.
    assert legacy["rows_per_insert"] == 1
    assert new["rows_per_insert"] > 5
    assert new["inserts"] * 3 < legacy["inserts"]
    assert new["statements"] * 3 < legacy["statements"]


# --------------------------------------------------------------------------- #
# The weak-valid daily count reads today's candidate rows, on every path
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", PATHS)
def test_weak_valid_count_reads_todays_candidate_rows_on_every_path(tmp_path, monkeypatch, path) -> None:
    # Postgres compares the TEXT created_at under the database's collation, SQLite
    # bytewise: on both, every timestamp form the count accepts as today must sort at
    # or after the bare date, and every earlier day before it.
    now = datetime(2026, 10, 17, 14, 30, tzinfo=UTC)
    monkeypatch.setattr(scan_promotion, "utc_now", lambda: now)
    db = _database(tmp_path, path)
    repository = ScanDecisionRepository(db)
    counted = {
        "2026-10-17T13:00:00.000001+00:00": True,
        "2026-10-17T13:00:01Z": True,
        "2026-10-17T00:00:00+00:00": True,
        "2026-10-17T00:00:00": True,
        "2026-10-17 13:00:28+00:00": True,
        "2026-10-17T01:00:00+02:00": True,
        "2026-10-17T00:00:00-05:00": True,
        "2026-10-16T23:59:59.999999+00:00": False,
        "2026-10-16T23:00:00-05:00": False,
        "2026-10-18T00:00:01+00:00": False,  # a later date: read, then left out by the date check
    }
    promoted = json.dumps(
        {"metadata": {"source": "supervised_weak_valid", "supervised_weak_valid_promoted_to_candidate": True}}
    )
    rows = [(status, created_at) for created_at in counted for status in ("candidate", "rejected", "no_signal")]
    rows += [("CANDIDATE", "2026-10-17T13:00:02+00:00"), ("watchlist", "2026-10-17T13:00:03+00:00")]
    with db.connect() as connection:
        for status, created_at in rows:
            connection.execute(
                "INSERT INTO scan_decisions (scan_task, symbol, strategy_name, timeframe, status, final_score,"
                " alert_eligible, freshness, reason_codes_json, rejection_reasons_json, payload_json, created_at)"
                " VALUES ('intraday_scan', 'MSFT', 'vwap_reclaim', '5m', ?, 55.0, 1, 'fresh', '[]', '[]', ?, ?)",
                (status, promoted, created_at),
            )

    read = sorted(_weak_valid_count_rows(repository, now.date()), key=lambda row: row.id)

    expected = sorted(
        (
            row
            for row in repository.list(limit=1000)
            if row.status.lower() == "candidate" and row.created_at[:10] >= "2026-10-17"
        ),
        key=lambda row: row.id,
    )
    assert len(read) == 9  # 7 of today, 1 of tomorrow, the "CANDIDATE" row
    assert [row.model_dump() for row in read] == [row.model_dump() for row in expected]
    assert _weak_valid_daily_count(SimpleNamespace(scan_decisions=repository)) == sum(counted.values()) + 1
