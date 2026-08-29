"""Tests for the PEAD strategy (post-earnings drift) with an injected provider."""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.models.signal import SignalAction
from app.strategies import STRATEGY_REGISTRY, get_strategy
from app.strategies.pead import PEADStrategy


def _uptrend(n=80):
    ts = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    c = np.linspace(100, 140, n)
    return pd.DataFrame({"timestamp": ts, "open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": np.full(n, 1e6)})


def test_registered() -> None:
    assert "post_earnings_drift" in STRATEGY_REGISTRY
    assert isinstance(get_strategy("post_earnings_drift"), PEADStrategy)


def test_inert_without_provider() -> None:
    assert PEADStrategy().generate_signal(_uptrend(), "AAA") is None


def test_fires_on_positive_surprise_in_window_and_uptrend() -> None:
    strat = PEADStrategy(min_surprise_pct=5.0, drift_window_bars=15)
    strat.set_earnings_provider(lambda sym: {"surprise_pct": 12.0, "bars_since_report": 3})
    sig = strat.generate_signal(_uptrend(), "AAA")
    assert sig is not None
    assert sig.action == SignalAction.BUY
    assert sig.metadata["setup_type"] == "post_earnings_drift"
    assert sig.metadata["max_hold_bars"] == 12  # 15 - 3 remaining


def test_silent_on_small_surprise() -> None:
    strat = PEADStrategy(min_surprise_pct=5.0)
    strat.set_earnings_provider(lambda sym: {"surprise_pct": 1.0, "bars_since_report": 2})
    assert strat.generate_signal(_uptrend(), "AAA") is None


def test_silent_outside_drift_window() -> None:
    strat = PEADStrategy(min_surprise_pct=5.0, drift_window_bars=15)
    strat.set_earnings_provider(lambda sym: {"surprise_pct": 20.0, "bars_since_report": 40})
    assert strat.generate_signal(_uptrend(), "AAA") is None


def test_silent_in_downtrend_even_with_surprise() -> None:
    ts = pd.date_range("2024-01-01", periods=80, freq="D", tz="UTC")
    c = np.linspace(140, 100, 80)  # downtrend
    df = pd.DataFrame({"timestamp": ts, "open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": np.full(80, 1e6)})
    strat = PEADStrategy(min_surprise_pct=5.0)
    strat.set_earnings_provider(lambda sym: {"surprise_pct": 20.0, "bars_since_report": 2})
    assert strat.generate_signal(df, "AAA") is None
