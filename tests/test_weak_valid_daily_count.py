"""The supervised weak-valid daily cap counts today's promotions by date.

``_weak_valid_daily_count`` reads every scan decision of today (UTC date) whose
status is ``candidate`` -- no longer the newest 5,000 rows of any status -- and
checks each row exactly as before. ``_reference_weak_valid_daily_count`` is a
verbatim copy of the count it replaced. While the newest 5,000 rows still hold all
of today's rows the two must agree, row for row and end to end (logs, decision and
rows written), including the original's edge cases: case-insensitive status/source,
``Z``, offset, naive and space-separated timestamps. Once more than 5,000 later rows
push today's promotions out of that window only the new count still sees them, so
the cap holds. A read that fails, or one of today's candidate rows that cannot be
loaded or checked, counts as the daily limit (fail closed) and is logged, where the
original returned 0 or a partial count.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pandas as pd
import pytest
from sqlalchemy import create_engine

from app.live_signal_schema import MarketQuote
from app.models.signal import Signal, SignalAction
from app.screener import scan_promotion, service_scan
from app.screener.scan_promotion import _weak_valid_count_rows, _weak_valid_daily_count
from app.screener.service import MarketScreenerService
from app.storage.db import Database
from app.storage.repositories import ScanDecisionRepository
from app.utils.time import utc_now
from tests.conftest import make_settings
from tests.test_screener_service import (
    FakeBacktestRepository,
    FakeMarketDataEngine,
    FakeRunLogRepository,
    FakeSignalStateRepository,
    _frame,
)

NOW = datetime(2026, 10, 17, 14, 30, tzinfo=UTC)
FAILED = "paper_supervised_weak_valid_daily_count_failed"
CHANGED_BY_THE_CAP = {"daily_count", "promotion_blockers", "promoted_to_candidate"}


def _reference_weak_valid_daily_count(service: Any) -> int:
    repository = getattr(service, "scan_decisions", None)
    if repository is None or not hasattr(repository, "list"):
        return 0
    today = utc_now().date()
    count = 0
    with suppress(Exception):
        for row in repository.list(limit=5000):
            if str(getattr(row, "status", "") or "").lower() != "candidate":
                continue
            payload = dict(getattr(row, "payload", {}) or {})
            metadata = dict(payload.get("metadata") or {})
            if str(metadata.get("source") or "").lower() != "supervised_weak_valid":
                continue
            if not bool(metadata.get("supervised_weak_valid_promoted_to_candidate")):
                continue
            created_at = getattr(row, "created_at", None)
            if created_at is None:
                count += 1
                continue
            if isinstance(created_at, str):
                created_date = created_at.replace("Z", "+00:00")
                created_date = datetime.fromisoformat(created_date).date()
            else:
                created_date = created_at.date()
            if created_date == today:
                count += 1
    return count


@pytest.fixture
def freeze(monkeypatch):
    """Pin the clock both implementations read ``today`` from."""

    def _freeze(instant: datetime) -> None:
        def clock() -> datetime:
            return instant

        monkeypatch.setattr(scan_promotion, "utc_now", clock)
        monkeypatch.setattr(sys.modules[__name__], "utc_now", clock)

    return _freeze


def _repository(path) -> ScanDecisionRepository:
    database = Database(make_settings(path))
    database.initialize()
    return ScanDecisionRepository(database)


_UNSET = object()


def _row(
    status: str = "candidate",
    *,
    created_at: str,
    payload: Any = None,
    payload_json: Any = _UNSET,
    reason_codes_json: str = '["supervised_weak_valid_promoted"]',
    final_score: Any = 55.0,
    symbol: str = "MSFT",
) -> tuple:
    stored_payload = json.dumps(payload or {}) if payload_json is _UNSET else payload_json
    return (
        "intraday_scan", symbol, "vwap_reclaim", "5m", status, final_score, 1, "fresh",
        reason_codes_json, '["relative_volume_too_low"]', stored_payload, created_at,
    )


def _insert(repository: ScanDecisionRepository, rows: list[tuple]) -> None:
    with repository.db.connect() as connection:
        connection.executemany(
            """
            INSERT INTO scan_decisions (
                scan_task, symbol, strategy_name, timeframe, status, final_score,
                alert_eligible, freshness, reason_codes_json, rejection_reasons_json,
                payload_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )


