"""Intraday (5m/15m) walk-forward: folds fit, positions close at the bell, and
the stop-width floor and variants are measured without touching the gate.

Found 2026-10-02: 16 of the first 18 live paper trades came from 5m/15m
strategies, yet no intraday walk-forward row had ever been produced -- intraday
runs fetched 350 bars (~4.5 sessions of 5m) against a ~223-day daily fold plan.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.backtesting.batch import BatchBacktestService
from app.backtesting.engine import BacktestEngine, EngineConfig
from app.backtesting.intraday import (
    floored_stop_and_target,
    last_bar_of_session,
    session_atr_by_bar,
)
from app.backtesting.intraday_plan import history_bars_for, intraday_variants, splitter_for
from app.backtesting.metrics import bars_per_year_for
from app.models.signal import Signal, SignalAction
from app.storage.db import Database
from app.storage.repositories import BacktestRepository, RunLogRepository
from tests.conftest import make_settings


def _sessions(days: int, *, minutes: int = 5, seed: int = 7) -> pd.DataFrame:
    """Regular-hours bars (13:30-20:00 UTC, i.e. 09:30-16:00 EDT) on weekdays."""

    rng = np.random.default_rng(seed)
    rows, price = [], 100.0
    for day in pd.bdate_range("2026-06-01", periods=days, tz="UTC"):
        for step in range(390 // minutes):
            ts = day + pd.Timedelta(hours=13, minutes=30 + step * minutes)
            change = rng.normal(0.0002, 0.002)
            open_, close = price, price * (1 + change)
            rows.append(
                {
                    "timestamp": ts,
                    "open": open_,
                    "high": max(open_, close) * 1.001,
                    "low": min(open_, close) * 0.999,
                    "close": close,
                    "volume": 10_000,
                }
            )
            price = close
    return pd.DataFrame(rows)


class EveryNthBarLong:
    """Buys every Nth bar with a tight 0.3% stop and 0.6% target."""

    name = "test_every_nth_long"

    def __init__(self, every: int = 20, stop_pct: float = 0.003, target_pct: float = 0.006) -> None:
        self.every, self.stop_pct, self.target_pct = every, stop_pct, target_pct

    def generate_signal(self, window: pd.DataFrame, symbol: str):
        if len(window) % self.every:
            return None
        price = float(window["close"].iloc[-1])
        return Signal(
            symbol=symbol,
            strategy_name=self.name,
            action=SignalAction.BUY,
            rationale="test",
            price=price,
            stop_loss=price * (1 - self.stop_pct),
            take_profit=price * (1 + self.target_pct),
        )


def test_intraday_history_and_folds_fit() -> None:
    settings = make_settings_stub()
    assert history_bars_for("5m", settings) == 6686  # ~86 sessions x 78 bars
    assert history_bars_for("1d", settings) == 500
    history = _sessions(86)
    folds = list(splitter_for("5m", settings).split(history))
    assert len(folds) >= 10, len(folds)
    assert splitter_for("1d", settings).train_days == 180  # daily plan unchanged


def test_session_close_never_carries_overnight() -> None:
    data = _sessions(6)
    config = EngineConfig(
        initial_cash=100_000.0, bars_per_year=bars_per_year_for("5m"), flatten_at_session_end=True
    )
    result = BacktestEngine(config=config).run(
        symbol="SYN",
        strategy=EveryNthBarLong(every=60, stop_pct=0.05, target_pct=0.1),
        data=data,
        file_path="t",
    )

    assert result.trades, "fixture must trade"
    for trade in result.trades:
        entry = pd.Timestamp(trade["entry_time"]).tz_convert("America/New_York").date()
        exit_ = pd.Timestamp(trade["exit_time"]).tz_convert("America/New_York").date()
        assert entry == exit_, trade
    assert any(t["reason"] == "session_close" for t in result.trades)


def test_session_atr_uses_prior_sessions_only() -> None:
    data = _sessions(20)
    atr = session_atr_by_bar(data)
    ends = last_bar_of_session(data["timestamp"])
    assert atr[0] is None  # first session has no history
    assert sum(ends) == 20
    first_with_atr = next(i for i, value in enumerate(atr) if value is not None)
    # Every bar of the same session shares one ATR value (no intraday look-ahead).
    session_of = (
        pd.to_datetime(data["timestamp"], utc=True).dt.tz_convert("America/New_York").dt.date
    )
    same_day = [atr[i] for i in range(len(atr)) if session_of[i] == session_of[first_with_atr]]
    assert len(set(same_day)) == 1


def test_stop_floor_widens_stop_and_keeps_reward_to_risk() -> None:
    stop, target = floored_stop_and_target(
        fill_price=100.0, signal_price=100.0, stop=99.7, target=100.6, min_distance=1.0
    )
    assert stop == 99.0 and round(target, 6) == 102.0  # 2:1 kept
    assert floored_stop_and_target(
        fill_price=100.0, signal_price=100.0, stop=98.0, target=104.0, min_distance=1.0
    ) == (98.0, 104.0)


def test_variants_are_logged_not_persisted(tmp_path) -> None:
    settings = make_settings(
        tmp_path, walk_forward_intraday_variants=["hold_overnight", "stop_floor_0.5"]
    )
    db = Database(settings)
    db.initialize()
    repo, logs = BacktestRepository(db), RunLogRepository(db)
    service = BatchBacktestService(
        settings=settings,
        market_data_engine=None,
        backtest_repository=repo,
        run_log_repository=logs,
    )
    config = EngineConfig(
        initial_cash=100_000.0, bars_per_year=bars_per_year_for("5m"), flatten_at_session_end=True
    )
    strategy = EveryNthBarLong()
    history = _sessions(60)

    baseline = service._run_strategy(
        engine=BacktestEngine(repo, config=config),
        symbol="SYN",
        strategy=strategy,
        history=history,
        timeframe="5m",
        provider="test",
        walk_forward=True,
    )
    service._run_variants(config, "SYN", strategy, history, "5m", "test", baseline)

    with db.connect() as connection:
        paths = [
            r["file_path"] for r in connection.execute("SELECT file_path FROM backtests").fetchall()
        ]
        events = [
            r["payload_json"]
            for r in connection.execute(
                "SELECT payload_json FROM run_logs WHERE event_type='backtest_variant_result'"
            ).fetchall()
        ]
    assert baseline["fold_count"] >= 5 and baseline["number_of_trades"] > 0
    assert paths == ["test:5m:SYN:walk_forward_oos"]
    assert len(events) == 2 and '"hold_overnight"' in events[0] and '"stop_floor_0.5"' in events[1]


def test_variant_names_parse() -> None:
    assert intraday_variants(
        make_settings_stub(variants=["hold_overnight", "stop_floor_1.0", "bogus", "stop_floor_x"])
    ) == [
        ("hold_overnight", {"flatten_at_session_end": False}),
        ("stop_floor_1.0", {"min_stop_session_atr_multiple": 1.0}),
    ]


def make_settings_stub(variants=None):
    from types import SimpleNamespace

    return SimpleNamespace(
        walk_forward_intraday_lookback_days=120, walk_forward_intraday_variants=variants or []
    )
