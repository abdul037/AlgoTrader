"""Parity of the narrowed weak-valid daily count with the original full ``list()`` read.

``_reference_weak_valid_daily_count`` is a verbatim copy of ``_weak_valid_daily_count``
as it was before the read stopped shipping payloads it never uses. Every test runs
both on the same data and requires the same count (and, end to end, the same logs
and scan decisions), including the edge cases of the original: the 5,000-row window,
case-insensitive status/source, ``Z`` and offset timestamps, an unreadable row
voiding the whole read, and an error part-way through stopping the count there.
"""

from __future__ import annotations

import json
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


def _both(service: Any) -> tuple[int, int]:
    return _reference_weak_valid_daily_count(service), _weak_valid_daily_count(service)


def _varied_rows() -> list[tuple]:
    d, prev, nxt = "2026-10-17", "2026-10-16", "2026-10-18"
    return [
        _row(created_at=f"{d}T13:00:00.000001+00:00", payload=_weak()),  # counts
        _row(created_at=f"{d}T13:00:01Z", payload=_weak()),  # "Z" suffix, counts
        _row(created_at=f"{d}T00:00:00+00:00", payload=_weak()),  # midnight, no fraction, counts
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

    reference, narrowed = _both(service)

    assert narrowed == reference
    if instant.date() == NOW.date():
        assert reference == 10  # the rows marked "counts" above


def test_count_keeps_the_newest_5000_row_window(tmp_path, freeze) -> None:
    # Today's promotions older than the newest 5,000 rows were never counted by the
    # original read; the narrowed read must not start counting them.
    freeze(NOW)
    repository = _repository(tmp_path)
    outside = [_row(created_at=f"2026-10-17T08:00:0{index}.000001+00:00", payload=_weak()) for index in range(2)]
    edge = [_row(created_at="2026-10-17T08:30:00.000001+00:00", payload=_weak())]
    newest = [_row(created_at=f"2026-10-17T12:00:0{index}.000001+00:00", payload=_weak()) for index in range(2)]
    _insert(repository, outside + edge + _filler(4997) + newest)
    service = SimpleNamespace(scan_decisions=repository)

    reference, narrowed = _both(service)

    assert reference == 3  # edge + newest; the two older rows sit outside the window
    assert narrowed == reference


def test_count_inside_a_scan_writes_its_queued_rows_first_like_list(tmp_path, freeze, monkeypatch) -> None:
    # P5 (this read) meets P6 (a scan's no_signal rows queued in its thread): list()
    # writes the thread's queue before it reads, so the narrowed read must too, or a
    # queued row is missing from the newest-5,000 window and an older promotion slides
    # back in. The queue is empty when scan_universe counts today; a scan nested in
    # another scan's deferred_writes() inherits that scan's queue.
    from app.storage import scan_decisions as scan_decisions_module

    freeze(NOW)
    monkeypatch.setattr(scan_decisions_module, "utc_now", lambda: NOW)  # the queued row's created_at
    repository = _repository(tmp_path)
    oldest = _row(created_at="2026-10-17T08:00:00.000001+00:00", payload=_weak())
    newest = [_row(created_at=f"2026-10-17T12:00:0{index}.000001+00:00", payload=_weak()) for index in range(2)]
    _insert(repository, [oldest, *_filler(4997), *newest])  # exactly 5,000 rows
    service = SimpleNamespace(scan_decisions=repository)
    queued = {
        "scan_task": "intraday_scan", "strategy_name": "vwap_reclaim", "timeframe": "5m",
        "status": "no_signal", "final_score": None, "alert_eligible": False, "freshness": "fresh",
        "reason_codes": ["no_signal"], "rejection_reasons": [], "payload": {},
    }

    with repository.deferred_writes():
        repository.create_deferred(symbol="NVDA", **queued)
        narrowed = _weak_valid_daily_count(service)  # first: list() would write the queue
        reference = _reference_weak_valid_daily_count(service)
        repository.create_deferred(symbol="AMD", **queued)
        slim_ids = [row.id for row in _weak_valid_count_rows(repository)]
        full_ids = [row.id for row in repository.list(limit=5000)]

    assert reference == 2  # the queued row pushes the oldest promotion out of the window
    assert narrowed == reference
    assert slim_ids == full_ids and len(slim_ids) == 5000


@pytest.mark.parametrize(
    "unreadable",
    [
        _row("rejected", created_at="2026-10-17T10:00:00.000001+00:00", reason_codes_json="[1]"),
        _row("no_signal", created_at="2026-10-17T10:00:00.000001+00:00", final_score="not-a-number"),
        _row(created_at="2026-10-17T10:00:00.000001+00:00", payload_json="{not json"),
    ],
    ids=["non_string_reason_code", "non_numeric_score", "candidate_with_broken_payload"],
)
def test_unreadable_row_in_window_voids_the_count_like_list(tmp_path, freeze, unreadable) -> None:
    # list() builds every row before the count starts, so one row the repository
    # cannot load made the original return 0 -- even with newer promotions present.
    freeze(NOW)
    repository = _repository(tmp_path)
    newer = [_row(created_at=f"2026-10-17T11:00:0{index}.000001+00:00", payload=_weak()) for index in range(2)]
    _insert(repository, [unreadable, *newer])
    service = SimpleNamespace(scan_decisions=repository)

    reference, narrowed = _both(service)

    assert reference == 0
    assert narrowed == reference


def test_unreadable_row_outside_the_window_is_ignored_like_list(tmp_path, freeze) -> None:
    freeze(NOW)
    repository = _repository(tmp_path)
    unreadable = _row("rejected", created_at="2026-10-17T07:00:00.000001+00:00", reason_codes_json="[1]")
    newest = [_row(created_at=f"2026-10-17T12:00:0{index}.000001+00:00", payload=_weak()) for index in range(3)]
    _insert(repository, [unreadable, *_filler(4997), *newest])
    service = SimpleNamespace(scan_decisions=repository)

    reference, narrowed = _both(service)

    assert reference == 3
    assert narrowed == reference


@pytest.mark.parametrize(
    "breaking",
    [
        _row(created_at="2026-10-17T11:00:00+00:00 not-a-time", payload=_weak()),
        _row(created_at="2026-10-17T11:00:00.000001+00:00", payload={"metadata": "oops"}),
    ],
    ids=["unparseable_created_at", "non_mapping_metadata"],
)
def test_error_part_way_stops_the_count_at_the_same_row(tmp_path, freeze, breaking) -> None:
    # The original walks rows newest first inside one suppress(): an error on a row
    # keeps what was counted before it. Same rows in the same order => same count.
    freeze(NOW)
    repository = _repository(tmp_path)
    older = [_row(created_at=f"2026-10-17T10:00:0{index}.000001+00:00", payload=_weak()) for index in range(3)]
    newer = [_row(created_at=f"2026-10-17T12:00:0{index}.000001+00:00", payload=_weak()) for index in range(2)]
    _insert(repository, [*older, breaking, *newer])
    service = SimpleNamespace(scan_decisions=repository)

    reference, narrowed = _both(service)

    assert reference == 2  # only the rows newer than the breaking one
    assert narrowed == reference


def test_narrowed_read_is_list_without_unusable_payloads(tmp_path) -> None:
    repository = _repository(tmp_path)
    _insert(repository, [*_varied_rows(), *_filler(40, hour=8)])

    full = repository.list(limit=5000)
    narrowed = _weak_valid_count_rows(repository)

    assert [row.id for row in narrowed] == [row.id for row in full]
    dropped_payload_bytes = 0
    for slim, row in zip(narrowed, full, strict=True):
        assert slim.model_dump(exclude={"payload"}) == row.model_dump(exclude={"payload"})
        if row.status.lower() == "candidate":
            assert slim.payload == row.payload  # every row the count can use keeps its payload
        elif row.status.isascii() and row.status == row.status.lower():
            assert slim.payload == {}  # e.g. "rejected", "no_signal": left in the database
            dropped_payload_bytes += len(json.dumps(row.payload))
    assert dropped_payload_bytes > 0


def test_narrowed_read_through_the_sqlalchemy_connection_wrapper(tmp_path, freeze) -> None:
    # Production (Postgres) runs the same SQL through Database's SQLAlchemy wrapper:
    # '?' rebinding, mapping rows and the payload_json alias must behave the same.
    freeze(NOW)
    repository = _repository(tmp_path)
    _insert(repository, [*_varied_rows(), *_filler(40, hour=8)])
    repository.db.is_sqlite = False
    repository.db._engine = create_engine(f"sqlite:///{repository.db.path}")
    service = SimpleNamespace(scan_decisions=repository)

    reference, narrowed = _both(service)

    assert reference == 10
    assert narrowed == reference
    full = repository.list(limit=5000)
    slim = _weak_valid_count_rows(repository)
    assert [row.id for row in slim] == [row.id for row in full]
    assert [row.payload for row in slim if row.status.lower() == "candidate"] == [
        row.payload for row in full if row.status.lower() == "candidate"
    ]


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
    reference_repo, narrowed_repo = ListOnly(rows), ListOnly(rows)

    reference = _reference_weak_valid_daily_count(SimpleNamespace(scan_decisions=reference_repo))
    narrowed = _weak_valid_daily_count(SimpleNamespace(scan_decisions=narrowed_repo))

    assert reference == 3
    assert narrowed == reference
    assert narrowed_repo.calls == reference_repo.calls == [{"limit": 5000}]

    class Streaming:
        def list(self, **_kwargs: Any):
            yield SimpleNamespace(status="candidate", payload=_weak(), created_at=None)
            yield SimpleNamespace(status="candidate", payload=_weak(), created_at=None)
            raise RuntimeError("connection dropped")

    assert _both(SimpleNamespace(scan_decisions=Streaming())) == (2, 2)

    mocked = MagicMock(spec=ScanDecisionRepository)
    mocked.list.return_value = rows
    assert _both(SimpleNamespace(scan_decisions=mocked)) == (3, 3)


def test_missing_or_failing_repository_counts_zero(tmp_path, freeze, monkeypatch) -> None:
    freeze(NOW)
    assert _both(SimpleNamespace()) == (0, 0)
    assert _both(SimpleNamespace(scan_decisions=None)) == (0, 0)
    assert _both(SimpleNamespace(scan_decisions=SimpleNamespace(db=object()))) == (0, 0)

    repository = _repository(tmp_path)
    _insert(repository, [_row(created_at="2026-10-17T12:00:00+00:00", payload=_weak())])

    def broken_connect():
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(repository.db, "connect", broken_connect)
    assert _both(SimpleNamespace(scan_decisions=repository)) == (0, 0)


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
        for row in repository.list(limit=5000)
        if row.symbol == "NVDA"
    ]
    return {
        "attempts": [
            payload for event, payload in service.logs.items if event == "paper_supervised_weak_valid_promotion_attempt"
        ],
        "candidates": [(item.symbol, item.metadata.get("source")) for item in response.candidates],
        "written": written,
    }


@pytest.mark.parametrize("promoted_today", [0, 2, 4])
def test_scan_logs_the_same_daily_count_and_decision(tmp_path, freeze, monkeypatch, promoted_today) -> None:
    # End to end: the same prior rows, one scan counted by the original function and
    # one by the narrowed read. Logs (daily_count, blockers), the promotion decision
    # and the rows written must match -- including when today's promotions reach the
    # default daily limit of 4.
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
    narrowed = _scan_outcome(tmp_path / "narrowed", prior, monkeypatch, counter=_weak_valid_daily_count)

    assert narrowed == reference
    assert reference["attempts"] and all(item["daily_count"] == promoted_today for item in reference["attempts"])
    limited = "supervised_weak_valid_daily_limit_reached" in reference["attempts"][0]["promotion_blockers"]
    assert limited is (promoted_today >= 4)
    promoted = any(source == "supervised_weak_valid" for _symbol, source in reference["candidates"])
    assert promoted is (promoted_today < 4)