def _weak(promoted: Any = True, source: str = "supervised_weak_valid", **extra: Any) -> dict:
    return {"metadata": {"source": source, "supervised_weak_valid_promoted_to_candidate": promoted}, **extra}


def _filler(count: int, *, hour: int = 9) -> list[tuple]:
    """Distinct, ordinary non-candidate rows (real-looking payloads) for the window tests."""

    return [
        _row(
            "no_signal" if index % 2 else "rejected",
            created_at=f"2026-10-17T{hour:02d}:{index // 60 % 60:02d}:{index % 60:02d}.{index:06d}+00:00",
            payload={"market_data_status": {"verified": True}, "metadata": _weak()["metadata"]},
            symbol=f"S{index % 50}",
        )
        for index in range(count)
    ]


def _promotions(count: int, *, hour: int = 8) -> list[tuple]:
    return [
        _row(created_at=f"2026-10-17T{hour:02d}:0{index}:00.000001+00:00", payload=_weak())
        for index in range(count)
    ]


def _busy_day() -> list[tuple]:
    """4 promotions early in the day, then 5,220 later rows: 5,200 of other statuses
    and 20 candidates that are not promotions (read, never counted)."""

    near_misses = [
        _row(created_at=f"2026-10-17T11:00:{index:02d}.000001+00:00", payload=_weak(source="paper_near_miss"))
        for index in range(20)
    ]
    return [*_promotions(4), *_filler(5200, hour=10), *near_misses]


def _both(service: Any) -> tuple[int, int]:
    return _reference_weak_valid_daily_count(service), _weak_valid_daily_count(service)


def _todays_candidates(repository: ScanDecisionRepository, today: Any) -> list[Any]:
    """What the new read must return, taken from list(): today's (or later) candidate rows."""

    return [
        row
        for row in repository.list(limit=100_000)
        if row.status.lower() == "candidate" and row.created_at >= today.isoformat()
    ]


def _varied_rows() -> list[tuple]:
    d, prev, nxt = "2026-10-17", "2026-10-16", "2026-10-18"
    return [
        _row(created_at=f"{d}T13:00:00.000001+00:00", payload=_weak()),  # counts
        _row(created_at=f"{d}T13:00:01Z", payload=_weak()),  # "Z" suffix, counts
        _row(created_at=f"{d}T00:00:00+00:00", payload=_weak()),  # midnight, no fraction, counts
        _row(created_at=f"{d}T00:00:00", payload=_weak()),  # naive midnight, counts
        _row(created_at=f"{d} 13:00:28+00:00", payload=_weak()),  # space separator, counts
        _row(created_at=f"{prev}T23:59:59.999999+00:00", payload=_weak()),  # yesterday
        _row(created_at=f"{d}T01:00:00+02:00", payload=_weak()),  # written date today (UTC: yesterday), counts
        _row(created_at=f"{prev}T23:00:00-05:00", payload=_weak()),  # written date yesterday (UTC: today)
        _row(created_at=f"{nxt}T00:00:01+00:00", payload=_weak()),  # tomorrow
        _row("Candidate", created_at=f"{d}T13:00:02+00:00", payload=_weak()),  # counts
        _row("CANDIDATE", created_at=f"{d}T13:00:03+00:00", payload=_weak()),  # counts
        _row("candidate ", created_at=f"{d}T13:00:04+00:00", payload=_weak()),
        _row("candİdate", created_at=f"{d}T13:00:05+00:00", payload=_weak()),
        _row("CANDİDATE", created_at=f"{d}T13:00:06+00:00", payload=_weak()),
        _row("rejected", created_at=f"{d}T13:00:07+00:00", payload=_weak()),
        _row("watchlist", created_at=f"{d}T13:00:08+00:00", payload=_weak()),
        _row("no_signal", created_at=f"{d}T13:00:09+00:00", payload=_weak()),
        _row("suppressed", created_at=f"{d}T13:00:10+00:00", payload=_weak()),
        _row("REJECTED", created_at=f"{d}T13:00:11+00:00", payload=_weak()),
        _row(created_at=f"{d}T13:00:12+00:00", payload=_weak(promoted=False)),
        _row(created_at=f"{d}T13:00:13+00:00", payload=_weak(promoted=1)),  # truthy, counts
        _row(created_at=f"{d}T13:00:14+00:00", payload=_weak(promoted="yes")),  # truthy, counts
        _row(created_at=f"{d}T13:00:15+00:00", payload=_weak(promoted="")),
        _row(created_at=f"{d}T13:00:16+00:00", payload=_weak(promoted=None)),
        _row(created_at=f"{d}T13:00:17+00:00", payload=_weak(promoted=0)),
        _row(created_at=f"{d}T13:00:18+00:00", payload={"metadata": {"source": "supervised_weak_valid"}}),
        _row(created_at=f"{d}T13:00:19+00:00", payload=_weak(source="SUPERVISED_WEAK_VALID")),  # counts
        _row(created_at=f"{d}T13:00:20+00:00", payload=_weak(source="paper_near_miss")),
        _row(created_at=f"{d}T13:00:21+00:00", payload={"metadata": None}),
        _row(created_at=f"{d}T13:00:22+00:00", payload={}),
        _row(created_at=f"{d}T13:00:23+00:00", payload_json=None),
        _row(created_at=f"{d}T13:00:24+00:00", payload_json=""),
        _row(created_at=f"{d}T13:00:25+00:00", payload=_weak()["metadata"]),  # flags at top level only
        _row(created_at=f"{d}T13:00:26+00:00", payload=_weak(extra_noise=list(range(50)))),  # counts
        _row(created_at=f"{d}T13:00:27+00:00", payload={"metadata": [("source", "supervised_weak_valid")]}),
    ]


