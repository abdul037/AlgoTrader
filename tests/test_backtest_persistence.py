"""Walk-forward persistence and the backtest expectancy baseline.

Found 2026-09-26: every walk-forward run persisted one row per fold (~37) plus
the aggregate, every 30 minutes, growing the backtests table to 1.45M rows.
Only the per-fold rows carried ``expectancy_usd``, so the live-vs-backtest
decay baseline averaged ~10-bar fold slices (0-1 trades each) and ignored the
real out-of-sample aggregate entirely.
"""

from __future__ import annotations

import json

from app.backtesting.batch import BatchBacktestService
from app.backtesting.engine import BacktestEngine, EngineConfig
from app.backtesting.walk_forward import aggregate_out_of_sample
from app.storage.db import Database
from app.storage.repositories import BacktestRepository
from app.strategies import get_strategy
from tests.conftest import make_settings
from tests.test_walk_forward_warmup import _trending_history


def test_aggregate_carries_expectancy_from_merged_trades() -> None:
    folds = [
        [{"pnl_usd": 30.0, "pnl_pct": 1.5}, {"pnl_usd": -10.0, "pnl_pct": -0.5}],
        [],
        [{"pnl_usd": 40.0, "pnl_pct": 2.0}],
    ]
    metrics = [{"total_return_pct": 0.2}, {"total_return_pct": 0.0}, {"total_return_pct": 0.4}]

    result = aggregate_out_of_sample(folds, metrics, test_days=14)

    assert result["metrics"]["number_of_trades"] == 3
    assert result["metrics"]["expectancy_usd"] == 20.0  # (30 - 10 + 40) / 3
    assert round(result["metrics"]["expectancy_pct"], 4) == 1.0


def _repo(tmp_path):
    settings = make_settings(tmp_path)
    db = Database(settings)
    db.initialize()
    return settings, BacktestRepository(db)


def _rows(repo) -> list[str]:
    with repo.db.connect() as connection:
        return [row["file_path"] for row in connection.execute("SELECT file_path FROM backtests").fetchall()]


def test_walk_forward_persists_only_the_aggregate_row(tmp_path) -> None:
    settings, repo = _repo(tmp_path)
    service = BatchBacktestService(
        settings=settings, market_data_engine=None, backtest_repository=repo, run_log_repository=None
    )

    summary = service._run_strategy(
        engine=BacktestEngine(repo, config=EngineConfig(initial_cash=10_000.0)),
        symbol="SYN",
        strategy=get_strategy("ma_crossover"),
        history=_trending_history(),
        timeframe="1d",
        provider="test",
        walk_forward=True,
    )

    paths = _rows(repo)
    assert summary["fold_count"] > 1, "fixture must produce several folds"
    assert paths == ["test:1d:SYN:walk_forward_oos"], paths  # no :fold: rows, no :holdout row
    with repo.db.connect() as connection:
        stored = json.loads(connection.execute("SELECT metrics_json FROM backtests").fetchone()["metrics_json"])
    assert "expectancy_usd" in stored


def test_expectancy_baseline_ignores_legacy_fold_rows(tmp_path) -> None:
    _settings, repo = _repo(tmp_path)
    common = dict(symbol="SYN", strategy_name="ma_crossover", started_at="2026-09-01T00:00:00+00:00", trades=[])
    # A legacy per-fold row with a wild expectancy must not move the baseline.
    repo.create(
        backtest_id="bt_fold",
        file_path="test:1d:SYN:fold:2026-08-01T00:00:00+00:00",
        completed_at="2026-09-02T00:00:00+00:00",
        metrics={"expectancy_usd": -999.0},
        **common,
    )
    repo.create(
        backtest_id="bt_agg",
        file_path="test:1d:SYN:walk_forward_oos",
        completed_at="2026-09-01T00:00:00+00:00",
        metrics={"expectancy_usd": 12.5, "out_of_sample": True},
        **common,
    )

    assert repo.expectancy_by_strategy() == {"ma_crossover": 12.5}
