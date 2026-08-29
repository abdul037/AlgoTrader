"""Connors RSI(2) mean-reversion: fires on a sharp oversold dip inside an uptrend,
stays silent in downtrends and when not oversold."""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.models.signal import SignalAction
from app.strategies import STRATEGY_REGISTRY, get_strategy
from app.strategies.connors_rsi2_reversion import ConnorsRSI2ReversionStrategy


def _frame(closes: list[float]) -> pd.DataFrame:
    n = len(closes)
    ts = pd.date_range("2025-01-01", periods=n, freq="D", tz="UTC")
    close = np.array(closes, dtype="float64")
    return pd.DataFrame(
        {
            "timestamp": ts,
            "open": close,
            "high": close * 1.005,
            "low": close * 0.995,
            "close": close,
            "volume": np.full(n, 1_000_000.0),
        }
    )


def _uptrend_with_final_dip() -> pd.DataFrame:
    # 208 bars rising 100 -> ~200 (so ema_200 lags well below), then a sharp 2-bar
    # drop that drives RSI(2) toward zero while price stays above the 200 EMA.
    base = list(np.linspace(100, 200, 208))
    base += [base[-1] * 0.95, base[-1] * 0.90]
    return _frame(base)


def test_registered() -> None:
    assert "connors_rsi2_reversion" in STRATEGY_REGISTRY
    assert isinstance(get_strategy("connors_rsi2_reversion"), ConnorsRSI2ReversionStrategy)


def test_fires_long_on_oversold_dip_in_uptrend() -> None:
    sig = ConnorsRSI2ReversionStrategy().generate_signal(_uptrend_with_final_dip(), "TEST")
    assert sig is not None
    assert sig.action == SignalAction.BUY
    assert sig.stop_loss < sig.price < sig.take_profit
    assert sig.metadata["rsi_2"] <= 10.0
    assert sig.metadata["setup_type"] == "connors_rsi2_reversion"


def test_silent_when_not_oversold() -> None:
    # Steady uptrend, no dip -> RSI(2) stays high -> no signal.
    steady = _frame(list(np.linspace(100, 200, 210)))
    assert ConnorsRSI2ReversionStrategy().generate_signal(steady, "TEST") is None


def test_silent_in_downtrend_even_if_oversold() -> None:
    # Falling series: a dip is oversold but price is below the 200 EMA -> filtered out.
    down = list(np.linspace(200, 100, 208)) + [100 * 0.95, 100 * 0.90]
    assert ConnorsRSI2ReversionStrategy().generate_signal(_frame(down), "TEST") is None


def test_needs_enough_bars() -> None:
    assert ConnorsRSI2ReversionStrategy().generate_signal(_frame([100.0] * 50), "TEST") is None