@pytest.mark.parametrize(
    "instant",
    [
        NOW,
        datetime(2026, 10, 17, 0, 0, 0, 1, tzinfo=UTC),
        datetime(2026, 10, 17, 23, 59, 59, 999999, tzinfo=UTC),
        datetime(2026, 10, 16, 12, 0, tzinfo=UTC),
        datetime(2026, 10, 18, 12, 0, tzinfo=UTC),
    ],
)
def test_count_matches_reference_on_varied_rows(tmp_path, freeze, instant) -> None:
    freeze(instant)
    repository = _repository(tmp_path)
    _insert(repository, _varied_rows())
    service = SimpleNamespace(scan_decisions=repository)

    reference, by_date = _both(service)

    assert by_date == reference
    assert reference == {NOW.date(): 12, NOW.date() - timedelta(days=1): 2, NOW.date() + timedelta(days=1): 1}[
        instant.date()
    ]


def test_todays_promotions_still_count_after_more_than_5000_later_rows(tmp_path, freeze) -> None:
    # The leak the newest-5,000-row window had: 4 promotions early in the day, then a
    # busy session writes more than 5,000 rows -- the old count no longer saw any of
    # them, so a fifth promotion went through. Today's later candidate rows that are
    # not promotions are read but never counted.
    freeze(NOW)
    repository = _repository(tmp_path)
    _insert(repository, _busy_day())
    service = SimpleNamespace(scan_decisions=repository)

    reference, by_date = _both(service)

    assert reference == 0  # all four sit below the newest 5,000 rows
    assert by_date == 4
    assert len(_weak_valid_count_rows(repository, NOW.date())) == 24


def test_read_is_todays_candidate_rows_of_list(tmp_path) -> None:
    repository = _repository(tmp_path)
    _insert(repository, [*_varied_rows(), *_filler(40, hour=8), *_promotions(3, hour=7)])
    today = NOW.date()

    rows = _weak_valid_count_rows(repository, today)

    expected = _todays_candidates(repository, today)
    assert [row.model_dump() for row in rows] == [row.model_dump() for row in expected]
    assert {row.created_at[:10] for row in rows} == {"2026-10-17", "2026-10-18"}
    assert {row.status for row in rows} == {"candidate", "Candidate", "CANDIDATE"}
    assert len(rows) == 28 and len(repository.list(limit=100_000)) == 78  # filler, yesterday, other statuses stay out


def test_rows_the_count_does_not_read_can_no_longer_void_it(tmp_path, freeze) -> None:
    # The original loaded the newest 5,000 rows of every status, so one row there the
    # repository could not load returned 0. The count now loads only today's
    # candidate rows: a broken row of another status, or of an earlier day, is never read.
    freeze(NOW)
    repository = _repository(tmp_path)
    unread = [
        _row("rejected", created_at="2026-10-17T10:00:00.000001+00:00", reason_codes_json="[1]"),
        _row("no_signal", created_at="2026-10-17T10:00:01.000001+00:00", final_score="not-a-number"),
        _row(created_at="2026-10-16T23:59:59.999999+00:00", payload_json="{not json"),
    ]
    _insert(repository, [*unread, *_promotions(3, hour=11)])
    service = SimpleNamespace(scan_decisions=repository)

    reference, by_date = _both(service)

    assert reference == 0
    assert by_date == 3


