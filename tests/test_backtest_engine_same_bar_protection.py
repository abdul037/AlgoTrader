"""Explicit tests for BacktestEngine's no-same-bar-fill guarantee."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from app.backtesting.cost_model import CostModel, is_extended_hours
from app.backtesting.engine import BacktestEngine, EngineConfig
from app.backtesting.metrics import HOURLY_BARS_PER_YEAR
from app.models.signal import Signal, SignalAction


class _BuyOnBarStrategy:
    name = "buy_on_bar"

    def __init__(self, signal_bar_index: int):
        self.signal_bar_index = signal_bar_index
        self.calls = 0

    def generate_signal(self, data: pd.DataFrame, symbol: str):
        bar_index = len(data) - 1
        self.calls += 1
        if bar_index != self.signal_bar_index:
            return None
        last = data.iloc[-1]
        return Signal(
            symbol=symbol,
            strategy_name=self.name,
            action=SignalAction.BUY,
            rationale="same-bar-fill-test",
            confidence=0.9,
            price=float(last["close"]),
        )


def _frame_with_prices(
    prices: list[tuple[float, float, float, float]],
    *,
    start: datetime | None = None,
    step: timedelta = timedelta(days=1),
) -> pd.DataFrame:
    start = start or datetime(2026, 1, 5, 14, 30, tzinfo=timezone.utc)
    rows = []
    for index, (open_, high, low, close) in enumerate(prices):
        rows.append(
            {
                "timestamp": start + step * index,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": 1_000_000,
            }
        )
    return pd.DataFrame(rows)


def _run(strategy: _BuyOnBarStrategy, frame: pd.DataFrame, config: EngineConfig):
    engine = BacktestEngine(config=config)
    return engine.run(symbol="TEST", strategy=strategy, data=frame, file_path="same_bar_test")


def test_signal_at_bar_N_fills_at_bar_N_plus_1_open() -> None:
    cost_model = CostModel()
    frame = _frame_with_prices(
        [
            (99.0, 101.0, 98.0, 100.0),
            (100.5, 102.0, 100.0, 101.0),
            (103.0, 104.0, 102.5, 103.5),
            (104.0, 105.0, 103.5, 104.5),
        ]
    )

    result = _run(
        _BuyOnBarStrategy(signal_bar_index=1),
        frame,
        EngineConfig(initial_cash=10_000.0, risk_per_trade_pct=None, cost_model=cost_model),
    )

    assert result.trades, "expected entry to be force-closed at end of data"
    trade = result.trades[0]
    expected_entry_time = pd.Timestamp(frame.loc[2, "timestamp"])
    expected_entry_price = cost_model.entry_fill_price(
        float(frame.loc[2, "open"]),
        side="buy",
        extended_hours=False,
    )
    assert pd.Timestamp(trade["entry_time"]) == expected_entry_time
    assert trade["entry_price"] == pytest.approx(expected_entry_price)


def test_signal_at_last_bar_is_dropped() -> None:
    frame = _frame_with_prices(
        [
            (99.0, 101.0, 98.0, 100.0),
            (101.0, 102.0, 100.5, 101.5),
            (102.0, 103.0, 101.5, 102.5),
        ]
    )

    result = _run(
        _BuyOnBarStrategy(signal_bar_index=2),
        frame,
        EngineConfig(initial_cash=10_000.0, risk_per_trade_pct=None, cost_model=CostModel()),
    )

    assert result.trades == []
    assert result.metrics["number_of_trades"] == 0


def test_fill_price_never_equals_signal_bar_close() -> None:
    cost_model = CostModel()
    frame = _frame_with_prices(
        [
            (99.0, 101.0, 98.0, 100.0),
            (100.0, 101.0, 99.5, 100.0),
            (110.0, 112.0, 109.0, 111.0),
            (111.0, 113.0, 110.0, 112.0),
        ]
    )

    result = _run(
        _BuyOnBarStrategy(signal_bar_index=1),
        frame,
        EngineConfig(initial_cash=10_000.0, risk_per_trade_pct=None, cost_model=cost_model),
    )

    assert result.trades, "expected entry to be force-closed at end of data"
    trade = result.trades[0]
    signal_bar_close = float(frame.loc[1, "close"])
    expected_entry_price = cost_model.entry_fill_price(
        float(frame.loc[2, "open"]),
        side="buy",
        extended_hours=False,
    )
    assert trade["entry_price"] != pytest.approx(signal_bar_close)
    assert abs(trade["entry_price"] - expected_entry_price) <= expected_entry_price * 0.0001


def test_extended_hours_disabled_drops_signal_when_next_bar_is_pre_market() -> None:
    cost_model = CostModel()
    frame = _frame_with_prices(
        [
            (99.0, 101.0, 98.0, 100.0),
            (101.0, 102.0, 100.5, 101.5),
            (102.0, 103.0, 101.5, 102.5),
        ],
        start=datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc),
        step=timedelta(hours=1),
    )

    assert is_extended_hours(frame.loc[1, "timestamp"])
    result = _run(
        _BuyOnBarStrategy(signal_bar_index=0),
        frame,
        EngineConfig(
            initial_cash=10_000.0,
            risk_per_trade_pct=None,
            cost_model=cost_model,
            bars_per_year=HOURLY_BARS_PER_YEAR,
            allow_extended_hours=False,
        ),
    )

    assert result.trades == []
    assert result.metrics["number_of_trades"] == 0
