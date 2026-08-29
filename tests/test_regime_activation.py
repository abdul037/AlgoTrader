"""Regime router activation: the market-only regime signal and the screener's
memoized, flag-gated regime accessor."""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.intelligence.market_regime import MarketIntelligenceService
from app.screener.regime_router import RegimeSignal


def _uptrend_frame(n=180, start=300, end=420):
    ts = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    close = np.linspace(start, end, n)
    return pd.DataFrame({"timestamp": ts, "open": close, "high": close * 1.01,
                         "low": close * 0.99, "close": close, "volume": np.full(n, 1e6)})


class _MarketData:
    def __init__(self, frame):
        self._frame = frame
    def get_history(self, symbol, *, timeframe, bars, force_refresh=False):
        return self._frame


class _Settings:
    # Minimal settings surface MarketIntelligenceService reads.
    def __getattr__(self, name):
        return None


def test_market_regime_signal_returns_a_signal_in_uptrend() -> None:
    svc = MarketIntelligenceService(_Settings(), _MarketData(_uptrend_frame()))
    sig = svc.market_regime_signal()
    assert isinstance(sig, RegimeSignal)
    assert sig.trend in {"up", "neutral", "down"}
    assert sig.volatility in {"low", "normal", "high"}


def test_market_regime_signal_none_when_no_history() -> None:
    class _NoData:
        def get_history(self, *a, **k):
            raise RuntimeError("no data")
    svc = MarketIntelligenceService(_Settings(), _NoData())
    assert svc.market_regime_signal() is None


class _FakeIntelligence:
    def __init__(self):
        self.calls = 0
    def market_regime_signal(self, *, force_refresh=False):
        self.calls += 1
        return RegimeSignal(trend="down")


def _screener_stub(enabled: bool):
    """Bind the real _current_regime to a stub with just the fields it reads."""
    from app.screener.service import MarketScreenerService
    from types import SimpleNamespace

    obj = SimpleNamespace()
    obj.settings = SimpleNamespace(regime_router_enabled=enabled, regime_router_cache_seconds=300.0)
    obj.intelligence = _FakeIntelligence()
    obj._regime_cache = None
    obj._current_regime = MarketScreenerService._current_regime.__get__(obj)
    return obj


def test_current_regime_disabled_returns_none_without_calling_intelligence() -> None:
    obj = _screener_stub(enabled=False)
    assert obj._current_regime() is None
    assert obj.intelligence.calls == 0


def test_current_regime_enabled_computes_once_and_caches() -> None:
    obj = _screener_stub(enabled=True)
    first = obj._current_regime()
    second = obj._current_regime()
    assert isinstance(first, RegimeSignal) and first.trend == "down"
    assert second is first
    assert obj.intelligence.calls == 1  # cached within the TTL window