@pytest.mark.parametrize(
    "unreadable",
    [
        _row(created_at="2026-10-17T10:00:00.000001+00:00", reason_codes_json="[1]", payload=_weak()),
        _row(created_at="2026-10-17T10:00:00.000001+00:00", final_score="not-a-number", payload=_weak()),
        _row(created_at="2026-10-17T10:00:00.000001+00:00", payload_json="{not json"),
        _row(created_at="2026-10-17T11:00:00+00:00 not-a-time", payload=_weak()),
        _row(created_at="2026-10-17T11:00:00.000001+00:00", payload={"metadata": "oops"}),
    ],
    ids=[
        "non_string_reason_code",
        "non_numeric_score",
        "broken_payload",
        "unparseable_created_at",
        "non_mapping_metadata",
    ],
)
def test_a_candidate_row_of_today_that_cannot_be_checked_fails_closed(tmp_path, freeze, caplog, unreadable) -> None:
    # The original returned 0 (a row list() cannot load) or the count of the rows
    # newer than the one that raised. Either let promotions past the cap; the count
    # now reads as the daily limit and says why.
    freeze(NOW)
    repository = _repository(tmp_path)
    _insert(repository, [*_promotions(1, hour=9), unreadable, *_promotions(2, hour=12)])
    logs = FakeRunLogRepository()
    service = SimpleNamespace(scan_decisions=repository, logs=logs)

    with caplog.at_level(logging.WARNING, logger=scan_promotion.__name__):
        reference, by_date = _both(service)

    assert reference in (0, 2)
    assert by_date == 4  # the default daily limit
    assert [event for event, _ in logs.items] == [FAILED]
    assert logs.items[0][1]["daily_count"] == 4 and logs.items[0][1]["error"]
    assert "counted as the limit" in caplog.text


def test_count_inside_a_scan_writes_its_queued_rows_first_like_list(tmp_path, freeze, monkeypatch) -> None:
    # A scan's no_signal rows are queued in its thread and only that thread writes
    # them: before each of its reads, as list() does. The count's read is one of those
    # reads, so it writes the queue first (the rows are never candidates, so they
    # cannot change the count).
    from app.storage import scan_decisions as scan_decisions_module

    freeze(NOW)
    monkeypatch.setattr(scan_decisions_module, "utc_now", lambda: NOW)  # the queued row's created_at
    repository = _repository(tmp_path)
    _insert(repository, _promotions(2))
    service = SimpleNamespace(scan_decisions=repository)
    queued = {
        "scan_task": "intraday_scan", "strategy_name": "vwap_reclaim", "timeframe": "5m",
        "status": "no_signal", "final_score": None, "alert_eligible": False, "freshness": "fresh",
        "reason_codes": ["no_signal"], "rejection_reasons": [], "payload": {},
    }

    def stored_symbols() -> list[str]:
        connection = sqlite3.connect(repository.db.path)  # past the repository: no flush
        try:
            return [row[0] for row in connection.execute("SELECT symbol FROM scan_decisions ORDER BY id")]
        finally:
            connection.close()

    with repository.deferred_writes():
        repository.create_deferred(symbol="NVDA", **queued)
        assert "NVDA" not in stored_symbols()
        assert _weak_valid_daily_count(service) == 2
        assert stored_symbols()[-1] == "NVDA"
        assert repository._pending() == []


def test_read_through_the_sqlalchemy_connection_wrapper(tmp_path, freeze) -> None:
    # Production (Postgres) runs the same SQL through Database's SQLAlchemy wrapper:
    # '?' rebinding and mapping rows must behave the same (the real Postgres run is
    # in tests/test_scan_decision_writes.py).
    freeze(NOW)
    repository = _repository(tmp_path)
    _insert(repository, [*_varied_rows(), *_filler(40, hour=8)])
    repository.db.is_sqlite = False
    repository.db._engine = create_engine(f"sqlite:///{repository.db.path}")
    service = SimpleNamespace(scan_decisions=repository)

    reference, by_date = _both(service)

    assert reference == 12
    assert by_date == reference
    rows = _weak_valid_count_rows(repository, NOW.date())
    expected = _todays_candidates(repository, NOW.date())
    assert [row.model_dump() for row in rows] == [row.model_dump() for row in expected]


