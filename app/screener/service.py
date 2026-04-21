"""Universe screener and batch backtest services."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.backtesting.engine import BacktestEngine
from app.broker.etoro_rate_limit import EToroRateLimitError
from app.intelligence import MarketIntelligenceService, build_trade_plan
from app.live_signal_schema import LiveSignalSnapshot, SignalState
from app.models.screener import BatchBacktestSummary, MarketUniverseResponse, ScreenerRunResponse
from app.runtime_settings import AppSettings
from app.screener.accuracy import build_accuracy_profile
from app.screener.filters import FilterOutcome, MarketContext, ScreenerFilterPipeline, build_market_context
from app.screener.scoring import build_backtest_snapshot, freshness_for_decision, rank_live_signal
from app.strategies import get_strategy, get_strategy_specs
from app.telegram_notify import TelegramNotifier
from app.universe import resolve_universe
from app.utils.time import utc_now


def _active_strategy_names(settings: Any, *, requested: set[str] | None = None) -> set[str] | None:
    if requested:
        return {item.strip().lower() for item in requested if item.strip()}
    configured = {
        item.strip().lower()
        for item in getattr(settings, "screener_active_strategy_names", []) or []
        if item.strip()
    }
    if not configured or "all" in configured:
        return None
    return configured


def _strategy_specs(settings: Any, *, timeframe: str, requested: set[str] | None = None) -> list[Any]:
    active = _active_strategy_names(settings, requested=requested)
    specs = get_strategy_specs(timeframe=timeframe)
    if active is None:
        return specs
    return [spec for spec in specs if spec.name.lower() in active]


def _strategy_kwargs(settings: Any, spec: Any) -> dict[str, object]:
    kwargs = dict(spec.default_kwargs)
    if spec.name != getattr(settings, "screener_primary_strategy_name", "rsi_vwap_ema_confluence"):
        return kwargs
    kwargs.update(
        {
            "minimum_confluence_score": float(settings.confluence_minimum_score),
            "minimum_relative_volume": max(
                float(kwargs.get("minimum_relative_volume") or 0.0),
                float(settings.confluence_minimum_relative_volume),
            ),
            "minimum_adx": float(settings.confluence_minimum_adx),
            "rsi_long_min": float(settings.confluence_rsi_long_min),
            "rsi_long_max": float(settings.confluence_rsi_long_max),
            "rsi_short_min": float(settings.confluence_rsi_short_min),
            "rsi_short_max": float(settings.confluence_rsi_short_max),
            "max_extension_atr": float(settings.confluence_max_extension_atr),
            "minimum_body_to_range": float(settings.confluence_min_body_to_range),
            "minimum_close_location": float(settings.confluence_min_close_location),
        }
    )
    return kwargs


class MarketScreenerService:
    """Evaluate a configurable market universe across multiple strategies and timeframes."""

    def __init__(
        self,
        *,
        settings: AppSettings,
        market_data_engine: Any,
        signal_state_repository: Any,
        run_log_repository: Any,
        backtest_repository: Any | None = None,
        scan_decision_repository: Any | None = None,
        telegram_notifier: TelegramNotifier | Any | None = None,
    ):
        self.settings = settings
        self.market_data = market_data_engine
        self.signal_states = signal_state_repository
        self.logs = run_log_repository
        self.backtests = backtest_repository
        self.scan_decisions = scan_decision_repository
        self.notifier = telegram_notifier
        self.filters = ScreenerFilterPipeline(settings)
        self.intelligence = MarketIntelligenceService(settings, market_data_engine)

    def get_universe(self, *, limit: int | None = None) -> MarketUniverseResponse:
        symbols = resolve_universe(self.settings, limit=limit)
        return MarketUniverseResponse(
            universe_name=self.settings.market_universe_name,
            symbols=symbols,
            count=len(symbols),
        )

    def scan_universe(
        self,
        *,
        symbols: list[str] | None = None,
        timeframes: list[str] | None = None,
        limit: int | None = None,
        validated_only: bool = False,
        notify: bool = False,
        force_refresh: bool = False,
        scan_task: str = "manual_scan",
        cancel_event: Any | None = None,
    ) -> ScreenerRunResponse:
        universe = [symbol.upper() for symbol in (symbols or resolve_universe(self.settings))]
        scan_timeframes = [timeframe.lower() for timeframe in (timeframes or self.settings.screener_default_timeframes)]
        candidates: list[LiveSignalSnapshot] = []
        errors: list[str] = []
        rejection_summary: dict[str, int] = {}
        closest_rejections: list[dict[str, Any]] = []
        suppressed = 0
        evaluated_strategy_runs = 0
        evaluated_symbols = 0
        abort_scan = False
        self.logs.log(
            "market_universe_scan_started",
            {
                "scan_task": scan_task,
                "universe_name": self.settings.market_universe_name,
                "symbols": universe,
                "timeframes": scan_timeframes,
                "validated_only": validated_only,
            },
        )

        for symbol in universe:
            if self._scan_cancelled(cancel_event):
                errors.append("scan_cancelled")
                break
            if abort_scan:
                break
            evaluated_symbols += 1
            for timeframe in scan_timeframes:
                if self._scan_cancelled(cancel_event):
                    errors.append("scan_cancelled")
                    abort_scan = True
                    break
                try:
                    history = self.market_data.get_history(
                        symbol,
                        timeframe=timeframe,
                        bars=self._bars_for_timeframe(timeframe),
                        force_refresh=force_refresh,
                    )
                    quote = self.market_data.get_quote(
                        symbol,
                        timeframe=timeframe,
                        force_refresh=force_refresh,
                    )
                except EToroRateLimitError as exc:
                    errors.append(f"{symbol} {timeframe}: {exc}")
                    self._add_scan_diagnostic(
                        rejection_summary,
                        closest_rejections,
                        symbol=symbol,
                        timeframe=timeframe,
                        strategy_name="market_data",
                        status="error",
                        rejection_reasons=["market_data_rate_limited"],
                    )
                    abort_scan = True
                    break
                except Exception as exc:
                    errors.append(f"{symbol} {timeframe}: {exc}")
                    self._add_scan_diagnostic(
                        rejection_summary,
                        closest_rejections,
                        symbol=symbol,
                        timeframe=timeframe,
                        strategy_name="market_data",
                        status="error",
                        rejection_reasons=["market_data_error"],
                    )
                    continue

                for spec in _strategy_specs(self.settings, timeframe=timeframe):
                    if self._scan_cancelled(cancel_event):
                        errors.append("scan_cancelled")
                        abort_scan = True
                        break
                    evaluated_strategy_runs += 1
                    strategy = get_strategy(spec.name, **_strategy_kwargs(self.settings, spec))
                    try:
                        signal = strategy.generate_signal(history.copy(), symbol)
                    except Exception as exc:
                        errors.append(f"{symbol} {timeframe} {spec.name}: {exc}")
                        self._add_scan_diagnostic(
                            rejection_summary,
                            closest_rejections,
                            symbol=symbol,
                            timeframe=timeframe,
                            strategy_name=spec.name,
                            status="error",
                            rejection_reasons=["strategy_error"],
                        )
                        continue
                    if signal is None:
                        self._increment_rejection(rejection_summary, "no_strategy_signal")
                        continue
                    signal.metadata.setdefault("timeframe", timeframe)
                    signal.metadata.setdefault("strategy_style", spec.style)
                    backtest = self._backtest_validation(signal.symbol, signal.strategy_name, timeframe)
                    backtest_snapshot = build_backtest_snapshot(
                        backtest["summary"],
                        validated=backtest["passes"],
                        validation_reason=backtest["reason"],
                    )
                    context = build_market_context(history, quote=quote, signal=signal)
                    signal.metadata.setdefault(
                        "indicator_confluence_score",
                        float(context.measurements.get("indicator_confluence_score") or 0.0),
                    )
                    signal.metadata.setdefault(
                        "execution_quality",
                        float(context.measurements.get("execution_quality") or 0.5),
                    )
                    accuracy_profile = build_accuracy_profile(
                        history,
                        signal=signal,
                        context=context,
                        settings=self.settings,
                    )
                    signal.metadata.update(
                        {
                            "accuracy_score": accuracy_profile.overall_score,
                            "entry_location_score": accuracy_profile.entry_location_score,
                            "support_resistance_score": accuracy_profile.support_resistance_score,
                            "confirmation_score": accuracy_profile.confirmation_score,
                            "false_positive_risk_score": accuracy_profile.false_positive_risk_score,
                            "accuracy_pass_reasons": list(accuracy_profile.pass_reasons),
                            "accuracy_rejection_reasons": list(accuracy_profile.rejection_reasons),
                            **accuracy_profile.measurements,
                        }
                    )
                    intelligence = self.intelligence.analyze(
                        symbol=signal.symbol,
                        timeframe=timeframe,
                        history=history,
                        quote=quote,
                        signal=signal,
                        force_refresh=force_refresh,
                    )
                    market_data_status = self._market_data_status(history=history, quote=quote)
                    if self.settings.require_verified_market_data_for_alerts and not market_data_status["verified"]:
                        suppressed += 1
                        self._add_scan_diagnostic(
                            rejection_summary,
                            closest_rejections,
                            symbol=signal.symbol,
                            timeframe=timeframe,
                            strategy_name=signal.strategy_name,
                            status="suppressed",
                            rejection_reasons=[market_data_status["verification_reason"]],
                            measurements=market_data_status,
                        )
                        self._record_scan_decision(
                            scan_task=scan_task,
                            signal=signal,
                            timeframe=timeframe,
                            status="suppressed",
                            final_score=None,
                            alert_eligible=False,
                            freshness=None,
                            filter_outcome=FilterOutcome(
                                passed=False,
                                pass_reasons=[],
                                rejection_reasons=[market_data_status["verification_reason"]],
                                reason_codes=[market_data_status["verification_reason"]],
                                measurements=market_data_status,
                            ),
                            payload={
                                "market_data_status": market_data_status,
                                "backtest_snapshot": backtest_snapshot,
                            },
                        )
                        continue
                    filter_outcome = self.filters.evaluate(
                        signal=signal,
                        context=context,
                        backtest_snapshot=backtest_snapshot,
                        intelligence=intelligence,
                    )
                    if market_data_status["verified"]:
                        filter_outcome.pass_reasons.append("market_data_verified")
                        filter_outcome.reason_codes.append("market_data_verified")
                    else:
                        filter_outcome.pass_reasons.append("market_data_unverified")
                        filter_outcome.reason_codes.append("market_data_unverified")
                    filter_outcome.measurements.update(market_data_status)
                    if not filter_outcome.passed:
                        suppressed += 1
                        self._add_scan_diagnostic(
                            rejection_summary,
                            closest_rejections,
                            symbol=signal.symbol,
                            timeframe=timeframe,
                            strategy_name=signal.strategy_name,
                            status="rejected",
                            rejection_reasons=filter_outcome.rejection_reasons,
                            final_score=None,
                            measurements=filter_outcome.measurements,
                        )
                        self._record_scan_decision(
                            scan_task=scan_task,
                            signal=signal,
                            timeframe=timeframe,
                            status="rejected",
                            final_score=None,
                            alert_eligible=False,
                            freshness=None,
                            filter_outcome=filter_outcome,
                            payload={
                                "backtest_snapshot": backtest_snapshot,
                                "measurements": filter_outcome.measurements,
                                "rationale": signal.rationale,
                            },
                        )
                        continue

                    previous_decision = (
                        self.scan_decisions.get_latest(
                            symbol=signal.symbol,
                            strategy_name=signal.strategy_name,
                            timeframe=timeframe,
                            since_minutes=self.settings.screener_duplicate_alert_window_minutes,
                            statuses=["candidate", "watchlist", "alerted"],
                        )
                        if self.scan_decisions is not None and scan_task != "manual_scan"
                        else None
                    )
                    provisional_freshness = "fresh" if previous_decision is None else "repeated_upgraded"
                    ranking = rank_live_signal(
                        settings=self.settings,
                        signal=signal,
                        context=context,
                        backtest_snapshot=backtest_snapshot,
                        intelligence=intelligence,
                        watchlist_only=filter_outcome.watchlist_only,
                        freshness=provisional_freshness,
                    )
                    freshness, suppress_repeat = freshness_for_decision(
                        previous_decision,
                        final_score=float(ranking["final_score"]),
                        minimum_improvement=float(self.settings.screener_min_score_improvement_for_repeat),
                    )
                    if suppress_repeat:
                        suppressed += 1
                        self._add_scan_diagnostic(
                            rejection_summary,
                            closest_rejections,
                            symbol=signal.symbol,
                            timeframe=timeframe,
                            strategy_name=signal.strategy_name,
                            status="suppressed",
                            rejection_reasons=["recent_alert_without_material_score_improvement"],
                            final_score=float(ranking["final_score"]),
                            measurements=filter_outcome.measurements,
                        )
                        self._record_scan_decision(
                            scan_task=scan_task,
                            signal=signal,
                            timeframe=timeframe,
                            status="suppressed",
                            final_score=float(ranking["final_score"]),
                            alert_eligible=False,
                            freshness=freshness,
                            filter_outcome=FilterOutcome(
                                passed=False,
                                pass_reasons=filter_outcome.pass_reasons,
                                rejection_reasons=["recent_alert_without_material_score_improvement"],
                                reason_codes=[*filter_outcome.pass_reasons, "recent_alert_without_material_score_improvement"],
                                measurements=filter_outcome.measurements,
                                watchlist_only=filter_outcome.watchlist_only,
                            ),
                            payload={
                                "backtest_snapshot": backtest_snapshot,
                                "measurements": filter_outcome.measurements,
                                "score_breakdown": ranking["score_breakdown"],
                            },
                        )
                        continue
                    ranking = rank_live_signal(
                        settings=self.settings,
                        signal=signal,
                        context=context,
                        backtest_snapshot=backtest_snapshot,
                        intelligence=intelligence,
                        watchlist_only=filter_outcome.watchlist_only,
                        freshness=freshness,
                    )
                    if ranking["actionability"] == "reject":
                        suppressed += 1
                        self._add_scan_diagnostic(
                            rejection_summary,
                            closest_rejections,
                            symbol=signal.symbol,
                            timeframe=timeframe,
                            strategy_name=signal.strategy_name,
                            status="rejected",
                            rejection_reasons=["final_score_below_keep_threshold"],
                            final_score=float(ranking["final_score"]),
                            measurements={**filter_outcome.measurements, **intelligence.measurements},
                        )
                        self._record_scan_decision(
                            scan_task=scan_task,
                            signal=signal,
                            timeframe=timeframe,
                            status="rejected",
                            final_score=float(ranking["final_score"]),
                            alert_eligible=False,
                            freshness=freshness,
                            filter_outcome=FilterOutcome(
                                passed=False,
                                pass_reasons=filter_outcome.pass_reasons,
                                rejection_reasons=["final_score_below_keep_threshold"],
                                reason_codes=[*filter_outcome.pass_reasons, "final_score_below_keep_threshold"],
                                measurements={**filter_outcome.measurements, **intelligence.measurements},
                                watchlist_only=filter_outcome.watchlist_only,
                            ),
                            payload={
                                "backtest_snapshot": backtest_snapshot,
                                "measurements": filter_outcome.measurements,
                                "score_breakdown": ranking["score_breakdown"],
                                "market_intelligence": intelligence.measurements,
                            },
                        )
                        continue

                    snapshot = self._snapshot_from_signal(
                        signal,
                        quote=quote,
                        timeframe=timeframe,
                        context=context,
                        intelligence=intelligence,
                        market_data_status=market_data_status,
                        filter_outcome=filter_outcome,
                        backtest_snapshot=backtest_snapshot,
                        ranking=ranking,
                        freshness=freshness,
                    )
                    if validated_only and not bool(snapshot.metadata.get("backtest_validated")):
                        suppressed += 1
                        self._add_scan_diagnostic(
                            rejection_summary,
                            closest_rejections,
                            symbol=snapshot.symbol,
                            timeframe=timeframe,
                            strategy_name=snapshot.strategy_name,
                            status="suppressed",
                            rejection_reasons=["validated_only_filter"],
                            final_score=snapshot.score,
                            measurements=filter_outcome.measurements,
                        )
                        self._record_scan_decision(
                            scan_task=scan_task,
                            signal=signal,
                            timeframe=timeframe,
                            status="suppressed",
                            final_score=snapshot.score,
                            alert_eligible=False,
                            freshness=freshness,
                            filter_outcome=FilterOutcome(
                                passed=False,
                                pass_reasons=snapshot.pass_reasons,
                                rejection_reasons=["validated_only_filter"],
                                reason_codes=[*snapshot.pass_reasons, "validated_only_filter"],
                                measurements=filter_outcome.measurements,
                                watchlist_only=filter_outcome.watchlist_only,
                            ),
                            payload=snapshot.model_dump(),
                        )
                        continue

                    self.signal_states.upsert(snapshot)
                    candidates.append(snapshot)
                    self._record_scan_decision(
                        scan_task=scan_task,
                        signal=signal,
                        timeframe=timeframe,
                        status="candidate" if bool(snapshot.metadata.get("alert_eligible")) else "watchlist",
                        final_score=snapshot.score,
                        alert_eligible=bool(snapshot.metadata.get("alert_eligible")),
                        freshness=freshness,
                        filter_outcome=filter_outcome,
                        payload=snapshot.model_dump(),
                    )
                if abort_scan:
                    break

        ranked = sorted(candidates, key=self._ranking_key, reverse=True)
        top_k = min(limit or self.settings.screener_top_k, len(ranked)) if ranked else 0
        top_candidates = [
            item.model_copy(update={"rank": index + 1})
            for index, item in enumerate(ranked[:top_k])
        ]
        response = ScreenerRunResponse(
            generated_at=utc_now().isoformat(),
            universe_name=self.settings.market_universe_name,
            timeframes=scan_timeframes,
            evaluated_symbols=evaluated_symbols,
            evaluated_strategy_runs=evaluated_strategy_runs,
            candidates=top_candidates,
            suppressed=suppressed,
            alerts_sent=0,
            errors=errors,
            rejection_summary=dict(sorted(rejection_summary.items(), key=lambda item: (-item[1], item[0]))),
            closest_rejections=self._rank_closest_rejections(closest_rejections),
        )
        if notify and self.notifier is not None and hasattr(self.notifier, "send_text"):
            sent = bool(self.notifier.send_text(self.notifier.format_screener_summary(response)))
            response.alerts_sent = 1 if sent else 0

        self.logs.log(
            "market_universe_scan_completed",
            {
                "scan_task": scan_task,
                "universe_name": self.settings.market_universe_name,
                "symbols_scanned": universe[:evaluated_symbols],
                "timeframes": scan_timeframes,
                "evaluated_strategy_runs": evaluated_strategy_runs,
                "symbols_passed": [item.symbol for item in top_candidates],
                "candidates": len(candidates),
                "suppressed": suppressed,
                "alerts_sent": response.alerts_sent,
                "errors": errors,
                "rejection_summary": response.rejection_summary,
                "closest_rejections": response.closest_rejections,
            },
        )
        return response

    def analyze_symbol(
        self,
        symbol: str,
        *,
        force_refresh: bool = False,
    ) -> LiveSignalSnapshot:
        """Return a premium single-symbol analysis using the screener stack."""

        normalized = symbol.upper().strip()
        response = self.scan_universe(
            symbols=[normalized],
            timeframes=list(self.settings.single_symbol_analysis_timeframes),
            limit=5,
            validated_only=False,
            notify=False,
            force_refresh=force_refresh,
            scan_task="single_symbol_analysis",
        )
        if response.candidates:
            best = response.candidates[0].model_copy(deep=True)
            best.metadata["analysis_mode"] = "single_symbol"
            best.metadata["analysis_candidates_evaluated"] = response.evaluated_strategy_runs
            best.metadata["analysis_errors"] = list(response.errors)
            return best
        if response.errors:
            return self._build_data_unavailable_snapshot(normalized, response=response)
        return self._build_no_trade_snapshot(normalized, response=response, force_refresh=force_refresh)

    def _snapshot_from_signal(
        self,
        signal: Any,
        *,
        quote: Any,
        timeframe: str,
        context: MarketContext,
        intelligence: Any,
        market_data_status: dict[str, Any],
        filter_outcome: FilterOutcome,
        backtest_snapshot: dict[str, Any],
        ranking: dict[str, Any],
        freshness: str,
    ) -> LiveSignalSnapshot:
        current_price = float(quote.last_execution or quote.ask or quote.bid or signal.price or context.current_price or 0.0)
        risk_reward = signal.metadata.get("risk_reward_ratio")
        resolved_risk_reward = float(risk_reward) if risk_reward is not None else self._compute_risk_reward(signal)
        trade_plan = build_trade_plan(
            signal=signal,
            timeframe=timeframe,
            current_price=current_price,
            entry_price=float(signal.price or current_price),
            stop_loss=signal.stop_loss,
            take_profit=signal.take_profit,
            risk_reward_ratio=resolved_risk_reward,
            final_score=float(ranking["final_score"]),
            context=context,
            intelligence=intelligence,
            actionability=str(ranking["actionability"]),
        )
        targets = [
            float(target)
            for target in [
                trade_plan.get("target_1"),
                trade_plan.get("target_2"),
                trade_plan.get("stretch_target"),
            ]
            if target is not None
        ]
        metadata = {
            "data_source": market_data_status["quote_provider"],
            "data_source_quote": market_data_status["quote_provider"],
            "data_source_history": market_data_status["history_provider"],
            "data_source_verified": market_data_status["verified"],
            "data_source_verification_reason": market_data_status["verification_reason"],
            "quote_live_verified": market_data_status["quote_live_verified"],
            "quote_verification_reason": market_data_status["quote_verification_reason"],
            "bars_fresh": market_data_status["history_fresh"],
            "history_freshness_reason": market_data_status["history_freshness_reason"],
            "freshness_status": market_data_status["freshness_status"],
            "data_source_primary": market_data_status["quote_is_primary"],
            "data_source_used_fallback": market_data_status["quote_used_fallback"],
            "data_source_from_cache": market_data_status["history_from_cache"],
            "data_source_quote_derived": market_data_status["quote_derived"],
            "data_source_age_seconds": market_data_status["history_age_seconds"],
            "quote_age_seconds": market_data_status["quote_age_seconds"],
            "bar_age_seconds": market_data_status["history_age_seconds"],
            "quote_timestamp": market_data_status["quote_timestamp"],
            "bar_timestamp": market_data_status["bar_timestamp"],
            "style": signal.metadata.get("style"),
            "signal_role": signal.metadata.get("signal_role", "entry_long"),
            "backtest_validated": bool(backtest_snapshot.get("validated")),
            "backtest_validation_reason": backtest_snapshot.get("validation_reason"),
            "freshness": freshness,
            "market_context_summary": intelligence.summary,
            "market_regime_label": intelligence.market_regime_label,
            "market_risk_mode": intelligence.risk_mode,
            "market_volatility_environment": intelligence.volatility_environment,
            "market_regime_score": intelligence.market_regime_score,
            "higher_timeframe_alignment_score": intelligence.higher_timeframe_alignment_score,
            "lower_timeframe_alignment_score": intelligence.lower_timeframe_alignment_score,
            "timeframe_alignment_score": intelligence.timeframe_alignment_score,
            "relative_strength_vs_market": intelligence.relative_strength_vs_market,
            "relative_strength_vs_sector": intelligence.relative_strength_vs_sector,
            "sector_strength_score": intelligence.sector_strength_score,
            "benchmark_strength_score": intelligence.benchmark_strength_score,
            "time_of_day_score": intelligence.time_of_day_score,
            "momentum_state": intelligence.momentum_state,
            "trade_plan": trade_plan,
            "verdict": trade_plan.get("verdict"),
            "timing_label": trade_plan.get("timing_label"),
            "analysis_mode": "scan",
            "indicator_confluence_score": signal.metadata.get("indicator_confluence_score"),
            **signal.metadata,
        }
        execution_blockers = self._execution_blockers(
            market_data_status=market_data_status,
            final_score=float(ranking["final_score"]),
            risk_reward_ratio=resolved_risk_reward,
            actionability=str(ranking["actionability"]),
        )
        execution_ready = not execution_blockers
        metadata.update(
            {
                "execution_ready": execution_ready,
                "execution_blockers": execution_blockers,
                "alert_eligible": ranking["actionability"] == "alert" and execution_ready,
            }
        )
        metadata.update(
            {
                "backtest_strategy_name": backtest_snapshot.get("strategy_name"),
                "backtest_completed_at": backtest_snapshot.get("completed_at"),
                "backtest_number_of_trades": backtest_snapshot.get("total_trades"),
                "backtest_profit_factor": backtest_snapshot.get("profit_factor"),
                "backtest_annualized_return_pct": backtest_snapshot.get("annualized_return_pct"),
                "backtest_max_drawdown_pct": backtest_snapshot.get("max_drawdown_pct"),
                "backtest_win_rate": backtest_snapshot.get("win_rate"),
                "backtest_expectancy_pct": backtest_snapshot.get("expectancy_pct"),
                "backtest_recent_consistency_score": backtest_snapshot.get("recent_consistency_score"),
                "backtest_credibility_score": backtest_snapshot.get("credibility_score"),
                "backtest_profile_label": backtest_snapshot.get("profile_label"),
                "backtest_recent_vs_long_run_score": backtest_snapshot.get("recent_vs_long_run_score"),
                "backtest_sample_reliability_score": backtest_snapshot.get("sample_reliability_score"),
            }
        )

        return LiveSignalSnapshot(
            symbol=signal.symbol.upper(),
            strategy_name=signal.strategy_name,
            state=SignalState(signal.action.value),
            timeframe=timeframe,
            generated_at=utc_now().isoformat(),
            signal_generated_at=signal.timestamp,
            candle_timestamp=market_data_status["bar_timestamp"],
            rate_timestamp=market_data_status["quote_timestamp"],
            current_price=current_price,
            current_bid=quote.bid,
            current_ask=quote.ask,
            entry_price=float(signal.price or current_price),
            exit_price=float(trade_plan.get("target_2") or signal.take_profit or current_price),
            stop_loss=signal.stop_loss,
            take_profit=float(trade_plan.get("target_2")) if trade_plan.get("target_2") is not None else signal.take_profit,
            targets=targets,
            risk_reward_ratio=resolved_risk_reward,
            signal_role=str(signal.metadata.get("signal_role") or "entry_long"),
            direction_label=str(ranking["direction_label"]),
            confidence_label=str(ranking["confidence_label"]),
            freshness=freshness,
            rationale=signal.rationale,
            score=float(ranking["final_score"]),
            score_breakdown=ranking["score_breakdown"],
            confidence=signal.confidence,
            tradable=execution_ready,
            execution_ready=execution_ready,
            supported=signal.symbol.upper() in set(self.settings.allowed_instruments),
            asset_class="equity",
            pass_reasons=list(filter_outcome.pass_reasons),
            reject_reasons=[],
            indicators={**context.measurements, **intelligence.measurements, **filter_outcome.measurements},
            metadata=metadata,
            backtest_snapshot=backtest_snapshot,
        )

    def _backtest_validation(self, symbol: str, strategy_name: str, timeframe: str | None = None) -> dict[str, Any]:
        if self.backtests is None:
            return {"passes": False, "reason": "no_backtest_repository", "summary": None}
        summary = self._get_latest_backtest_summary(symbol.upper(), strategy_name, timeframe)
        if summary is None:
            return {"passes": False, "reason": "no_backtest_summary", "summary": None}
        metrics = summary.get("metrics", {})
        failures: list[str] = []
        if int(metrics.get("number_of_trades", 0) or 0) < self.settings.min_backtest_trades_for_alerts:
            failures.append("too_few_trades")
        if float(metrics.get("profit_factor", 0.0) or 0.0) < self.settings.min_backtest_profit_factor:
            failures.append("profit_factor_below_threshold")
        if float(metrics.get("annualized_return_pct", 0.0) or 0.0) < self.settings.min_backtest_annualized_return_pct:
            failures.append("annualized_return_below_threshold")
        if float(metrics.get("max_drawdown_pct", 9999.0) or 9999.0) > self.settings.max_backtest_drawdown_pct:
            failures.append("drawdown_above_threshold")
        return {
            "passes": not failures,
            "reason": ",".join(failures) if failures else "passed",
            "summary": summary,
        }

    def _get_latest_backtest_summary(
        self,
        symbol: str,
        strategy_name: str,
        timeframe: str | None,
    ) -> dict[str, Any] | None:
        if self.backtests is None:
            return None
        if timeframe:
            try:
                return self.backtests.get_latest_summary(symbol, strategy_name, timeframe=timeframe)
            except TypeError:
                return self.backtests.get_latest_summary(symbol, strategy_name)
        return self.backtests.get_latest_summary(symbol, strategy_name)

    @staticmethod
    def _compute_risk_reward(signal: Any) -> float | None:
        if signal.price is None or signal.stop_loss is None or signal.take_profit is None:
            return None
        if signal.action.value == "buy":
            risk = max(float(signal.price) - float(signal.stop_loss), 0.01)
            reward = float(signal.take_profit) - float(signal.price)
        else:
            risk = max(float(signal.stop_loss) - float(signal.price), 0.01)
            reward = float(signal.price) - float(signal.take_profit)
        if reward <= 0:
            return None
        return round(reward / risk, 2)

    @staticmethod
    def _bars_for_timeframe(timeframe: str) -> int:
        mapping = {"1d": 400, "1h": 320, "15m": 300, "5m": 320, "1m": 360}
        return mapping.get(timeframe, 250)

    @staticmethod
    def _ranking_key(snapshot: LiveSignalSnapshot) -> tuple[int, int, float, float]:
        alert_eligible = 1 if bool(snapshot.metadata.get("alert_eligible")) else 0
        validated = 1 if bool(snapshot.metadata.get("backtest_validated")) else 0
        confidence = float(snapshot.confidence or 0.0)
        return alert_eligible, validated, snapshot.score, confidence

    @classmethod
    def _add_scan_diagnostic(
        cls,
        rejection_summary: dict[str, int],
        closest_rejections: list[dict[str, Any]],
        *,
        symbol: str,
        timeframe: str,
        strategy_name: str,
        status: str,
        rejection_reasons: list[str],
        final_score: float | None = None,
        measurements: dict[str, Any] | None = None,
    ) -> None:
        reasons = cls._normalize_rejection_reasons(rejection_reasons)
        for reason in reasons:
            cls._increment_rejection(rejection_summary, reason)
        closest_rejections.append(
            {
                "symbol": symbol.upper(),
                "timeframe": timeframe,
                "strategy_name": strategy_name,
                "status": status,
                "score": round(float(final_score), 2) if final_score is not None else None,
                "rejection_reasons": reasons[:5],
                "measurements": cls._diagnostic_measurements(measurements or {}),
            }
        )

    @staticmethod
    def _increment_rejection(rejection_summary: dict[str, int], reason: str) -> None:
        rejection_summary[reason] = rejection_summary.get(reason, 0) + 1

    @staticmethod
    def _normalize_rejection_reasons(rejection_reasons: list[str]) -> list[str]:
        normalized: list[str] = []
        for raw_reason in rejection_reasons or ["unknown_rejection"]:
            for reason in str(raw_reason or "unknown_rejection").split(","):
                cleaned = reason.strip() or "unknown_rejection"
                if cleaned not in normalized:
                    normalized.append(cleaned)
        return normalized or ["unknown_rejection"]

    @classmethod
    def _rank_closest_rejections(cls, closest_rejections: list[dict[str, Any]], *, limit: int = 5) -> list[dict[str, Any]]:
        return sorted(
            closest_rejections,
            key=lambda item: (
                item.get("score") is not None,
                float(item.get("score") or 0.0),
                item.get("symbol") or "",
            ),
            reverse=True,
        )[:limit]

    @staticmethod
    def _diagnostic_measurements(measurements: dict[str, Any]) -> dict[str, Any]:
        keys = [
            "accuracy_score",
            "confirmation_score",
            "false_positive_risk_score",
            "indicator_confluence_score",
            "relative_volume",
            "risk_reward_ratio",
            "market_regime_score",
            "timeframe_alignment_score",
            "verification_reason",
            "freshness_status",
        ]
        compact: dict[str, Any] = {}
        for key in keys:
            value = measurements.get(key)
            if value is None:
                continue
            if isinstance(value, float):
                compact[key] = round(value, 4)
            else:
                compact[key] = value
        return compact

    def _record_scan_decision(
        self,
        *,
        scan_task: str,
        signal: Any,
        timeframe: str,
        status: str,
        final_score: float | None,
        alert_eligible: bool,
        freshness: str | None,
        filter_outcome: FilterOutcome,
        payload: dict[str, Any],
    ) -> None:
        if self.scan_decisions is None:
            return
        self.scan_decisions.create(
            scan_task=scan_task,
            symbol=signal.symbol,
            strategy_name=signal.strategy_name,
            timeframe=timeframe,
            status=status,
            final_score=final_score,
            alert_eligible=alert_eligible,
            freshness=freshness,
            reason_codes=list(filter_outcome.reason_codes),
            rejection_reasons=list(filter_outcome.rejection_reasons),
            payload=payload,
        )

    def _market_data_status(self, *, history: pd.DataFrame, quote: Any) -> dict[str, Any]:
        history_provider = str(history.attrs.get("provider") or "unknown")
        quote_provider = str(getattr(quote, "source", None) or history_provider)
        history_used_fallback = bool(history.attrs.get("used_fallback", False))
        quote_used_fallback = bool(getattr(quote, "used_fallback", False))
        history_from_cache = bool(history.attrs.get("from_cache", False))
        quote_from_cache = bool(getattr(quote, "from_cache", False))
        quote_derived = bool(getattr(quote, "quote_derived_from_history", False))
        quote_age_seconds = getattr(quote, "data_age_seconds", None)
        history_age_seconds = history.attrs.get("data_age_seconds")
        quote_failures: list[str] = []
        history_failures: list[str] = []
        if quote_provider == "unknown":
            quote_failures.append("missing_quote_provider")
        if history_provider == "unknown":
            history_failures.append("missing_history_provider")
        if self.settings.require_primary_provider_for_alerts and quote_used_fallback:
            quote_failures.append("fallback_quote_provider_used")
        if self.settings.require_direct_quote_for_alerts and quote_derived:
            quote_failures.append("quote_derived_from_history")
        if self.settings.require_uncached_market_data_for_alerts and quote_from_cache:
            quote_failures.append("cached_quote_used")
        if quote_age_seconds is not None and float(quote_age_seconds) > float(self.settings.max_market_data_age_seconds):
            quote_failures.append("quote_too_old")
        if self.settings.require_uncached_market_data_for_alerts and history_from_cache:
            history_failures.append("cached_market_data_used")
        if history_age_seconds is not None and float(history_age_seconds) > float(self.settings.max_market_data_age_seconds):
            history_failures.append("market_data_too_old")
        quote_live_verified = not quote_failures and quote_provider != "unknown"
        history_fresh = not history_failures and history_provider != "unknown"
        verification_failures = [*quote_failures, *history_failures]
        verified = quote_live_verified and history_fresh
        if not quote_live_verified and not history_fresh:
            freshness_status = "stale_quote_and_bars"
        elif not quote_live_verified:
            freshness_status = "stale_quote"
        elif not history_fresh:
            freshness_status = "stale_bars"
        else:
            freshness_status = "fresh"
        return {
            "history_provider": history_provider,
            "quote_provider": quote_provider,
            "quote_is_primary": not quote_used_fallback,
            "history_is_primary": not history_used_fallback,
            "quote_used_fallback": quote_used_fallback,
            "history_used_fallback": history_used_fallback,
            "history_from_cache": history_from_cache,
            "quote_from_cache": quote_from_cache,
            "quote_derived": quote_derived,
            "quote_age_seconds": round(float(quote_age_seconds), 3) if quote_age_seconds is not None else None,
            "history_age_seconds": round(float(history_age_seconds), 3) if history_age_seconds is not None else None,
            "quote_timestamp": getattr(quote, "timestamp", None),
            "bar_timestamp": history.iloc[-1]["timestamp"].isoformat() if len(history) else None,
            "quote_live_verified": quote_live_verified,
            "quote_verification_reason": ",".join(quote_failures) if quote_failures else "verified_live_quote",
            "history_fresh": history_fresh,
            "history_freshness_reason": ",".join(history_failures) if history_failures else "history_fresh",
            "freshness_status": freshness_status,
            "verified": verified,
            "verification_reason": ",".join(verification_failures) if verification_failures else "verified_live_market_data",
        }

    def _execution_blockers(
        self,
        *,
        market_data_status: dict[str, Any],
        final_score: float,
        risk_reward_ratio: float | None,
        actionability: str,
    ) -> list[str]:
        blockers: list[str] = []
        if not market_data_status["verified"]:
            blockers.append(str(market_data_status["verification_reason"]))
        if actionability != "alert":
            blockers.append(f"actionability_{actionability}")
        if final_score < float(self.settings.screener_min_final_score_to_alert):
            blockers.append("final_score_below_alert_threshold")
        if risk_reward_ratio is None or float(risk_reward_ratio) < float(self.settings.screener_min_reward_to_risk):
            blockers.append("reward_to_risk_below_threshold")
        return blockers

    def _build_no_trade_snapshot(
        self,
        symbol: str,
        *,
        response: ScreenerRunResponse,
        force_refresh: bool,
    ) -> LiveSignalSnapshot:
        try:
            history = self.market_data.get_history(symbol, timeframe="1d", bars=180, force_refresh=force_refresh)
            quote = self.market_data.get_quote(symbol, timeframe="1d", force_refresh=force_refresh)
        except Exception:
            return self._build_data_unavailable_snapshot(symbol, response=response)
        placeholder_signal = SimpleNamespace(
            strategy_name="market_intelligence",
            metadata={"signal_role": "entry_long", "style": "watchlist"},
            price=quote.last_execution or quote.ask or quote.bid,
            stop_loss=None,
            take_profit=None,
        )
        intelligence = self.intelligence.analyze(
            symbol=symbol,
            timeframe="1d",
            history=history,
            quote=quote,
            signal=placeholder_signal,
            force_refresh=force_refresh,
        )
        market_data_status = self._market_data_status(history=history, quote=quote)
        context = build_market_context(history, quote=quote, signal=placeholder_signal)
        accuracy_profile = build_accuracy_profile(
            history,
            signal=placeholder_signal,
            context=context,
            settings=self.settings,
        )
        recent_decisions = self._recent_scan_decisions(symbol=symbol, limit=40)
        best_rejected = self._best_rejected_setup(recent_decisions)
        top_reasons: list[str] = []
        primary_blocker: str | None = None
        if not market_data_status["verified"]:
            primary_blocker = str(market_data_status["verification_reason"])
            top_reasons = [primary_blocker]
        if best_rejected is not None:
            for reason in best_rejected["rejection_reasons"]:
                if reason not in top_reasons:
                    top_reasons.append(reason)
        else:
            for item in recent_decisions:
                reasons = list(item.rejection_reasons or [])
                if reasons:
                    for reason in reasons:
                        if reason not in top_reasons:
                            top_reasons.append(reason)
                    break
        if not top_reasons:
            if intelligence.momentum_state == "exhausted":
                top_reasons.append("momentum_exhausted_no_entry_trigger")
            else:
                top_reasons.append("no_strategy_setup_triggered")
        top_score = float(best_rejected.get("score") or 0.0) if best_rejected else 0.0
        near_miss_measurements = dict(best_rejected.get("measurements") or {}) if best_rejected else {}
        near_miss_backtest = dict(best_rejected.get("backtest_snapshot") or {}) if best_rejected else {}
        effective_measurements = {**context.measurements, **accuracy_profile.measurements, **near_miss_measurements}
        improvement_guidance = self._no_trade_improvement_guidance(
            reasons=top_reasons,
            measurements=effective_measurements,
            market_data_status=market_data_status,
        )
        if primary_blocker is not None:
            rationale = (
                f"Live market-data gate blocked {symbol}. "
                f"Top blockers: {', '.join(top_reasons[:3])}."
            )
        else:
            rationale = (
                f"No clear edge for {symbol}. "
                f"Top blockers: {', '.join(top_reasons[:3]) if top_reasons else 'current setup is below the profitability threshold'}."
            )
        metadata = {
            "analysis_mode": "single_symbol",
            "verdict": "no_trade",
            "timing_label": "no_trade",
            "market_context_summary": intelligence.summary,
            "market_regime_label": intelligence.market_regime_label,
            "market_risk_mode": intelligence.risk_mode,
            "market_volatility_environment": intelligence.volatility_environment,
            "market_regime_score": intelligence.market_regime_score,
            "higher_timeframe_alignment_score": intelligence.higher_timeframe_alignment_score,
            "lower_timeframe_alignment_score": intelligence.lower_timeframe_alignment_score,
            "timeframe_alignment_score": intelligence.timeframe_alignment_score,
            "relative_strength_vs_market": intelligence.relative_strength_vs_market,
            "relative_strength_vs_sector": intelligence.relative_strength_vs_sector,
            "sector_strength_score": intelligence.sector_strength_score,
            "benchmark_strength_score": intelligence.benchmark_strength_score,
            "time_of_day_score": intelligence.time_of_day_score,
            "momentum_state": intelligence.momentum_state,
            "indicator_confluence_score": effective_measurements.get("indicator_confluence_score"),
            "execution_quality": effective_measurements.get("execution_quality"),
            "accuracy_score": effective_measurements.get("accuracy_score", accuracy_profile.overall_score),
            "entry_location_score": effective_measurements.get("entry_location_score", accuracy_profile.entry_location_score),
            "support_resistance_score": effective_measurements.get(
                "support_resistance_score",
                accuracy_profile.support_resistance_score,
            ),
            "confirmation_score": effective_measurements.get("confirmation_score", accuracy_profile.confirmation_score),
            "false_positive_risk_score": effective_measurements.get(
                "false_positive_risk_score",
                accuracy_profile.false_positive_risk_score,
            ),
            "accuracy_pass_reasons": list(accuracy_profile.pass_reasons),
            "accuracy_rejection_reasons": list(accuracy_profile.rejection_reasons),
            **effective_measurements,
            "analysis_strategy_runs_evaluated": response.evaluated_strategy_runs,
            "no_strategy_setup_triggered": best_rejected is None,
            "data_source": market_data_status["quote_provider"],
            "data_source_quote": market_data_status["quote_provider"],
            "data_source_history": market_data_status["history_provider"],
            "data_source_verified": market_data_status["verified"],
            "data_source_verification_reason": market_data_status["verification_reason"],
            "quote_live_verified": market_data_status["quote_live_verified"],
            "quote_verification_reason": market_data_status["quote_verification_reason"],
            "bars_fresh": market_data_status["history_fresh"],
            "history_freshness_reason": market_data_status["history_freshness_reason"],
            "freshness_status": market_data_status["freshness_status"],
            "quote_timestamp": market_data_status["quote_timestamp"],
            "bar_timestamp": market_data_status["bar_timestamp"],
            "data_gate_blocked": not market_data_status["verified"],
            "trade_plan": {
                "verdict": "no_trade",
                "timing_label": "no_trade",
                "preferred_entry_method": "none",
                "entry_zone_low": None,
                "entry_zone_high": None,
                "confirmation_trigger": improvement_guidance,
                "target_1": None,
                "target_2": None,
                "stretch_target": None,
                "trailing_logic": "No trade active.",
                "invalidation_condition": "No edge until a new setup forms.",
                "hold_style": "watch",
                "position_quality_label": "none",
                "summary": "No-trade verdict.",
                "estimated_reward_to_risk": None,
            },
            "near_miss_setup": best_rejected,
            "top_rejection_reasons": list(dict.fromkeys(top_reasons)),
            "primary_blocker": primary_blocker,
            "analysis_errors": list(response.errors),
            "alert_eligible": False,
            "execution_ready": False,
            "execution_blockers": [primary_blocker] if primary_blocker else ["no_clear_edge"],
        }
        return LiveSignalSnapshot(
            symbol=symbol.upper(),
            strategy_name="market_intelligence",
            state=SignalState.NONE,
            timeframe="1d",
            generated_at=utc_now().isoformat(),
            candle_timestamp=market_data_status["bar_timestamp"],
            rate_timestamp=market_data_status["quote_timestamp"],
            current_price=float(quote.last_execution or quote.ask or quote.bid or 0.0),
            current_bid=quote.bid,
            current_ask=quote.ask,
            entry_price=None,
            exit_price=None,
            stop_loss=None,
            take_profit=None,
            targets=[],
            risk_reward_ratio=None,
            signal_role="none",
            direction_label="no_trade",
            confidence_label="reject",
            freshness="fresh",
            rationale=rationale,
            score=max(min(top_score, 54.0), 35.0) if top_score else 0.0,
            score_breakdown={
                "nearest_rejected_setup": round(top_score, 2) if top_score else 0.0,
                "market_context": round(intelligence.market_regime_score * 100.0, 2),
            },
            confidence=0.25,
            tradable=False,
            execution_ready=False,
            supported=symbol.upper() in set(self.settings.allowed_instruments),
            asset_class="equity",
            pass_reasons=[],
            reject_reasons=list(dict.fromkeys(top_reasons))[:5],
            indicators={**context.measurements, **intelligence.measurements, **accuracy_profile.measurements, **near_miss_measurements},
            metadata=metadata,
            backtest_snapshot=near_miss_backtest,
        )

    def _recent_scan_decisions(self, *, symbol: str, limit: int) -> list[Any]:
        if self.scan_decisions is None or not hasattr(self.scan_decisions, "list"):
            return []
        try:
            return list(self.scan_decisions.list(limit=limit, symbol=symbol, scan_task="single_symbol_analysis"))
        except TypeError:
            try:
                return list(self.scan_decisions.list(limit=limit, symbol=symbol))
            except TypeError:
                return []

    @staticmethod
    def _best_rejected_setup(decisions: list[Any]) -> dict[str, Any] | None:
        rejected = [
            item
            for item in decisions
            if getattr(item, "status", "") in {"rejected", "suppressed", "watchlist"}
            and list(getattr(item, "rejection_reasons", []) or [])
        ]
        if not rejected:
            return None

        def score(item: Any) -> float:
            explicit = getattr(item, "final_score", None)
            if explicit is not None:
                return float(explicit)
            payload = dict(getattr(item, "payload", {}) or {})
            measurements = dict(payload.get("measurements") or {})
            accuracy = float(measurements.get("accuracy_score") or 0.0)
            confluence = float(measurements.get("indicator_confluence_score") or 0.0)
            execution = float(measurements.get("execution_quality") or 0.0)
            regime = float(measurements.get("market_regime_score") or measurements.get("regime_alignment_score") or 0.0)
            return round(((accuracy * 0.35) + (confluence * 0.25) + (execution * 0.20) + (regime * 0.20)) * 100.0, 2)

        best = max(rejected, key=score)
        payload = dict(getattr(best, "payload", {}) or {})
        measurements = dict(payload.get("measurements") or {})
        score_breakdown = dict(payload.get("score_breakdown") or {})
        backtest_snapshot = dict(payload.get("backtest_snapshot") or {})
        return {
            "strategy_name": getattr(best, "strategy_name", None),
            "timeframe": getattr(best, "timeframe", None),
            "status": getattr(best, "status", None),
            "score": score(best),
            "rejection_reasons": list(dict.fromkeys(list(getattr(best, "rejection_reasons", []) or [])))[:6],
            "reason_codes": list(dict.fromkeys(list(getattr(best, "reason_codes", []) or [])))[:12],
            "measurements": measurements,
            "score_breakdown": score_breakdown,
            "backtest_snapshot": backtest_snapshot,
            "created_at": getattr(best, "created_at", None),
        }

    def _no_trade_improvement_guidance(
        self,
        *,
        reasons: list[str],
        measurements: dict[str, Any],
        market_data_status: dict[str, Any],
    ) -> str:
        if not market_data_status["verified"]:
            return "Wait for a direct verified eToro quote and fresh bars before evaluating a trade."

        guidance: list[str] = []
        for reason in reasons:
            note = self._guidance_for_rejection(reason, measurements)
            if note and note not in guidance:
                guidance.append(note)
            if len(guidance) >= 3:
                break
        if not guidance:
            guidance.append("Wait for a cleaner setup with stronger confirmation, better risk/reward, and lower false-positive risk.")
        return " ".join(guidance)

    def _guidance_for_rejection(self, reason: str, measurements: dict[str, Any]) -> str | None:
        def fmt(value: Any) -> str:
            try:
                return f"{float(value):.2f}"
            except (TypeError, ValueError):
                return "n/a"

        mapping = {
            "confidence_below_threshold": f"Confidence must rise above {self.settings.screener_min_confidence:.2f}.",
            "volatility_out_of_range": (
                "ATR volatility must move inside "
                f"{self.settings.screener_min_atr_pct:.2f}-{self.settings.screener_max_atr_pct:.2f}% "
                f"(now {fmt(measurements.get('atr_pct'))}%)."
            ),
            "structure_too_choppy": (
                f"Trend structure must clean up; efficiency ratio needs >= {self.settings.screener_min_efficiency_ratio:.2f} "
                f"(now {fmt(measurements.get('efficiency_ratio'))})."
            ),
            "relative_strength_market_too_low": (
                f"Relative strength vs market must improve above {self.settings.screener_min_relative_strength_vs_market:.2f}% "
                f"(now {fmt(measurements.get('relative_strength_vs_market'))}%)."
            ),
            "relative_strength_sector_too_low": (
                f"Relative strength vs sector must improve above {self.settings.screener_min_relative_strength_vs_sector:.2f}% "
                f"(now {fmt(measurements.get('relative_strength_vs_sector'))}%)."
            ),
            "entry_too_extended": (
                "Wait for a pullback closer to EMA/VWAP; extension must be <= "
                f"{self.settings.screener_max_extension_atr_multiple:.2f} ATR "
                f"(now {fmt(measurements.get('extension_atr_multiple') or measurements.get('entry_extension_atr'))})."
            ),
            "reward_to_risk_too_low": f"Risk/reward must improve above {self.settings.screener_min_reward_to_risk:.2f}.",
            "confirmation_too_weak": (
                f"RSI/VWAP/EMA/MACD/RVOL confirmation must improve above {self.settings.screener_min_confirmation_score:.2f} "
                f"(now {fmt(measurements.get('confirmation_score'))})."
            ),
            "accuracy_score_too_low": (
                f"Entry accuracy must improve above {self.settings.screener_min_accuracy_score:.2f} "
                f"(now {fmt(measurements.get('accuracy_score'))})."
            ),
            "false_positive_risk_too_high": (
                f"False-positive risk must drop below {self.settings.screener_max_false_positive_risk:.2f} "
                f"(now {fmt(measurements.get('false_positive_risk_score'))})."
            ),
            "indicator_confluence_too_low": (
                f"Indicator confluence must improve above {self.settings.screener_min_indicator_confluence:.2f} "
                f"(now {fmt(measurements.get('indicator_confluence_score'))})."
            ),
            "execution_quality_too_low": (
                f"Execution quality must improve above {self.settings.screener_min_execution_quality:.2f} "
                f"(now {fmt(measurements.get('execution_quality'))})."
            ),
            "recent_backtest_consistency_too_low": "Backtest evidence is weak; refresh or improve strategy history before alerting.",
            "backtest_validation_blocked": "Backtest validation must pass before this setup can alert.",
            "backtest_credibility_too_low": "Backtest credibility must improve before this setup can alert.",
            "momentum_exhausted_no_entry_trigger": (
                "Momentum is exhausted without a clean entry trigger; wait for a pullback/reset near EMA or VWAP."
            ),
            "no_strategy_setup_triggered": (
                "No strategy setup fired across the checked timeframes; wait for a fresh breakout, pullback, or VWAP/EMA reclaim trigger."
            ),
        }
        return mapping.get(reason)

    @staticmethod
    def _scan_cancelled(cancel_event: Any | None) -> bool:
        return bool(cancel_event is not None and hasattr(cancel_event, "is_set") and cancel_event.is_set())

    def _build_data_unavailable_snapshot(
        self,
        symbol: str,
        *,
        response: ScreenerRunResponse,
    ) -> LiveSignalSnapshot:
        error_detail = response.errors[0] if response.errors else "market_data_unavailable"
        provider = str(self.settings.primary_market_data_provider or "unknown")
        rationale = (
            f"Data quality insufficient for {symbol}. "
            f"Primary provider request failed: {error_detail}."
        )
        metadata = {
            "analysis_mode": "single_symbol",
            "verdict": "no_trade",
            "timing_label": "data_quality_insufficient",
            "market_context_summary": "market data unavailable",
            "data_source": provider,
            "data_source_quote": provider,
            "data_source_history": provider,
            "data_source_verified": False,
            "data_source_verification_reason": "provider_request_failed",
            "quote_live_verified": False,
            "quote_verification_reason": "provider_request_failed",
            "bars_fresh": False,
            "history_freshness_reason": "provider_request_failed",
            "freshness_status": "unavailable",
            "data_gate_blocked": True,
            "trade_plan": {
                "verdict": "no_trade",
                "timing_label": "data_quality_insufficient",
                "preferred_entry_method": "none",
                "entry_zone_low": None,
                "entry_zone_high": None,
                "confirmation_trigger": "Wait for a successful direct eToro quote and fresh bars.",
                "target_1": None,
                "target_2": None,
                "stretch_target": None,
                "trailing_logic": "No trade active.",
                "invalidation_condition": "No edge until market data is verified.",
                "hold_style": "watch",
                "position_quality_label": "none",
                "summary": "Market data unavailable.",
                "estimated_reward_to_risk": None,
            },
            "top_rejection_reasons": ["provider_request_failed"],
            "primary_blocker": "provider_request_failed",
            "analysis_errors": list(response.errors),
            "alert_eligible": False,
            "execution_ready": False,
            "execution_blockers": ["provider_request_failed"],
        }
        return LiveSignalSnapshot(
            symbol=symbol.upper(),
            strategy_name="market_intelligence",
            state=SignalState.NONE,
            timeframe="1d",
            generated_at=utc_now().isoformat(),
            candle_timestamp=None,
            rate_timestamp=None,
            current_price=None,
            current_bid=None,
            current_ask=None,
            entry_price=None,
            exit_price=None,
            stop_loss=None,
            take_profit=None,
            targets=[],
            risk_reward_ratio=None,
            signal_role="none",
            direction_label="no_trade",
            confidence_label="reject",
            freshness="unavailable",
            rationale=rationale,
            score=0.0,
            score_breakdown={},
            confidence=0.0,
            tradable=False,
            execution_ready=False,
            supported=symbol.upper() in set(self.settings.allowed_instruments),
            asset_class="equity",
            pass_reasons=[],
            reject_reasons=["provider_request_failed"],
            indicators={},
            metadata=metadata,
            backtest_snapshot={},
        )


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
    ) -> BatchBacktestSummary:
        universe = [symbol.upper() for symbol in (symbols or resolve_universe(self.settings, limit=limit))]
        scan_timeframes = [timeframe.lower() for timeframe in (timeframes or ["1d"])]
        requested = set(strategy_names or [])
        errors: list[str] = []
        results: list[dict[str, Any]] = []
        run_count = 0
        engine = BacktestEngine(self.backtests)

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

                for spec in _strategy_specs(self.settings, timeframe=timeframe, requested=requested):
                    run_count += 1
                    strategy = get_strategy(spec.name, **_strategy_kwargs(self.settings, spec))
                    try:
                        result = engine.run(
                            symbol=symbol,
                            strategy=strategy,
                            data=history.copy(),
                            file_path=f"{provider or self.settings.primary_market_data_provider}:{timeframe}:{symbol}",
                            initial_cash=initial_cash,
                        )
                    except Exception as exc:
                        errors.append(f"{symbol} {timeframe} {spec.name}: {exc}")
                        continue
                    result_payload = {
                        "symbol": result.symbol,
                        "strategy_name": result.strategy_name,
                        "timeframe": timeframe,
                        "provider": provider or self.settings.primary_market_data_provider,
                        **result.metrics,
                    }
                    results.append(result_payload)

        aggregate = self._aggregate_metrics(results)
        summary = BatchBacktestSummary(
            generated_at=utc_now().isoformat(),
            symbols_evaluated=len(universe),
            strategy_runs=run_count,
            timeframe=",".join(scan_timeframes),
            provider=provider or self.settings.primary_market_data_provider,
            results=sorted(results, key=lambda item: item.get("annualized_return_pct", 0.0), reverse=True),
            aggregate_metrics=aggregate,
            errors=errors,
        )
        self.logs.log(
            "batch_backtest_run",
            {
                "symbols": len(universe),
                "timeframes": scan_timeframes,
                "strategy_runs": run_count,
                "results": len(results),
                "errors": len(errors),
            },
        )
        return summary

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
