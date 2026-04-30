"""Snapshot builders for the market screener service."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.intelligence import build_trade_plan
from app.live_signal_schema import LiveSignalSnapshot, SignalState
from app.models.screener import ScreenerRunResponse
from app.screener.accuracy import build_accuracy_profile
from app.screener.filters import FilterOutcome, MarketContext, build_market_context
from app.utils.time import utc_now


def snapshot_from_signal(
    service: Any,
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
    resolved_risk_reward = float(risk_reward) if risk_reward is not None else service._compute_risk_reward(signal)
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
    execution_blockers = service._execution_blockers(
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
        supported=signal.symbol.upper() in set(service.settings.allowed_instruments),
        asset_class="equity",
        pass_reasons=list(filter_outcome.pass_reasons),
        reject_reasons=[],
        indicators={**context.measurements, **intelligence.measurements, **filter_outcome.measurements},
        metadata=metadata,
        backtest_snapshot=backtest_snapshot,
    )


def build_no_trade_snapshot(
    service: Any,
    symbol: str,
    *,
    response: ScreenerRunResponse,
    force_refresh: bool,
) -> LiveSignalSnapshot:
    try:
        history = service.market_data.get_history(symbol, timeframe="1d", bars=180, force_refresh=force_refresh)
        quote = service.market_data.get_quote(symbol, timeframe="1d", force_refresh=force_refresh)
    except Exception:
        return service._build_data_unavailable_snapshot(symbol, response=response)
    placeholder_signal = SimpleNamespace(
        strategy_name="market_intelligence",
        metadata={"signal_role": "entry_long", "style": "watchlist"},
        price=quote.last_execution or quote.ask or quote.bid,
        stop_loss=None,
        take_profit=None,
    )
    intelligence = service.intelligence.analyze(
        symbol=symbol,
        timeframe="1d",
        history=history,
        quote=quote,
        signal=placeholder_signal,
        force_refresh=force_refresh,
    )
    market_data_status = service._market_data_status(history=history, quote=quote)
    context = build_market_context(history, quote=quote, signal=placeholder_signal)
    accuracy_profile = build_accuracy_profile(
        history,
        signal=placeholder_signal,
        context=context,
        settings=service.settings,
    )
    recent_decisions = service._recent_scan_decisions(symbol=symbol, limit=40)
    best_rejected = service._best_rejected_setup(recent_decisions)
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
    improvement_guidance = service._no_trade_improvement_guidance(
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
        supported=symbol.upper() in set(service.settings.allowed_instruments),
        asset_class="equity",
        pass_reasons=[],
        reject_reasons=list(dict.fromkeys(top_reasons))[:5],
        indicators={**context.measurements, **intelligence.measurements, **accuracy_profile.measurements, **near_miss_measurements},
        metadata=metadata,
        backtest_snapshot=near_miss_backtest,
    )


def build_data_unavailable_snapshot(
    service: Any,
    symbol: str,
    *,
    response: ScreenerRunResponse,
) -> LiveSignalSnapshot:
    error_detail = response.errors[0] if response.errors else "market_data_unavailable"
    provider = str(service.settings.primary_market_data_provider or "unknown")
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
        supported=symbol.upper() in set(service.settings.allowed_instruments),
        asset_class="equity",
        pass_reasons=[],
        reject_reasons=["provider_request_failed"],
        indicators={},
        metadata=metadata,
        backtest_snapshot={},
    )