def test_other_repositories_keep_the_plain_list_call(freeze) -> None:
    freeze(NOW)

    class ListOnly:
        def __init__(self, rows: list[Any]) -> None:
            self.rows = rows
            self.calls: list[dict[str, Any]] = []

        def list(self, **kwargs: Any) -> list[Any]:
            self.calls.append(kwargs)
            return self.rows

    rows = [
        SimpleNamespace(status="candidate", payload=_weak(), created_at=None),
        SimpleNamespace(status="candidate", payload=_weak(), created_at=NOW),
        SimpleNamespace(status="candidate", payload=_weak(), created_at=NOW - timedelta(days=1)),
        SimpleNamespace(status="CANDIDATE", payload=_weak(), created_at="2026-10-17T09:00:00Z"),
        SimpleNamespace(status="rejected", payload=_weak(), created_at=None),
        SimpleNamespace(status=None, payload=None, created_at=None),
        SimpleNamespace(payload=_weak()),
    ]
    reference_repo, by_date_repo = ListOnly(rows), ListOnly(rows)

    reference = _reference_weak_valid_daily_count(SimpleNamespace(scan_decisions=reference_repo))
    by_date = _weak_valid_daily_count(SimpleNamespace(scan_decisions=by_date_repo))

    assert reference == 3
    assert by_date == reference
    assert by_date_repo.calls == reference_repo.calls == [{"limit": 5000}]

    class Streaming:
        def list(self, **_kwargs: Any):
            yield SimpleNamespace(status="candidate", payload=_weak(), created_at=None)
            yield SimpleNamespace(status="candidate", payload=_weak(), created_at=None)
            raise RuntimeError("connection dropped")

    assert _both(SimpleNamespace(scan_decisions=Streaming())) == (2, 4)  # fails closed, not 2

    mocked = MagicMock(spec=ScanDecisionRepository)
    mocked.list.return_value = rows
    assert _both(SimpleNamespace(scan_decisions=mocked)) == (3, 3)


def test_a_failed_read_counts_as_the_daily_limit(tmp_path, freeze, monkeypatch) -> None:
    freeze(NOW)
    # No repository: nothing is stored, nothing to count (unchanged).
    assert _both(SimpleNamespace()) == (0, 0)
    assert _both(SimpleNamespace(scan_decisions=None)) == (0, 0)
    assert _both(SimpleNamespace(scan_decisions=SimpleNamespace(db=object()))) == (0, 0)

    repository = _repository(tmp_path)
    _insert(repository, [_row(created_at="2026-10-17T12:00:00+00:00", payload=_weak())])

    def broken_connect():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(repository.db, "connect", broken_connect)
    logs = FakeRunLogRepository()
    assert _both(SimpleNamespace(scan_decisions=repository, logs=logs)) == (0, 4)
    assert logs.items == [(FAILED, {"error": "RuntimeError: database unavailable", "daily_count": 4})]

    # The configured limit, and never 0 even when the limit is 0 or unset.
    for configured, expected in ((6, 6), (1, 1), (0, 1), (None, 1)):
        settings = SimpleNamespace(paper_supervised_weak_valid_max_proposals_per_day=configured)
        assert _weak_valid_daily_count(SimpleNamespace(scan_decisions=repository, settings=settings)) == expected

    class BrokenLogs:
        def log(self, *_args: Any) -> None:
            raise RuntimeError("run_logs unavailable too")

    assert _weak_valid_daily_count(SimpleNamespace(scan_decisions=repository, logs=BrokenLogs())) == 4


