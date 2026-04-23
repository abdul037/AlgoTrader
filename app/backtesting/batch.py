"""Run backtests across a universe of symbols and aggregate summaries.

Historically this class lived inside ``app/screener/service.py`` as
``BatchBacktestService``. The 1,500-line screener module no longer has any
business owning a backtest runner, and the brief's file-size ceiling (600
lines) requires it to shrink. The class now lives here and is re-exported
from ``app.screener.service`` for backward compatibility with existing
callers.

Behaviour change from the pre-audit version: this runner now executes each
strategy through walk-forward folds (when enabled) and aggregates the
out-of-sample metrics instead of returning a single in-sample pass. Callers
that still want an in-sample pass can set ``walk_forward=False``.
"""

from __future__ import annotations

from typing import Any

from app.backtesting.engine import BacktestEngine, EngineConfig
from app.backtesting.metrics import bars_per_year_for, leakage_tripwire_triggered
from app.backtesting.strategy_selection import strategy_kwargs_for, strategy_specs_for
from app.backtesting.walk_forward import WalkForwardSplitter, aggregate_out_of_sample
from app.models.screener import BatchBacktestSummary
from app.runtime_settings import AppSettings
from app.strategies import get_strategy
from app.universe import resolve_universe
from app.utils.ids import generate_id
from app.utils.time import utc_now


