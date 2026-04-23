"""Walk-forward splitter tests.

The brief mandates a property test that generates random splits and asserts no
future timestamps appear in any training set. That is exactly what
``test_no_future_leakage_property`` does via ``hypothesis``.

If ``hypothesis`` is not installed the property test is skipped at collection
time so the rest of the suite keeps running; CI should install it.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

from app.backtesting.walk_forward import (
    WalkForwardSplitter,
    aggregate_out_of_sample,
    assert_no_future_leakage,
)

try:  # pragma: no cover - hypothesis optional in minimal envs
    from hypothesis import assume, given, settings, strategies as st
    HYPOTHESIS_AVAILABLE = True
except Exception:  # pragma: no cover - hypothesis optional in minimal envs
    HYPOTHESIS_AVAILABLE = False


def _synth(days: int, start: datetime | None = None) -> pd.DataFrame:
    start = start or datetime(2024, 1, 2, tzinfo=timezone.utc)
    rows = []
    for i in range(days):
        ts = start + timedelta(days=i)
        rows.append({"timestamp": ts, "close": 100.0 + i, "volume": 1_000})
    return pd.DataFrame(rows)


def test_basic_split_yields_strictly_ordered_windows() -> None:
    frame = _synth(days=400)
    splitter = WalkForwardSplitter(train_days=180, test_days=14, step_days=14, embargo_days=1, holdout_days=28)
    windows = list(splitter.split(frame))
    assert windows, "expected at least one fold"
    for window in windows:
        assert window.train_df["timestamp"].max() < window.test_df["timestamp"].min()
        assert_no_future_leakage(window)


def test_holdout_is_excluded_from_all_folds() -> None:
    frame = _synth(days=400)
    splitter = WalkForwardSplitter(holdout_days=30)
    holdout_start = pd.to_datetime(frame["timestamp"], utc=True).max() - pd.Timedelta(days=30)
    for window in splitter.split(frame):
        assert window.test_df["timestamp"].max() < holdout_start


def test_holdout_window_is_the_tail_of_the_dataset() -> None:
    frame = _synth(days=400)
    splitter = WalkForwardSplitter()
    holdout = splitter.holdout_window(frame)
    assert holdout is not None
    tail_end = pd.to_datetime(frame["timestamp"], utc=True).max()
    assert holdout.test_end == tail_end


def test_embargo_prevents_adjacency_leakage() -> None:
    frame = _synth(days=400)
    splitter = WalkForwardSplitter(embargo_days=5)
    for window in splitter.split(frame):
        gap = window.test_start - window.train_end
        assert gap >= pd.Timedelta(days=5)


def test_aggregate_merges_trade_lists_but_keeps_fold_metrics() -> None:
    per_fold_trades = [[{"pnl_usd": 10}, {"pnl_usd": -3}], [{"pnl_usd": 5}]]
    per_fold_metrics = [{"win_rate": 50.0}, {"win_rate": 100.0}]
    result = aggregate_out_of_sample(per_fold_trades, per_fold_metrics)
    assert len(result["merged_trades"]) == 3
    assert result["metrics"]["fold_count"] == 2


@pytest.mark.skipif(not HYPOTHESIS_AVAILABLE, reason="hypothesis is not installed")
def test_no_future_leakage_property() -> None:  # pragma: no branch
    """Randomly-parameterized splitter configs must never leak training data."""

    @given(
        total_days=st.integers(min_value=200, max_value=800),
        train_days=st.integers(min_value=30, max_value=200),
        test_days=st.integers(min_value=5, max_value=30),
        step_days=st.integers(min_value=1, max_value=30),
        embargo_days=st.integers(min_value=0, max_value=10),
        holdout_days=st.integers(min_value=7, max_value=60),
    )
    @settings(max_examples=40)
    def _prop(total_days, train_days, test_days, step_days, embargo_days, holdout_days):
        assume(train_days + test_days + embargo_days + holdout_days + step_days < total_days)
        frame = _synth(days=total_days)
        splitter = WalkForwardSplitter(
            train_days=train_days,
            test_days=test_days,
            step_days=step_days,
            embargo_days=embargo_days,
            holdout_days=holdout_days,
        )
        for window in splitter.split(frame):
            assert_no_future_leakage(window)
            # Additionally, no test bar may fall within the holdout.
            max_ts = pd.to_datetime(frame["timestamp"], utc=True).max()
            holdout_boundary = max_ts - pd.Timedelta(days=holdout_days)
            assert window.test_df["timestamp"].max() <= holdout_boundary

    _prop()