def _weak_valid_scan_service(path, repository: ScanDecisionRepository, monkeypatch) -> MarketScreenerService:
    frame = _frame([100 + (index * 0.18) for index in range(90)])
    frame.loc[frame.index[-1], "volume"] = frame["volume"].tail(20).mean() * 0.40
    quote = MarketQuote(symbol="NVDA", bid=116.0, ask=116.04, last_execution=116.02, timestamp="2026-04-11T10:00:00Z")
    service = MarketScreenerService(
        settings=make_settings(
            path,
            market_universe_symbols=["NVDA"],
            screener_default_timeframes=["1d"],
            execution_mode="paper",
            enable_real_trading=False,
            auto_propose_enabled=True,
            paper_auto_approve_proposals=False,
            paper_auto_operation_mode="supervised",
            paper_scanner_exploration_enabled=True,
            paper_exploration_signal_profile="balanced_loose",
            paper_exploration_require_regular_hours=False,
            paper_near_miss_promotion_enabled=False,
            paper_exploration_min_relative_volume=0.80,
            paper_supervised_weak_valid_enabled=True,
            paper_supervised_weak_valid_min_score=45.0,
            paper_supervised_weak_valid_min_reward_to_risk=1.0,
            paper_supervised_weak_valid_min_relative_volume=0.30,
            screener_weak_backtest_action="rank_only",
            screener_min_confidence=0.1,
            screener_min_trend_strength_pct=0.0,
            screener_min_efficiency_ratio=0.0,
            screener_min_execution_quality=0.0,
            screener_min_accuracy_score=0.0,
            screener_min_confirmation_score=0.0,
            screener_max_false_positive_risk=1.0,
            screener_min_market_regime_score=0.0,
            screener_min_timeframe_alignment_score=0.0,
            screener_min_sector_strength_score=0.0,
            screener_min_benchmark_strength_score=0.0,
            screener_min_recent_backtest_consistency=0.0,
        ),
        market_data_engine=FakeMarketDataEngine({("NVDA", "1d"): frame}, {"NVDA": quote}),
        signal_state_repository=FakeSignalStateRepository(),
        run_log_repository=FakeRunLogRepository(),
        backtest_repository=FakeBacktestRepository(
            summary={
                "strategy_name": "weak_valid_strategy",
                "completed_at": "2026-04-10T12:00:00Z",
                "out_of_sample": True,
                "metrics": {
                    "number_of_trades": 40,
                    "profit_factor": 1.3,
                    "win_rate": 53.0,
                    "max_drawdown_pct": 12.0,
                    "out_of_sample": True,
                },
                "trades": [{"pnl_pct": 0.7}, {"pnl_pct": -0.4}, {"pnl_pct": 0.5}],
            }
        ),
        scan_decision_repository=repository,
    )

    class FakeStrategy:
        def generate_signal(self, _history: pd.DataFrame, symbol: str) -> Signal:
            return Signal(
                symbol=symbol,
                strategy_name="weak_valid_strategy",
                action=SignalAction.BUY,
                confidence=0.90,
                price=116.02,
                stop_loss=114.00,
                take_profit=118.10,
                rationale="supervised weak-valid fixture",
                metadata={
                    "style": "momentum",
                    "signal_role": "entry_long",
                    "risk_reward_ratio": 1.03,
                    "trend_quality": 1.0,
                    "momentum_quality": 1.0,
                    "liquidity_quality": 1.0,
                    "indicator_confluence_score": 0.8,
                },
            )

    spec = SimpleNamespace(name="weak_valid_strategy", timeframe="1d", style="momentum", default_kwargs={})
    monkeypatch.setattr(service, "_strategy_specs_for_timeframe", lambda *_args, **_kwargs: [spec])
    monkeypatch.setattr(service, "_build_strategy", lambda _spec: FakeStrategy())
    return service


def _scan_outcome(path, prior_rows: list[tuple], monkeypatch, *, counter: Any) -> dict[str, Any]:
    repository = _repository(path)
    _insert(repository, prior_rows)
    service = _weak_valid_scan_service(path, repository, monkeypatch)
    with monkeypatch.context() as patch:
        patch.setattr(service_scan, "_weak_valid_daily_count", counter)
        response = service.scan_universe(limit=3, scan_task="intraday_scan", strategy_spec_keys=["weak_valid_strategy:1d"])
    written = [
        (
            row.symbol, row.strategy_name, row.timeframe, row.status, row.final_score, row.alert_eligible,
            row.freshness, row.reason_codes, row.rejection_reasons,
            (row.payload.get("metadata") or {}).get("source"),
            (row.payload.get("metadata") or {}).get("supervised_weak_valid_promoted_to_candidate"),
        )
        for row in repository.list(limit=100_000, symbol="NVDA")
    ]
    return {
        "attempts": [
            payload for event, payload in service.logs.items if event == "paper_supervised_weak_valid_promotion_attempt"
        ],
        "candidates": [(item.symbol, item.metadata.get("source")) for item in response.candidates],
        "written": written,
        "failures": [payload for event, payload in service.logs.items if event == FAILED],
        "events": [event for event, _ in service.logs.items if event != FAILED],
    }


def _limited(outcome: dict[str, Any]) -> bool:
    return "supervised_weak_valid_daily_limit_reached" in outcome["attempts"][0]["promotion_blockers"]