class BatchBacktestService:
    """Run backtests across a universe and aggregate summary statistics."""

    def __init__(
        self,
        *,
        settings: AppSettings,
        market_data_engine: Any,
        backtest_repository: Any,
        run_log_repository: Any,
    ):
        self.settings = settings
        self.market_data = market_data_engine
        self.backtests = backtest_repository
        self.logs = run_log_repository

    def run(
        self,
        *,
        symbols: list[str] | None = None,
        timeframes: list[str] | None = None,
        strategy_names: list[str] | None = None,
        provider: str | None = None,
        initial_cash: float = 10000.0,
        limit: int | None = None,
        force_refresh: bool = False,
        walk_forward: bool = True,
    ) -> BatchBacktestSummary:
        universe = [symbol.upper() for symbol in (symbols or resolve_universe(self.settings, limit=limit))]
        scan_timeframes = [timeframe.lower() for timeframe in (timeframes or ["1d"])]
        requested = set(strategy_names or [])
        errors: list[str] = []
        results: list[dict[str, Any]] = []
        tripwires: list[str] = []
        run_count = 0

        for symbol in universe:
            for timeframe in scan_timeframes:
                try:
                    history = self.market_data.get_history(
                        symbol,
                        timeframe=timeframe,
                        bars=500 if timeframe == "1d" else 350,
                        provider=provider,
                        force_refresh=force_refresh,
                    )
                except Exception as exc:
                    errors.append(f"{symbol} {timeframe}: {exc}")
                    continue

                engine_config = EngineConfig(
                    initial_cash=initial_cash,
                    risk_per_trade_pct=float(getattr(self.settings, "max_risk_per_trade_pct", 1.0)),
                    bars_per_year=bars_per_year_for(timeframe),
                )
                engine = BacktestEngine(self.backtests, config=engine_config)

                for spec in strategy_specs_for(self.settings, timeframe=timeframe, requested=requested):
                    run_count += 1
                    strategy = get_strategy(spec.name, **strategy_kwargs_for(self.settings, spec))
                    try:
                        summary = self._run_strategy(
                            engine=engine,
                            symbol=symbol,
                            strategy=strategy,
                            history=history.copy(),
                            timeframe=timeframe,
                            provider=provider,
                            walk_forward=walk_forward,
                        )
                    except Exception as exc:
                        errors.append(f"{symbol} {timeframe} {spec.name}: {exc}")
                        continue
                    results.append(summary)
                    triggered, reason = leakage_tripwire_triggered(summary)
                    if triggered:
                        tripwires.append(
                            f"{symbol} {timeframe} {spec.name}: {reason}"
                        )

        aggregate = self._aggregate_metrics(results)
        summary = BatchBacktestSummary(
            generated_at=utc_now().isoformat(),
            symbols_evaluated=len(universe),
            strategy_runs=run_count,
            timeframe=",".join(scan_timeframes),
            provider=provider or self.settings.primary_market_data_provider,
            results=sorted(results, key=lambda item: item.get("annualized_return_pct", 0.0), reverse=True),
            aggregate_metrics=aggregate,
            errors=errors + tripwires,
        )
        self.logs.log(
            "batch_backtest_run",
            {
                "symbols": len(universe),
                "timeframes": scan_timeframes,
                "strategy_runs": run_count,
                "results": len(results),
                "errors": len(errors),
                "tripwires": len(tripwires),
                "walk_forward": walk_forward,
            },
        )
        return summary

    def _run_strategy(
        self,
        *,
        engine: BacktestEngine,
        symbol: str,
        strategy: Any,
        history: Any,
        timeframe: str,
        provider: str | None,
        walk_forward: bool,
    ) -> dict[str, Any]:
        """Run a single strategy either in-sample or via walk-forward folds."""

        file_path = f"{provider or self.settings.primary_market_data_provider}:{timeframe}:{symbol}"

        if not walk_forward:
            result = engine.run(
                symbol=symbol,
                strategy=strategy,
                data=history,
                file_path=file_path,
            )
            return {
                "symbol": result.symbol,
                "strategy_name": result.strategy_name,
                "timeframe": timeframe,
                "provider": provider or self.settings.primary_market_data_provider,
                "out_of_sample": False,
                **result.metrics,
            }

        splitter = WalkForwardSplitter(
            train_days=int(getattr(self.settings, "walk_forward_train_days", 180)),
            test_days=int(getattr(self.settings, "walk_forward_test_days", 14)),
            step_days=int(getattr(self.settings, "walk_forward_step_days", 14)),
            embargo_days=int(getattr(self.settings, "walk_forward_embargo_days", 1)),
            holdout_days=int(getattr(self.settings, "walk_forward_holdout_days", 28)),
        )
        per_fold_trades: list[list[dict]] = []
        per_fold_metrics: list[dict] = []
        for window in splitter.split(history):
            fold_result = engine.run(
                symbol=symbol,
                strategy=strategy,
                data=window.test_df,
                file_path=f"{file_path}:fold:{window.test_start.isoformat()}",
            )
            per_fold_trades.append(fold_result.trades)
            per_fold_metrics.append(fold_result.metrics)

        aggregated = aggregate_out_of_sample(per_fold_trades, per_fold_metrics)
        metrics = aggregated["metrics"]
        metrics["out_of_sample"] = True
        metrics["fold_count"] = int(metrics.get("fold_count", 0) or 0)
        # Fold-weighted return / DD aggregates. Fine-grained equity curves are
        # not persisted here; callers who want them should use the engine
        # directly and store results themselves.
        avg = lambda key: (
            sum(float(item.get(key, 0.0) or 0.0) for item in per_fold_metrics) / len(per_fold_metrics)
            if per_fold_metrics
            else 0.0
        )
        metrics["total_return_pct"] = avg("total_return_pct")
        metrics["annualized_return_pct"] = avg("annualized_return_pct")
        metrics["max_drawdown_pct"] = max(
            (float(item.get("max_drawdown_pct", 0.0) or 0.0) for item in per_fold_metrics),
            default=0.0,
        )
        completed_at = utc_now().isoformat()
        if self.backtests is not None:
            self.backtests.create(
                backtest_id=generate_id("bt"),
                symbol=symbol.upper(),
                strategy_name=strategy.name,
                file_path=f"{file_path}:walk_forward_oos",
                started_at=completed_at,
                completed_at=completed_at,
                metrics=metrics,
                trades=aggregated["merged_trades"],
            )
        return {
            "symbol": symbol.upper(),
            "strategy_name": strategy.name,
            "timeframe": timeframe,
            "provider": provider or self.settings.primary_market_data_provider,
            "out_of_sample": True,
            "fold_count": aggregated["metrics"].get("fold_count", 0),
            **metrics,
        }

    @staticmethod
    def _aggregate_metrics(results: list[dict[str, Any]]) -> dict[str, float]:
        if not results:
            return {}
        total = len(results)
        profitable = [item for item in results if float(item.get("total_return_pct", 0.0) or 0.0) > 0]
        avg = lambda key: round(sum(float(item.get(key, 0.0) or 0.0) for item in results) / total, 4)
        return {
            "profitable_run_pct": round((len(profitable) / total) * 100.0, 2),
            "average_total_return_pct": avg("total_return_pct"),
            "average_annualized_return_pct": avg("annualized_return_pct"),
            "average_profit_factor": avg("profit_factor"),
            "average_win_rate": avg("win_rate"),
            "average_max_drawdown_pct": avg("max_drawdown_pct"),
        }


__all__ = ["BatchBacktestService"]
