"""Backtesting package.

The public surface is intentionally small: anyone wiring up a backtest only
needs :class:`BacktestEngine`, the :class:`CostModel`, and (for Phase 1+)
:class:`WalkForwardSplitter`. Universe-level callers should import
``BatchBacktestService`` from ``app.backtesting.batch`` directly; keeping it
out of this package initializer avoids repository/model import cycles for
lightweight metric and cost-model imports.
"""

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


def __getattr__(name: str):
    if name == "BatchBacktestService":
        from app.backtesting.batch import BatchBacktestService

        return BatchBacktestService
    raise AttributeError(f"module 'app.backtesting' has no attribute {name!r}")


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