def _promoted(outcome: dict[str, Any]) -> bool:
    return any(source == "supervised_weak_valid" for _symbol, source in outcome["candidates"])


@pytest.mark.parametrize("promoted_today", [0, 2, 4])
def test_scan_logs_the_same_daily_count_and_decision(tmp_path, freeze, monkeypatch, promoted_today) -> None:
    # End to end: the same prior rows, one scan counted by the original function and
    # one by date. Logs (daily_count, blockers), the promotion decision and the rows
    # written must match -- including when today's promotions reach the default daily
    # limit of 4.
    freeze(NOW)
    prior = [
        *(
            _row(created_at=f"2026-10-17T09:0{index}:00.000001+00:00", payload=_weak())
            for index in range(promoted_today)
        ),
        _row(created_at="2026-10-16T15:00:00.000001+00:00", payload=_weak()),
        _row("rejected", created_at="2026-10-17T09:30:00.000001+00:00", payload=_weak()),
        *_filler(30, hour=10),
    ]

    reference = _scan_outcome(tmp_path / "reference", prior, monkeypatch, counter=_reference_weak_valid_daily_count)
    by_date = _scan_outcome(tmp_path / "by_date", prior, monkeypatch, counter=_weak_valid_daily_count)

    assert by_date == reference
    assert reference["attempts"] and all(item["daily_count"] == promoted_today for item in reference["attempts"])
    assert _limited(reference) is (promoted_today >= 4)
    assert _promoted(reference) is (promoted_today < 4)
    assert reference["failures"] == []


def test_scan_after_more_than_5000_later_rows_still_stops_at_the_daily_limit(tmp_path, freeze, monkeypatch) -> None:
    # The cap holds on a busy day: 4 promotions, then 5,220 later rows. The scan
    # decides and logs exactly as on a quiet day with the same 4 promotions; the
    # original count saw 0 and promoted a fifth. Between the two counts only the
    # cap's own fields of the attempt log differ.
    freeze(NOW)
    busy = _busy_day()

    by_date = _scan_outcome(tmp_path / "busy", busy, monkeypatch, counter=_weak_valid_daily_count)
    quiet = _scan_outcome(tmp_path / "quiet", _promotions(4), monkeypatch, counter=_weak_valid_daily_count)
    reference = _scan_outcome(tmp_path / "reference", busy, monkeypatch, counter=_reference_weak_valid_daily_count)

    assert by_date == quiet
    assert by_date["attempts"] and all(item["daily_count"] == 4 for item in by_date["attempts"])
    assert _limited(by_date) and not _promoted(by_date)
    assert by_date["failures"] == []
    assert reference["attempts"][0]["daily_count"] == 0 and _promoted(reference)  # the old leak
    first, old_first = by_date["attempts"][0], reference["attempts"][0]
    assert {key: value for key, value in first.items() if key not in CHANGED_BY_THE_CAP} == {
        key: value for key, value in old_first.items() if key not in CHANGED_BY_THE_CAP
    }
    assert set(first["promotion_blockers"]) - set(old_first["promotion_blockers"]) == {
        "supervised_weak_valid_daily_limit_reached"
    }


def test_scan_with_an_unreadable_count_blocks_like_a_reached_limit(tmp_path, freeze, monkeypatch) -> None:
    # One of today's candidate rows cannot be loaded: the original counted 0 and
    # promoted; the scan now decides and logs exactly as with 4 promotions counted,
    # plus one failure event.
    freeze(NOW)
    broken = [_row(created_at="2026-10-17T09:00:00.000001+00:00", payload_json="{not json")]

    failed = _scan_outcome(tmp_path / "failed", broken, monkeypatch, counter=_weak_valid_daily_count)
    limited = _scan_outcome(tmp_path / "limited", _promotions(4), monkeypatch, counter=_weak_valid_daily_count)
    reference = _scan_outcome(tmp_path / "reference", broken, monkeypatch, counter=_reference_weak_valid_daily_count)

    assert {key: value for key, value in failed.items() if key != "failures"} == {
        key: value for key, value in limited.items() if key != "failures"
    }
    assert _limited(failed) and not _promoted(failed)
    assert [item["daily_count"] for item in failed["failures"]] == [4]
    assert failed["failures"][0]["error"].startswith("JSONDecodeError")
    assert reference["attempts"][0]["daily_count"] == 0 and _promoted(reference)
