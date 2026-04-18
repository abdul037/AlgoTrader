from __future__ import annotations

import pandas as pd

from app.models.signal import SignalAction
from app.strategies.gold_momentum import GoldMomentumStrategy
from app.strategies.ma_crossover import MACrossoverStrategy


def make_frame(prices: list[float]) -> pd.DataFrame:
    timestamps = pd.date_range("2025-01-01", periods=len(prices), tz="UTC", freq="D")
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": prices,
            "high": [price + 1 for price in prices],
            "low": [price - 1 for price in prices],
            "close": prices,
            "volume": [1_000_000 for _ in prices],
        }
    )


def test_ma_crossover_emits_buy_signal() -> None:
    strategy = MACrossoverStrategy(fast_window=3, slow_window=5)
    prices = [10, 10, 10, 10, 9, 9, 10, 12]
    signal = strategy.generate_signal(make_frame(prices), "NVDA")
    assert signal is not None
    assert signal.action == SignalAction.BUY
    assert signal.stop_loss is not None


def test_gold_momentum_emits_buy_signal() -> None:
    strategy = GoldMomentumStrategy(breakout_window=5, trend_window=7)
    prices = [100, 101, 102, 101, 103, 104, 105, 104, 106, 107, 109, 111]
    signal = strategy.generate_signal(make_frame(prices), "GOLD")
    assert signal is not None
    assert signal.action == SignalAction.BUY
    assert signal.price == prices[-1]
