"""Tests for the pairs / stat-arb spread math and strategy."""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.models.signal import SignalAction
from app.strategies import STRATEGY_REGISTRY, get_strategy
from app.strategies.pairs import PairsTradingStrategy, compute_pair_spread


def _frame(closes):
    n = len(closes)
    ts = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    c = np.asarray(closes, dtype="float64")
    return pd.DataFrame({"timestamp": ts, "open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": np.full(n, 1e6)})


def test_registered() -> None:
    assert "pairs_stat_arb" in STRATEGY_REGISTRY
    assert isinstance(get_strategy("pairs_stat_arb"), PairsTradingStrategy)


def test_compute_pair_spread_on_cointegrated_series() -> None:
    rng = np.random.default_rng(1)
    base = np.cumsum(rng.normal(0, 1, 120)) + 100
    hedge = base + rng.normal(0, 0.5, 120)          # tracks base closely
    primary = base.copy()
    primary[-1] -= 8.0                               # primary suddenly cheap vs hedge
    pair = compute_pair_spread(_frame(primary), _frame(hedge), lookback=60)
    assert pair is not None
    assert pair.correlation > 0.6
    assert pair.zscore < -1.5                        # stretched cheap


def test_none_when_insufficient_overlap() -> None:
    assert compute_pair_spread(_frame([1.0] * 10), _frame([1.0] * 10), lookback=60) is None


class TestPairsStrategy:
    def test_inert_without_hedge_provider(self) -> None:
        strat = PairsTradingStrategy(hedge_symbol="SPY")
        assert strat.generate_signal(_frame(list(np.linspace(100, 120, 100))), "AAA") is None

    def test_fires_long_when_primary_cheap_vs_hedge(self) -> None:
        rng = np.random.default_rng(7)
        base = np.cumsum(rng.normal(0, 1, 140)) + 200
        hedge = base + rng.normal(0, 0.4, 140)
        primary = base.copy()
        primary[-1] -= 12.0  # sharp cheapening -> spread z well below -entry_z
        hedge_frame = _frame(hedge)

        strat = PairsTradingStrategy(hedge_symbol="HEDGE", lookback=60, entry_z=2.0, min_correlation=0.5)
        strat.set_hedge_provider(lambda sym, bars: hedge_frame)
        sig = strat.generate_signal(_frame(primary), "PRIMARY")
        assert sig is not None
        assert sig.action == SignalAction.BUY
        assert sig.stop_loss < sig.price < sig.take_profit
        assert sig.metadata["setup_type"] == "pairs_stat_arb"
        assert sig.metadata["spread_zscore"] < -2.0

    def test_silent_when_spread_not_stretched(self) -> None:
        rng = np.random.default_rng(3)
        base = np.cumsum(rng.normal(0, 1, 140)) + 150
        hedge = base + rng.normal(0, 0.4, 140)
        hedge_frame = _frame(hedge)
        strat = PairsTradingStrategy(hedge_symbol="HEDGE", lookback=60, entry_z=2.0, min_correlation=0.5)
        strat.set_hedge_provider(lambda sym, bars: hedge_frame)
        # primary == base, spread near mean -> no signal
        assert strat.generate_signal(_frame(base), "PRIMARY") is None

    def test_never_pairs_symbol_with_itself(self) -> None:
        strat = PairsTradingStrategy(hedge_symbol="AAA")  # scanned symbol == hedge
        strat.set_hedge_provider(lambda sym, bars: _frame([1.0] * 100))
        assert strat.generate_signal(_frame(list(np.linspace(100, 90, 100))), "AAA") is None
