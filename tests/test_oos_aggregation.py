from __future__ import annotations

from app.backtesting.walk_forward import aggregate_out_of_sample


def _folds(returns_pct: list[float]):
    """Build (per_fold_trades, per_fold_metrics) from a list of fold returns."""

    per_fold_trades = [[] for _ in returns_pct]
    per_fold_metrics = [{"total_return_pct": r} for r in returns_pct]
    return per_fold_trades, per_fold_metrics


def test_total_return_compounds_across_folds() -> None:
    trades, metrics = _folds([10.0, 10.0])
    result = aggregate_out_of_sample(trades, metrics, test_days=14)
    # 1.1 * 1.1 - 1 = 21%, not the 10% a naive average would give.
    assert result["metrics"]["total_return_pct"] == 21.0


def test_drawdown_captures_multi_fold_decline() -> None:
    # A three-fold sequence whose combined drawdown (28%) is deeper than any
    # single fold's move -- exactly what a per-fold maximum cannot express.
    trades, metrics = _folds([10.0, -20.0, -10.0])
    result = aggregate_out_of_sample(trades, metrics, test_days=14)
    # equity: 1.0 -> 1.1 -> 0.88 -> 0.792; peak 1.1, trough 0.792 -> 28%.
    assert result["metrics"]["max_drawdown_pct"] == 28.0


def test_drawdown_zero_when_monotonic_up() -> None:
    trades, metrics = _folds([5.0, 5.0, 5.0])
    result = aggregate_out_of_sample(trades, metrics, test_days=14)
    assert result["metrics"]["max_drawdown_pct"] == 0.0


def test_annualized_return_uses_true_oos_duration() -> None:
    trades, metrics = _folds([10.0, 10.0])
    result = aggregate_out_of_sample(trades, metrics, test_days=14)
    total = result["metrics"]["total_return_pct"]
    annualized = result["metrics"]["annualized_return_pct"]
    # 21% over 28 days annualizes to far more than the raw total.
    assert annualized > total > 0


def test_total_wipeout_is_floored() -> None:
    trades, metrics = _folds([-100.0])
    result = aggregate_out_of_sample(trades, metrics, test_days=14)
    assert result["metrics"]["total_return_pct"] == -100.0
    assert result["metrics"]["annualized_return_pct"] == -100.0
    assert result["metrics"]["max_drawdown_pct"] == 100.0


def test_no_folds_is_all_zero() -> None:
    result = aggregate_out_of_sample([], [], test_days=14)
    m = result["metrics"]
    assert m["fold_count"] == 0
    assert m["total_return_pct"] == 0.0
    assert m["annualized_return_pct"] == 0.0
    assert m["max_drawdown_pct"] == 0.0


def test_merged_trades_preserved() -> None:
    per_fold_trades = [[{"pnl_usd": 5.0}], [{"pnl_usd": -2.0}, {"pnl_usd": 3.0}]]
    per_fold_metrics = [{"total_return_pct": 5.0}, {"total_return_pct": 1.0}]
    result = aggregate_out_of_sample(per_fold_trades, per_fold_metrics, test_days=14)
    assert len(result["merged_trades"]) == 3
    assert result["metrics"]["fold_count"] == 2
