"""Scan-decision writes: one-round-trip create() and the batched no_signal rows.

Both are pure speed changes, so every test here runs the replaced code (copied
below as the reference oracle) next to the new code and asserts the same rows,
ids, returned records and reads.
"""

from __future__ import annotations

import itertools
import json
import sqlite3
import sys
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pytest
from sqlalchemy import create_engine

import app.utils.time as time_utils
from app.live_signal_schema import MarketQuote
from app.models.screener import ScanDecisionRecord
from app.screener.service import MarketScreenerService
from app.storage import scan_decisions as scan_decisions_module
from app.storage.db import Database, _SqlAlchemyConnection
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


def _database(directory, path: str) -> Database:
    directory.mkdir(parents=True, exist_ok=True)
    settings = make_settings(directory)
    Database(settings).initialize()
    return _WrappedSqliteDatabase(settings) if path == "sqlalchemy" else Database(settings)


class _StatementLog:
    """Record every statement a Database runs (whitespace-normalised)."""

    def __init__(self, db: Database):
        self.statements: list[str] = []
        original = db.connect
        statements = self.statements

        class _Recording:
            def __init__(self, connection: Any):
                self._connection = connection

            def execute(self, sql: str, params: Any = ()) -> Any:
                statements.append(" ".join(sql.split()))
                return self._connection.execute(sql, params)

        @contextmanager
        def connect():
            with original() as connection:
                yield _Recording(connection)

        db.connect = connect  # type: ignore[method-assign]


