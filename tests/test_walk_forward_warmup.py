"""Walk-forward folds must give strategies indicator warm-up.

Root cause of the empty backtest gate (found 2026-09-08): every fold ran the
engine on its ~10-bar test slice alone, below every strategy's indicator
warm-up, so 1.2M fold runs recorded zero trades. Folds now feed the train bars
as context and evaluate only the test window.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd

from app.backtesting.batch import _with_warmup
from app.backtesting.engine import BacktestEngine, EngineConfig
from app.backtesting.walk_forward import WalkForwardSplitter
from app.strategies import get_strategy


def _trending_history(days: int = 420) -> pd.DataFrame:
    """A noisy up-trend with periodic pullbacks so MA crossovers actually occur."""

    rng = np.random.default_rng(7)
    start = datetime(2025, 1, 2, tzinfo=timezone.utc)
    rows = []
    price = 100.0
    for i in range(days):
        drift = 0.15 if (i // 25) % 2 == 0 else -0.10
        price = max(1.0, price + drift + rng.normal(0.0, 0.8))
        rows.append(
            {
                "timestamp": start + timedelta(days=i),
                "open": price,
                "high": price + 0.6,
                "low": price - 0.6,
                "close": price,
                "volume": 1_000_000,
            }
        )
    return pd.DataFrame(rows)


def _engine() -> BacktestEngine:
    return BacktestEngine(config=EngineConfig(initial_cash=10_000.0))


def test_fold_without_warmup_cannot_trade_but_with_warmup_can() -> None:
    history = _trending_history()
    strategy = get_strategy("ma_crossover")
    splitter = WalkForwardSplitter(train_days=180, test_days=14, step_days=14, embargo_days=1, holdout_days=28)
    folds = list(splitter.split(history))
    assert folds, "fixture must yield folds"

    bare_trades = 0
    warm_trades = 0
    for window in folds:
        bare = _engine().run(symbol="SYN", strategy=strategy, data=window.test_df, file_path="bare")
        warm = _engine().run(
            symbol="SYN",
            strategy=strategy,
            data=_with_warmup(window),
            file_path="warm",
            trade_window_start=window.test_start,
        )
        bare_trades += int(bare.metrics["number_of_trades"])
        warm_trades += int(warm.metrics["number_of_trades"])
        # Evaluation stays inside the test window: no entry before test_start,
        # and the warm-up bars add no equity points (same bar count as the bare
        # run, which sees exactly the test bars).
        for trade in warm.trades:
            assert pd.Timestamp(trade["entry_time"]) >= pd.Timestamp(window.test_start)
        assert warm.metrics["bars_evaluated"] <= float(len(window.test_df))
        assert warm.metrics["bars_evaluated"] >= bare.metrics["bars_evaluated"] - 1.0

    assert bare_trades == 0  # the old behaviour: ~10 bars, below MA warm-up
    assert warm_trades > 0  # the fix: same folds now produce trades


def test_trade_window_start_excludes_warmup_from_equity_and_metrics() -> None:
    history = _trending_history(120)
    strategy = get_strategy("ma_crossover")
    cutoff = history["timestamp"].iloc[80]

    result = _engine().run(
        symbol="SYN", strategy=strategy, data=history, file_path="x", trade_window_start=cutoff
    )

    assert result.metrics["bars_evaluated"] == 40.0
    assert all(pd.Timestamp(t["entry_time"]) >= pd.Timestamp(cutoff) for t in result.trades)


def test_with_warmup_falls_back_to_test_frame_without_train() -> None:
    history = _trending_history(30)
    window = type("W", (), {"train_df": history.iloc[:0], "test_df": history})()

    assert len(_with_warmup(window)) == 30
