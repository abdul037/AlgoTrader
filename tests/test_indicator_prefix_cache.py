"""Indicators computed once per backtest must equal per-bar recomputation.

The engine slices one full-frame enrichment instead of recomputing ~40
indicators on every bar (a 5m walk-forward took 158 s per symbol/strategy).
That is only valid if every indicator is causal; these tests pin it, including
the opening range, which used to be filled for bars 1-4 from bars 2-5.
"""

from __future__ import annotations

import contextlib

import numpy as np
import pandas as pd
import pytest

import app.backtesting.engine as engine_module
from app.backtesting.engine import BacktestEngine, EngineConfig
from app.backtesting.metrics import bars_per_year_for
from app.indicators import _enrich, enrich_technical_indicators, precomputed_indicators
from app.strategies import get_strategy
from tests.test_intraday_walk_forward import _sessions


def _assert_same(a: pd.DataFrame, b: pd.DataFrame) -> None:
    assert list(a.columns) == list(b.columns)
    for column in a.columns:
        left, right = a[column].reset_index(drop=True), b[column].reset_index(drop=True)
        if pd.api.types.is_numeric_dtype(left):
            np.testing.assert_allclose(
                left.to_numpy(float), right.to_numpy(float), rtol=1e-12, atol=1e-12, equal_nan=True
            )
        else:
            assert left.equals(right), column


@pytest.mark.parametrize("timeframe", ["5m", "1d"])
def test_every_prefix_matches_fresh_computation(timeframe: str) -> None:
    data = _sessions(4)
    with precomputed_indicators(data):
        for rows in (1, 3, 5, 6, 77, 78, 79, 150, len(data)):
            cached = enrich_technical_indicators(data.iloc[:rows], timeframe=timeframe)
            _assert_same(cached, _enrich(data.iloc[:rows], timeframe=timeframe))


def test_non_prefix_frames_fall_back_to_fresh_computation() -> None:
    data = _sessions(3)
    other = _sessions(3, seed=11)
    with precomputed_indicators(data):
        _assert_same(
            enrich_technical_indicators(other.iloc[:100], timeframe="5m"),
            _enrich(other.iloc[:100], timeframe="5m"),
        )


@pytest.mark.parametrize(
    "name",
    [
        "vwap_reclaim",
        "anchored_vwap_pullback_continuation",
        "opening_range_breakout_retest",
        "rsi_reversal",
        "intraday_vwap_trend",
    ],
)
def test_backtest_trades_identical_with_and_without_cache(name: str, monkeypatch) -> None:
    data = _sessions(10)
    config = EngineConfig(
        initial_cash=100_000.0, bars_per_year=bars_per_year_for("15m"), flatten_at_session_end=True
    )

    cached = BacktestEngine(config=config).run(
        symbol="SYN", strategy=get_strategy(name), data=data, file_path="t"
    )
    monkeypatch.setattr(
        engine_module, "precomputed_indicators", lambda _frame: contextlib.nullcontext()
    )
    fresh = BacktestEngine(config=config).run(
        symbol="SYN", strategy=get_strategy(name), data=data, file_path="t"
    )

    assert [(t["entry_time"], t["exit_time"], t["reason"]) for t in cached.trades] == [
        (t["entry_time"], t["exit_time"], t["reason"]) for t in fresh.trades
    ]
    assert cached.metrics == fresh.metrics