class _Clock:
    """A ticking utc_now for every app module, so two runs stamp identical times."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch):
        self.start = datetime(2026, 4, 13, 14, 30, 0, 123456, tzinfo=UTC)
        self.reset()
        original = time_utils.utc_now
        for name, module in list(sys.modules.items()):
            if (name == "app" or name.startswith("app.")) and getattr(module, "utc_now", None) is original:
                monkeypatch.setattr(module, "utc_now", self)

    def reset(self) -> None:
        self._ticks = itertools.count()

    def __call__(self) -> datetime:
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
    """Rows committed to the file, read past the repository (no flush)."""

    connection = sqlite3.connect(db.path)
    try:
        return int(connection.execute("SELECT COUNT(*) FROM scan_decisions").fetchone()[0])
    finally:
        connection.close()


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


# --------------------------------------------------------------------------- #
# P2: create() returns the stored row without a follow-up SELECT
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", ["sqlite3", "sqlalchemy"])
def test_create_returns_the_stored_row_without_a_follow_up_select(tmp_path, monkeypatch, path) -> None:
    clock = _Clock(monkeypatch)
    legacy = LegacyScanDecisionRepository(_database(tmp_path / "legacy", path))
    new_db = _database(tmp_path / "new", path)
    repository = ScanDecisionRepository(new_db)
    log = _StatementLog(new_db)

    for case in CREATE_CASES:
        clock.reset()
        legacy_record = legacy.create(**case)
        clock.reset()
        before = len(log.statements)
        record = repository.create(**case)

        # One statement, no re-read.
        statements = log.statements[before:]
        assert len(statements) == 1
        assert statements[0].startswith("INSERT INTO scan_decisions")
        assert statements[0].endswith("RETURNING *")
        # Same record the old INSERT + SELECT returned (id, created_at, payload, types)...
        assert _canonical(record) == _canonical(legacy_record)
        # ...and the same as a fresh read of the row.
        assert _canonical(record) == _canonical(repository.get(record.id))

    assert _table(new_db) == _table(legacy.db)


# --------------------------------------------------------------------------- #
# P6: no_signal rows are queued and written in one INSERT per symbol
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("path", ["sqlite3", "sqlalchemy"])
def test_deferred_rows_and_ids_match_one_at_a_time_writes(tmp_path, monkeypatch, path) -> None:
    clock = _Clock(monkeypatch)
    calls: list[tuple[str, dict[str, Any]]] = []
    for index, case in enumerate(CREATE_CASES * 3):
        symbol = ["aaa", "AAA", "bbb", "bbb", "ccc"][index % 5]  # runs of one symbol, then the next
        calls.append(("create" if index % 7 == 3 else "create_deferred", {**case, "symbol": symbol}))

    def run(repository: ScanDecisionRepository) -> list[Any]:
        clock.reset()
        returned = []
        for method, fields in calls:
            result = getattr(repository, method)(**fields)
            returned.append(None if result is None else _canonical(result))
        repository.flush()
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


def test_every_read_sees_rows_queued_before_it(tmp_path, monkeypatch) -> None:
    _Clock(monkeypatch)
    db = _database(tmp_path, "sqlite3")
    repository = ScanDecisionRepository(db)

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


def test_a_large_queue_is_written_in_bounded_chunks(tmp_path, monkeypatch) -> None:
    clock = _Clock(monkeypatch)
    legacy = LegacyScanDecisionRepository(_database(tmp_path / "legacy", "sqlite3"))
    repository = ScanDecisionRepository(_database(tmp_path / "new", "sqlite3"))
    log = _StatementLog(repository.db)
    for target in (legacy, repository):
        clock.reset()
        for index in range(203):
            target.create_deferred(**_fields(strategy_name=f"spec_{index}", payload={"i": index}))
        target.flush()
    assert _raw_count(repository.db) == 203
    assert [statement.count("(?, ?, ?") for statement in log.statements] == [80, 80, 43]
    assert _table(repository.db) == _table(legacy.db)


def test_a_read_from_another_thread_sees_rows_queued_by_the_scan(tmp_path, monkeypatch) -> None:
    _Clock(monkeypatch)
    db = _database(tmp_path, "sqlite3")
    repository = ScanDecisionRepository(db)
    repository.create_deferred(**_fields(strategy_name="queued_in_scan_thread"))

    seen: list[str] = []
    reader = threading.Thread(target=lambda: seen.extend(item.strategy_name for item in repository.list()))
    reader.start()
    reader.join()

    assert seen == ["queued_in_scan_thread"]


def test_a_failed_flush_drops_the_queue_and_raises(tmp_path, monkeypatch) -> None:
    _Clock(monkeypatch)
    db = _database(tmp_path, "sqlite3")
    repository = ScanDecisionRepository(db)
    repository.create_deferred(**_fields(strategy_name="lost"))
    original = db.connect

    @contextmanager
    def broken():
        raise sqlite3.OperationalError("database is locked")
        yield  # pragma: no cover

    db.connect = broken  # type: ignore[method-assign]
    with pytest.raises(sqlite3.OperationalError):
        repository.flush()
    db.connect = original  # type: ignore[method-assign]
    assert repository.list() == []


def test_create_still_raises_before_writing_an_unserialisable_payload(tmp_path, monkeypatch) -> None:
    _Clock(monkeypatch)
    db = _database(tmp_path, "sqlite3")
    repository = ScanDecisionRepository(db)
    with pytest.raises(TypeError):
        repository.create(**_fields(payload={"bad": object()}))
    with pytest.raises(TypeError):
        repository.create_deferred(**_fields(payload={"bad": object()}))
    repository.flush()
    assert _raw_count(db) == 0


def test_payload_is_serialised_when_queued_not_when_written(tmp_path, monkeypatch) -> None:
    _Clock(monkeypatch)
    repository = ScanDecisionRepository(_database(tmp_path, "sqlite3"))
    payload: dict[str, Any] = {"market_data_status": {"verified": True}}
    repository.create_deferred(**_fields(payload=payload))
    payload["market_data_status"]["verified"] = False  # the scan reuses and changes dicts
    assert repository.list()[0].payload == {"market_data_status": {"verified": True}}


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

    # A flush that fails as well never replaces the scan's own error.
    monkeypatch.setattr(repository, "flush", lambda: (_ for _ in ()).throw(sqlite3.OperationalError("x")))
    with pytest.raises(RuntimeError, match="scan blew up"):
        service.scan_universe(scan_task="premarket_scan")


def test_moved_repository_is_still_importable_from_repositories() -> None:
    assert ScanDecisionRepository is scan_decisions_module.ScanDecisionRepository


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


def _scan_service(tmp_path, repository: ScanDecisionRepository, learning: _RecordingLearning) -> MarketScreenerService:
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
        ),
        market_data_engine=FakeMarketDataEngine(frames, quotes),
        signal_state_repository=FakeSignalStateRepository(),
        run_log_repository=FakeRunLogRepository(),
        backtest_repository=FakeBacktestRepository(summary=backtest_summary),
        scan_decision_repository=repository,
        telegram_notifier=FakeTelegramNotifier(),
        learning_service=learning,
    )


@pytest.mark.parametrize("path", ["sqlite3", "sqlalchemy"])
def test_whole_scans_write_and_read_exactly_as_before(tmp_path, monkeypatch, path) -> None:
    clock = _Clock(monkeypatch)

    def run(name: str, repository_class: type[ScanDecisionRepository]) -> dict[str, Any]:
        clock.reset()
        db = _database(tmp_path / name, path)
        repository = repository_class(db)
        log = _StatementLog(db)
        reads: list[Any] = []
        _recording_reads(repository, reads)
        learning = _RecordingLearning()
        service = _scan_service(tmp_path / name, repository, learning)
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
    assert len(new["table"]) > 100
    for key in ("table", "reads", "captured", "responses", "signal_states", "run_logs"):
        assert new[key] == legacy[key], key
    # And it is faster: no re-reads, many rows per INSERT, far fewer statements.
    assert legacy["rows_per_insert"] == 1
    assert new["rows_per_insert"] > 10
    assert new["inserts"] * 4 < legacy["inserts"]
    assert new["statements"] * 5 < legacy["statements"]
