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

import time
from math import isfinite
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
        deadline_seconds: float | None = None,
        start_offset: int = 0,
    ) -> BatchBacktestSummary:
        universe = [symbol.upper() for symbol in (symbols or resolve_universe(self.settings, limit=limit))]
        # Rotate the start so successive scheduled runs sweep the whole universe
        # rather than always re-backtesting the same leading symbols. The runner
        # bounds each pass to ``deadline_seconds`` (a full walk-forward pass over
        # the universe overruns the scheduler's hard job cap and would be killed).
        if start_offset and universe:
            offset = start_offset % len(universe)
            universe = universe[offset:] + universe[:offset]
        scan_timeframes = [timeframe.lower() for timeframe in (timeframes or ["1d"])]
        requested = set(strategy_names or [])
        errors: list[str] = []
        results: list[dict[str, Any]] = []
        tripwires: list[str] = []
        run_count = 0
        started_at = time.monotonic()
        symbols_covered = 0
        truncated = False

        for symbol in universe:
            if (
                deadline_seconds is not None
                and deadline_seconds > 0
                and (time.monotonic() - started_at) >= deadline_seconds
            ):
                truncated = True
                errors.append("backtest_deadline_exceeded")
                break
            # Count the symbol as covered as soon as it is attempted so the
            # rotation cursor always advances past a slow or failing symbol.
            symbols_covered += 1
            for timeframe in scan_timeframes:
                try:
                    history = self.market_data.get_history(
                        symbol,
                        timeframe=timeframe,
                        bars=520 if timeframe == "1w" else 500 if timeframe == "1d" else 350,
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
                    if (
                        deadline_seconds is not None
                        and deadline_seconds > 0
                        and (time.monotonic() - started_at) >= deadline_seconds
                    ):
                        # A single symbol's full strategy sweep can itself exceed
                        # the scheduler's hard job cap, so the top-of-symbol check
                        # alone is not enough — it would be killed mid-symbol and
                        # never advance the cursor. Bail here so the run returns
                        # cleanly; the symbol is already counted and the cursor
                        # moves past it next cycle.
                        truncated = True
                        errors.append("backtest_deadline_exceeded")
                        break
                    run_count += 1
                    strategy = get_strategy(spec.name, **strategy_kwargs_for(self.settings, spec))
                    # Pairs/stat-arb needs its second leg; give it a hedge provider
                    # bound to this engine + timeframe so it can be backtested.
                    if hasattr(strategy, "set_hedge_provider"):
                        strategy.set_hedge_provider(
                            lambda sym, bars, _tf=timeframe, _prov=provider: self.market_data.get_history(
                                sym, timeframe=_tf, bars=bars, provider=_prov
                            )
                        )
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
                if truncated:
                    break
            if truncated:
                break

        aggregate = self._aggregate_metrics(results)
        summary = BatchBacktestSummary(
            generated_at=utc_now().isoformat(),
            symbols_evaluated=symbols_covered,
            strategy_runs=run_count,
            timeframe=",".join(scan_timeframes),
            provider=provider or self.settings.primary_market_data_provider,
            results=sorted(results, key=lambda item: item.get("annualized_return_pct", 0.0), reverse=True),
            aggregate_metrics=aggregate,
            audit_rankings=self._audit_rankings(results, errors + tripwires),
            errors=errors + tripwires,
        )
        self.logs.log(
            "batch_backtest_run",
            {
                "universe": len(universe),
                "symbols_covered": symbols_covered,
                "start_offset": start_offset,
                "truncated": truncated,
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

        # Return and drawdown are aggregated over the concatenated OOS window
        # (compounded fold equity curve), not per fold -- see
        # aggregate_out_of_sample. This makes max_drawdown_pct reflect a
        # multi-fold drawdown that a per-fold maximum would hide.
        aggregated = aggregate_out_of_sample(
            per_fold_trades,
            per_fold_metrics,
            test_days=int(getattr(self.settings, "walk_forward_test_days", 14)),
        )
        metrics = aggregated["metrics"]
        metrics["out_of_sample"] = True
        metrics["fold_count"] = int(metrics.get("fold_count", 0) or 0)

        # Surface cost drag: the per-fold metrics carry cost_drag_pct/total_cost_usd
        # from the engine, but aggregate_out_of_sample rebuilds metrics from trades
        # and drops them. Average them back in so the gate sees friction.
        fold_cost_drags = [float(m.get("cost_drag_pct", 0.0) or 0.0) for m in per_fold_metrics]
        if fold_cost_drags:
            metrics["cost_drag_pct"] = round(sum(fold_cost_drags) / len(fold_cost_drags), 6)
            metrics["total_cost_usd"] = round(
                sum(float(m.get("total_cost_usd", 0.0) or 0.0) for m in per_fold_metrics), 4
            )

        # Evaluate the sealed holdout (the permanent last-N-days window that no
        # fold ever trained or tested on). This is the honest final check the
        # brief mandates; its result gates promotion in _promotion_hint.
        metrics.update(self._evaluate_holdout(engine, symbol, strategy, history, splitter, file_path))

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

    def _evaluate_holdout(
        self,
        engine: BacktestEngine,
        symbol: str,
        strategy: Any,
        history: Any,
        splitter: WalkForwardSplitter,
        file_path: str,
    ) -> dict[str, Any]:
        """Score the sealed holdout window. Runs through a repo-less engine so the
        holdout result is surfaced in the OOS metrics but never persisted as its
        own backtest row (which could pollute the gate's latest-summary lookup)."""

        try:
            window = splitter.holdout_window(history)
        except Exception:
            window = None
        if window is None:
            return {"holdout_evaluated": False}
        try:
            holdout_engine = BacktestEngine(config=engine.config)
            result = holdout_engine.run(
                symbol=symbol,
                strategy=strategy,
                data=window.test_df,
                file_path=f"{file_path}:holdout",
            )
        except Exception:
            return {"holdout_evaluated": False}
        m = result.metrics
        return {
            "holdout_evaluated": True,
            "holdout_return_pct": round(float(m.get("total_return_pct", 0.0) or 0.0), 4),
            "holdout_max_drawdown_pct": round(float(m.get("max_drawdown_pct", 0.0) or 0.0), 4),
            "holdout_trades": int(m.get("number_of_trades", 0) or 0),
            "holdout_expectancy_usd": round(float(m.get("expectancy_usd", 0.0) or 0.0), 4),
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

    @staticmethod
    def _audit_rankings(results: list[dict[str, Any]], errors: list[str]) -> list[dict[str, Any]]:
        grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for item in results:
            key = (str(item.get("strategy_name") or ""), str(item.get("timeframe") or ""))
            grouped.setdefault(key, []).append(item)

        rankings: list[dict[str, Any]] = []
        for (strategy_name, timeframe), items in grouped.items():
            if not strategy_name:
                continue
            total_trades = sum(int(item.get("number_of_trades", 0) or 0) for item in items)
            leakage_warnings = [
                error
                for error in errors
                if strategy_name in error and ("leakage" in error.lower() or "tripwire" in error.lower())
            ]
            avg_expectancy = _avg(items, "expectancy_usd")
            avg_profit_factor = _avg(items, "profit_factor", cap=99.0)
            avg_sharpe = _avg(items, "sharpe_like")
            avg_drawdown = _avg(items, "max_drawdown_pct")
            avg_cost_drag = _avg(items, "cost_drag_pct")
            profitable_run_pct = _profitable_run_pct(items)
            holdout_items = [item for item in items if item.get("holdout_evaluated")]
            holdout_evaluated = bool(holdout_items)
            avg_holdout_return = _avg(holdout_items, "holdout_return_pct") if holdout_items else 0.0
            score = (
                (avg_sharpe * 25.0)
                + (avg_profit_factor * 10.0)
                + (avg_expectancy * 0.1)
                + (profitable_run_pct * 0.15)
                + (min(total_trades, 200) * 0.05)
                - (avg_drawdown * 1.5)
                - (avg_cost_drag * 2.0)
                - (len(leakage_warnings) * 25.0)
            )
            rankings.append(
                {
                    "strategy_name": strategy_name,
                    "timeframe": timeframe,
                    "runs": len(items),
                    "total_trades": total_trades,
                    "average_expectancy_usd": round(avg_expectancy, 4),
                    "average_profit_factor": round(avg_profit_factor, 4),
                    "average_sharpe_like": round(avg_sharpe, 4),
                    "average_max_drawdown_pct": round(avg_drawdown, 4),
                    "average_cost_drag_pct": round(avg_cost_drag, 6),
                    "profitable_run_pct": round(profitable_run_pct, 2),
                    "holdout_evaluated": holdout_evaluated,
                    "average_holdout_return_pct": round(avg_holdout_return, 4),
                    "leakage_warning_count": len(leakage_warnings),
                    "risk_adjusted_rank_score": round(score, 4),
                    "promotion_hint": _promotion_hint(
                        total_trades=total_trades,
                        expectancy=avg_expectancy,
                        profit_factor=avg_profit_factor,
                        drawdown=avg_drawdown,
                        leakage_warning_count=len(leakage_warnings),
                        holdout_evaluated=holdout_evaluated,
                        holdout_return_pct=avg_holdout_return,
                    ),
                }
            )
        return sorted(rankings, key=lambda item: item["risk_adjusted_rank_score"], reverse=True)


__all__ = ["BatchBacktestService"]


def _avg(items: list[dict[str, Any]], key: str, *, cap: float | None = None) -> float:
    if not items:
        return 0.0
    values = []
    for item in items:
        try:
            value = float(item.get(key, 0.0) or 0.0)
        except (TypeError, ValueError):
            value = 0.0
        if not isfinite(value):
            value = cap if cap is not None else 0.0
        if cap is not None:
            value = min(value, cap)
        values.append(value)
    return sum(values) / len(values)


def _profitable_run_pct(items: list[dict[str, Any]]) -> float:
    if not items:
        return 0.0
    profitable = [item for item in items if float(item.get("total_return_pct", 0.0) or 0.0) > 0.0]
    return (len(profitable) / len(items)) * 100.0


def _promotion_hint(
    *,
    total_trades: int,
    expectancy: float,
    profit_factor: float,
    drawdown: float,
    leakage_warning_count: int,
    holdout_evaluated: bool = False,
    holdout_return_pct: float = 0.0,
) -> str:
    if leakage_warning_count:
        return "blocked_leakage_warning"
    # The sealed holdout is the final honest test: a strategy that lost money on
    # the window nothing ever trained or tested on is not a paper candidate,
    # however good its walk-forward folds looked.
    if holdout_evaluated and holdout_return_pct < 0.0:
        return "blocked_holdout_negative"
    if total_trades >= 100 and expectancy > 0.0 and profit_factor >= 1.15 and drawdown <= 12.0:
        return "paper_candidate"
    return "research_only"
