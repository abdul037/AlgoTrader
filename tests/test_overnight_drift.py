"""Tests for the engine time-stop (max_hold_bars) and the overnight-drift strategy."""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.backtesting.engine import BacktestEngine, EngineConfig
from app.models.signal import Signal, SignalAction
from app.strategies import STRATEGY_REGISTRY, get_strategy
from app.strategies.overnight_drift import OvernightDriftStrategy


def _frame(closes):
    n = len(closes)
    ts = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    c = np.asarray(closes, dtype="float64")
    return pd.DataFrame({"timestamp": ts, "open": c, "high": c * 1.02, "low": c * 0.98, "close": c, "volume": np.full(n, 1e6)})


class _OneShotStrategy:
    """Emits a single buy on the first bar, with a 1-bar time-stop."""

    name = "oneshot"
    required_bars = 1

    def __init__(self):
        self._fired = False

    def generate_signal(self, data, symbol):
        if self._fired or len(data) < 2:
            return None
        self._fired = True
        price = float(data.iloc[-1]["close"])
        return Signal(
            symbol=symbol, strategy_name=self.name, action=SignalAction.BUY,
            rationale="t", price=price, stop_loss=price * 0.9, take_profit=price * 1.5,
            metadata={"max_hold_bars": 1},
        )


def test_engine_time_stop_closes_after_max_hold_bars() -> None:
    # Gentle uptrend so neither the wide stop nor target triggers first.
    data = _frame(list(np.linspace(100, 110, 12)))
    eng = BacktestEngine(config=EngineConfig(initial_cash=100_000, risk_per_trade_pct=1.0))
    res = eng.run(symbol="TEST", strategy=_OneShotStrategy(), data=data, file_path="oneshot:1d:test")
    assert res.trades, "expected one trade"
    assert res.trades[0]["reason"] == "time_stop"


class TestOvernightStrategy:
    def test_registered(self) -> None:
        assert "overnight_drift" in STRATEGY_REGISTRY
        assert isinstance(get_strategy("overnight_drift"), OvernightDriftStrategy)

    def test_fires_on_firm_close_in_uptrend(self) -> None:
        # 210 rising bars; make the final bar close near its high and up on the day.
        closes = list(np.linspace(100, 200, 210))
        df = _frame(closes)
        # Force the last bar: open below close, close == high (close_location ~1).
        df.loc[df.index[-1], "open"] = closes[-1] * 0.99
        df.loc[df.index[-1], "high"] = closes[-1]
        df.loc[df.index[-1], "low"] = closes[-1] * 0.985
        sig = OvernightDriftStrategy().generate_signal(df, "TEST")
        assert sig is not None
        assert sig.action == SignalAction.BUY
        assert sig.metadata["max_hold_bars"] == 1
        assert sig.metadata["setup_type"] == "overnight_drift"

    def test_silent_in_downtrend(self) -> None:
        closes = list(np.linspace(200, 100, 210))
        assert OvernightDriftStrategy().generate_signal(_frame(closes), "TEST") is None
