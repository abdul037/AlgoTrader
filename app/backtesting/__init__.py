"""Backtesting package.

The public surface is intentionally small: anyone wiring up a backtest only
needs :class:`BacktestEngine`, the :class:`CostModel`, and (for Phase 1+)
:class:`WalkForwardSplitter`. The :class:`BatchBacktestService` is here for
universe-level runs and is re-exported from ``app.screener`` for backward
compatibility with older imports.
"""

from app.backtesting.batch import BatchBacktestService
from app.backtesting.cost_model import CostModel, is_extended_hours, zero_cost_model
from app.backtesting.engine import BacktestEngine, BacktestResult, EngineConfig
from app.backtesting.metrics import (
    bars_per_year_for,
    calmar,
    deflated_sharpe,
    expectancy_R,
    leakage_tripwire_triggered,
    sortino,
)
from app.backtesting.walk_forward import (
    WalkForwardSplitter,
    WalkForwardWindow,
    aggregate_out_of_sample,
)

__all__ = [
    "BacktestEngine",
    "BacktestResult",
    "BatchBacktestService",
    "CostModel",
    "EngineConfig",
    "WalkForwardSplitter",
    "WalkForwardWindow",
    "aggregate_out_of_sample",
    "bars_per_year_for",
    "calmar",
    "deflated_sharpe",
    "expectancy_R",
    "is_extended_hours",
    "leakage_tripwire_triggered",
    "sortino",
    "zero_cost_model",
]
