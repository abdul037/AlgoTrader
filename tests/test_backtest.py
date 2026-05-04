"""Tests for the audited backtest engine and metrics."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from app.backtesting.cost_model import CostModel, zero_cost_model
from app.backtesting.engine import BacktestEngine, EngineConfig
from app.backtesting.metrics import (
    bars_per_year_for,
    leakage_tripwire_triggered,
    summarize_trades,
)
from app.data.market_data import MarketDataService
from app.models.signal import Signal, SignalAction
from app.storage.db import Database
from app.storage.repositories import BacktestRepository
from app.strategies.ma_crossover import MACrossoverStrategy
from tests.conftest import make_settings


# ---------------------------------------------------------------------------
# End-to-end sanity
# ---------------------------------------------------------------------------


def test_backtest_metrics_are_sane(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings)
    database.initialize()

    csv_path = Path(__file__).resolve().parents[1] / "sample_data" / "nvda.csv"
    data = MarketDataService().load_csv(csv_path)
    engine = BacktestEngine(
        BacktestRepository(database),
        config=EngineConfig(initial_cash=10_000.0, risk_per_trade_pct=1.0, cost_model=zero_cost_model()),
    )
    result = engine.run(
        symbol="NVDA",
        strategy=MACrossoverStrategy(),
        data=data,
        file_path=str(csv_path),
    )

    assert result.metrics["number_of_trades"] >= 1
    assert result.metrics["max_drawdown_pct"] >= 0
    assert "sharpe_like" in result.metrics
    assert result.ending_cash > 0


def test_profit_factor_returns_inf_when_no_losers() -> None:
    summary = summarize_trades([
        {"pnl_usd": 100.0},
        {"pnl_usd": 50.0},
    ])
    assert math.isinf(summary["profit_factor"])


def test_leakage_tripwire_does_not_fire_on_small_samples() -> None:
    triggered, reason = leakage_tripwire_triggered({
        "profit_factor": math.inf,
        "win_rate": 100.0,
        "number_of_trades": 2,
    })
    assert triggered is False
    assert reason is None


def test_leakage_tripwire_fires_when_large_sample_exceeds_limits() -> None:
    triggered, reason = leakage_tripwire_triggered({
        "profit_factor": 3.4,
        "win_rate": 62.0,
        "number_of_trades": 50,
    })
    assert triggered is True
    assert reason is not None and "profit_factor" in reason


# ---------------------------------------------------------------------------
# No same-bar fill: signal on bar N must fill at bar N+1's open.
# ---------------------------------------------------------------------------


class _BuyAtFirstSignalStrategy:
    """Minimal stub strategy that buys on the first bar and then does nothing."""

    name = "buy_at_first"
    required_bars = 1
    _fired = False

    def generate_signal(self, data: pd.DataFrame, symbol: str):
        if self._fired:
            return None
        self._fired = True
        last = data.iloc[-1]
        return Signal(
            symbol=symbol,
            strategy_name=self.name,
            action=SignalAction.BUY,
            rationale="test",
            confidence=0.9,
            price=float(last["close"]),
            stop_loss=float(last["close"]) * 0.95,
            take_profit=float(last["close"]) * 1.05,
        )


def _synthetic_frame(bars: int = 10, start: datetime | None = None) -> pd.DataFrame:
    start = start or datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
    rows = []
    price = 100.0
    for i in range(bars):
        ts = start + timedelta(days=i)
        open_ = price
        close = price + 0.5  # monotonic uptrend so the strategy gets a real fill path
        high = close + 0.3
        low = open_ - 0.3
        rows.append(
            {"timestamp": ts, "open": open_, "high": high, "low": low, "close": close, "volume": 1_000_000}
        )
        price = close + 0.5
    return pd.DataFrame(rows)


def test_next_bar_open_fill_not_same_bar_close(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings)
    database.initialize()

    frame = _synthetic_frame(bars=6)
    engine = BacktestEngine(
        BacktestRepository(database),
        config=EngineConfig(initial_cash=10_000.0, cost_model=zero_cost_model(), risk_per_trade_pct=None),
    )
    strategy = _BuyAtFirstSignalStrategy()
    result = engine.run(symbol="TEST", strategy=strategy, data=frame, file_path="test")

    assert result.metrics["number_of_trades"] >= 0
    trade = result.trades[0]
    # The signal fires on bar 0 (close = 100.5); the audited engine must fill at
    # bar 1's open (101.0), not bar 0's close.
    assert trade["entry_price"] == 101.0


# ---------------------------------------------------------------------------
# Cost model integration
# ---------------------------------------------------------------------------


def test_cost_model_reduces_realized_pnl(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings)
    database.initialize()
    frame = _synthetic_frame(bars=12)

    def _engine(cost_model: CostModel) -> BacktestEngine:
        strategy = _BuyAtFirstSignalStrategy()
        return BacktestEngine(
            BacktestRepository(database),
            config=EngineConfig(
                initial_cash=10_000.0,
                cost_model=cost_model,
                risk_per_trade_pct=None,
            ),
        ), strategy

    gross_engine, strategy_gross = _engine(zero_cost_model())
    gross = gross_engine.run(symbol="TEST", strategy=strategy_gross, data=frame, file_path="test_gross")
    cost_engine, strategy_net = _engine(CostModel(spread_bps=30.0, overnight_fee_daily_pct=0.001, min_position_usd=10.0))
    net = cost_engine.run(symbol="TEST", strategy=strategy_net, data=frame, file_path="test_net")

    assert net.ending_cash < gross.ending_cash
    assert net.cost_breakdown["total_cost_usd"] > 0


# ---------------------------------------------------------------------------
# Gap-through stop handling
# ---------------------------------------------------------------------------


class _LongThenGapStrategy:
    """Buy once on bar 0; let the engine handle a gap-through stop on bar 2."""

    name = "long_then_gap"
    required_bars = 1
    _fired = False

    def generate_signal(self, data: pd.DataFrame, symbol: str):
        if self._fired:
            return None
        self._fired = True
        last = data.iloc[-1]
        return Signal(
            symbol=symbol,
            strategy_name=self.name,
            action=SignalAction.BUY,
            rationale="test",
            confidence=0.9,
            price=float(last["close"]),
            stop_loss=95.0,
            take_profit=120.0,
        )


def test_stop_fill_uses_worse_of_stop_and_open(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings)
    database.initialize()
    # Bar 0 close 100, bar 1 open 101 (fill), bar 2 gap-down open 90 with low 88 — stop at 95.
    rows = [
        {"timestamp": datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc), "open": 99.0, "high": 101.0, "low": 98.5, "close": 100.0, "volume": 1_000_000},
        {"timestamp": datetime(2026, 1, 6, 14, 30, tzinfo=timezone.utc), "open": 101.0, "high": 102.0, "low": 100.5, "close": 101.5, "volume": 1_000_000},
        {"timestamp": datetime(2026, 1, 7, 14, 30, tzinfo=timezone.utc), "open": 90.0, "high": 92.0, "low": 88.0, "close": 89.0, "volume": 1_000_000},
        {"timestamp": datetime(2026, 1, 8, 14, 30, tzinfo=timezone.utc), "open": 89.5, "high": 90.5, "low": 89.0, "close": 89.5, "volume": 1_000_000},
    ]
    frame = pd.DataFrame(rows)
    engine = BacktestEngine(
        BacktestRepository(database),
        config=EngineConfig(initial_cash=10_000.0, cost_model=zero_cost_model(), risk_per_trade_pct=None),
    )
    strategy = _LongThenGapStrategy()
    result = engine.run(symbol="TEST", strategy=strategy, data=frame, file_path="test_gap")

    assert result.trades, "expected a trade to have been stopped out"
    trade = result.trades[0]
    assert trade["reason"] == "stop_loss"
    # Gap-through: fill is the MIN of stop (95) and bar open (90) = 90.
    assert trade["exit_price"] <= 90.0, f"expected gap-through fill below stop, got {trade['exit_price']}"


# ---------------------------------------------------------------------------
# Bar-frequency aware annualization
# ---------------------------------------------------------------------------


def test_bars_per_year_mapping_for_known_timeframes() -> None:
    assert bars_per_year_for("1d") == 252
    assert bars_per_year_for("1h") == 1638
    assert bars_per_year_for("15m") == 6552
    assert bars_per_year_for("5m") == 19656
    assert bars_per_year_for("1m") == 98280
    assert bars_per_year_for("1w") == 52
    assert bars_per_year_for("unknown") == 252  # safe default
