"""The batch backtester must score the sealed holdout and let it gate promotion,
and must surface cost drag into the audit rankings."""

from __future__ import annotations

import numpy as np
import pandas as pd

from app.backtesting.batch import BatchBacktestService, _promotion_hint
from app.backtesting.engine import BacktestEngine, EngineConfig
from app.strategies import get_strategy


def _strong_item(**overrides):
    item = {
        "strategy_name": "s",
        "timeframe": "1d",
        "number_of_trades": 120,
        "expectancy_usd": 5.0,
        "profit_factor": 1.4,
        "sharpe_like": 1.2,
        "max_drawdown_pct": 6.0,
        "total_return_pct": 9.0,
        "cost_drag_pct": 0.02,
    }
    item.update(overrides)
    return item


def test_negative_holdout_blocks_promotion() -> None:
    # Strong walk-forward numbers, but it lost on the sealed holdout.
    rankings = BatchBacktestService._audit_rankings(
        [_strong_item(holdout_evaluated=True, holdout_return_pct=-3.0)], []
    )
    assert rankings[0]["promotion_hint"] == "blocked_holdout_negative"


def test_positive_holdout_allows_paper_candidate() -> None:
    rankings = BatchBacktestService._audit_rankings(
        [_strong_item(holdout_evaluated=True, holdout_return_pct=2.5)], []
    )
    assert rankings[0]["promotion_hint"] == "paper_candidate"
    assert rankings[0]["average_holdout_return_pct"] == 2.5
    assert rankings[0]["holdout_evaluated"] is True


def test_cost_drag_surfaced_in_rankings() -> None:
    rankings = BatchBacktestService._audit_rankings([_strong_item(cost_drag_pct=0.05)], [])
    assert rankings[0]["average_cost_drag_pct"] == 0.05


def test_promotion_hint_holdout_gate_direct() -> None:
    base = dict(total_trades=150, expectancy=5.0, profit_factor=1.4, drawdown=6.0, leakage_warning_count=0)
    assert _promotion_hint(**base) == "paper_candidate"
    assert _promotion_hint(**base, holdout_evaluated=True, holdout_return_pct=1.0) == "paper_candidate"
    assert _promotion_hint(**base, holdout_evaluated=True, holdout_return_pct=-0.5) == "blocked_holdout_negative"
    # Not evaluated -> gate is inert.
    assert _promotion_hint(**base, holdout_evaluated=False, holdout_return_pct=-9.0) == "paper_candidate"


def test_run_strategy_evaluates_holdout_and_cost_drag(tmp_path) -> None:
    # ~420 daily bars so there are walk-forward folds AND a sealed holdout window.
    from types import SimpleNamespace

    n = 420
    ts = pd.date_range("2024-01-01", periods=n, freq="D", tz="UTC")
    rng = np.random.default_rng(3)
    close = 100 + np.cumsum(rng.normal(0.05, 1.0, n)) + np.linspace(0, 40, n)
    for i in range(30, n, 25):  # periodic dips for the mean-reversion strategy
        close[i] *= 0.95
    history = pd.DataFrame(
        {"timestamp": ts, "open": close, "high": close * 1.01, "low": close * 0.99,
         "close": close, "volume": np.full(n, 1e6)}
    )
    settings = SimpleNamespace(
        max_risk_per_trade_pct=1.0, primary_market_data_provider="test",
        walk_forward_train_days=180, walk_forward_test_days=14, walk_forward_step_days=14,
        walk_forward_embargo_days=1, walk_forward_holdout_days=28,
    )
    service = BatchBacktestService(
        settings=settings, market_data_engine=None, backtest_repository=None,
        run_log_repository=SimpleNamespace(log=lambda *a, **k: None),
    )
    engine = BacktestEngine(config=EngineConfig(initial_cash=100_000, risk_per_trade_pct=1.0))
    out = service._run_strategy(
        engine=engine, symbol="TEST", strategy=get_strategy("mean_reversion"),
        history=history, timeframe="1d", provider="test", walk_forward=True,
    )
    assert out["out_of_sample"] is True
    assert out["holdout_evaluated"] is True
    assert "holdout_return_pct" in out and "holdout_max_drawdown_pct" in out
    assert "cost_drag_pct" in out
