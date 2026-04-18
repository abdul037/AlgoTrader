"""Ranking and backtest-evidence helpers for live screener candidates."""

from __future__ import annotations

from typing import Any

from app.backtesting.metrics import compute_expectancy, summarize_recent_trades


def build_backtest_snapshot(
    summary: dict[str, Any] | None,
    *,
    validated: bool,
    validation_reason: str,
) -> dict[str, Any]:
    """Build a compact backtest evidence payload for a live candidate."""

    if summary is None:
        return {
            "validated": False,
            "validation_reason": validation_reason,
            "strategy_name": None,
            "timeframe_specific": False,
            "total_trades": 0,
            "win_rate": 0.0,
            "profit_factor": 0.0,
            "expectancy_pct": 0.0,
            "average_return_pct": 0.0,
            "max_drawdown_pct": None,
            "annualized_return_pct": None,
            "recent_trade_count": 0,
            "recent_win_rate": 0.0,
            "recent_average_return_pct": 0.0,
            "recent_profit_factor": 0.0,
            "recent_consistency_score": 0.0,
            "sample_reliability_score": 0.0,
            "recent_vs_long_run_score": 0.0,
            "credibility_score": 0.0,
            "profile_label": "unproven",
        }

    metrics = dict(summary.get("metrics") or {})
    trades = list(summary.get("trades") or [])
    expectancy = compute_expectancy(trades)
    recent = summarize_recent_trades(trades)
    recent_consistency_score = round(
        (
            _normalize(metrics.get("win_rate"), 40.0, 70.0)
            + _normalize(metrics.get("profit_factor"), 1.0, 2.2)
            + _normalize(recent.get("recent_average_return_pct"), 0.0, 2.5)
        )
        / 3.0,
        4,
    )
    sample_reliability_score = round(_normalize(metrics.get("number_of_trades"), 10.0, 60.0), 4)
    long_run_return = float(expectancy.get("average_return_pct", 0.0) or 0.0)
    recent_return = float(recent.get("recent_average_return_pct", 0.0) or 0.0)
    recent_vs_long_run_score = round(
        max(0.0, 1.0 - min(abs(recent_return - long_run_return) / 2.5, 1.0)),
        4,
    )
    credibility_score = round(
        (
            sample_reliability_score
            + _normalize(metrics.get("profit_factor"), 1.0, 2.0)
            + _normalize(metrics.get("win_rate"), 42.0, 62.0)
            + recent_consistency_score
            + recent_vs_long_run_score
        )
        / 5.0,
        4,
    )
    if credibility_score >= 0.75:
        profile_label = "credible"
    elif credibility_score >= 0.55:
        profile_label = "developing"
    else:
        profile_label = "fragile"

    return {
        "validated": bool(validated),
        "validation_reason": validation_reason,
        "strategy_name": summary.get("strategy_name"),
        "completed_at": summary.get("completed_at"),
        "timeframe_specific": False,
        "total_trades": int(metrics.get("number_of_trades", 0) or 0),
        "win_rate": float(metrics.get("win_rate", 0.0) or 0.0),
        "profit_factor": float(metrics.get("profit_factor", 0.0) or 0.0),
        "expectancy_pct": float(expectancy.get("expectancy_pct", 0.0) or 0.0),
        "average_return_pct": float(expectancy.get("average_return_pct", 0.0) or 0.0),
        "max_drawdown_pct": _optional_float(metrics.get("max_drawdown_pct")),
        "annualized_return_pct": _optional_float(metrics.get("annualized_return_pct")),
        "recent_trade_count": int(recent.get("recent_trade_count", 0) or 0),
        "recent_win_rate": float(recent.get("recent_win_rate", 0.0) or 0.0),
        "recent_average_return_pct": float(recent.get("recent_average_return_pct", 0.0) or 0.0),
        "recent_profit_factor": float(recent.get("recent_profit_factor", 0.0) or 0.0),
        "recent_consistency_score": recent_consistency_score,
        "sample_reliability_score": sample_reliability_score,
        "recent_vs_long_run_score": recent_vs_long_run_score,
        "credibility_score": credibility_score,
        "profile_label": profile_label,
    }


def rank_live_signal(
    *,
    settings: Any,
    signal: Any,
    context: Any,
    backtest_snapshot: dict[str, Any],
    intelligence: Any | None = None,
    watchlist_only: bool,
    freshness: str = "fresh",
) -> dict[str, Any]:
    """Blend live setup quality and backtest evidence into a final 0-100 score."""

    rr = float(signal.metadata.get("risk_reward_ratio") or 0.0)
    indicator_confluence = _clamp01(signal.metadata.get("indicator_confluence_score"))
    trend_quality = _clamp01(signal.metadata.get("trend_quality"))
    momentum_quality = _clamp01(signal.metadata.get("momentum_quality"))
    liquidity_quality = _clamp01(signal.metadata.get("liquidity_quality"))
    execution_quality = _clamp01(signal.metadata.get("execution_quality"))
    spread_score = (
        1.0
        if context.spread_bps is None
        else max(0.0, 1.0 - (float(context.spread_bps) / max(float(settings.screener_max_spread_bps), 1.0)))
    )
    components = {
        "setup_quality": max(_clamp01(float(signal.confidence or 0.0)), indicator_confluence * 0.9),
        "trend_strength": max(
            _normalize(context.trend_strength_pct, settings.screener_min_trend_strength_pct, max(settings.screener_min_trend_strength_pct * 4, 1.0)),
            trend_quality,
        ),
        "momentum_confirmation": (
            max(_normalize(context.momentum_pct, 0.2, 4.0), momentum_quality)
            + _normalize(context.relative_volume, 1.0, 2.2)
        )
        / 2.0,
        "liquidity_quality": (
            max(_normalize(context.average_volume, settings.screener_min_average_volume, settings.screener_min_average_volume * 3), liquidity_quality)
            + max(_normalize(context.average_dollar_volume, settings.screener_min_average_dollar_volume, settings.screener_min_average_dollar_volume * 4), liquidity_quality)
            + spread_score
        )
        / 3.0,
        "volatility_suitability": _volatility_score(
            atr_pct=context.atr_pct,
            minimum=float(settings.screener_min_atr_pct),
            maximum=float(settings.screener_max_atr_pct),
        ),
        "reward_to_risk": _normalize(rr, settings.screener_min_reward_to_risk, settings.screener_min_reward_to_risk * 2.5),
        "execution_quality": max(spread_score, execution_quality),
        "indicator_confluence": indicator_confluence,
        "market_regime": _clamp01(getattr(intelligence, "market_regime_score", context.regime_alignment_score)),
        "higher_timeframe_alignment": _clamp01(getattr(intelligence, "timeframe_alignment_score", 0.5)),
        "relative_strength_market": _normalize(getattr(intelligence, "relative_strength_vs_market", 0.0), 0.0, 8.0),
        "relative_strength_sector": _normalize(getattr(intelligence, "relative_strength_vs_sector", 0.0), -0.5, 6.0),
        "time_of_day": _clamp01(getattr(intelligence, "time_of_day_score", 0.75)),
        "signal_freshness": _freshness_score(freshness),
        "backtest_win_rate": _normalize(backtest_snapshot.get("win_rate"), 40.0, 65.0),
        "backtest_profit_factor": _normalize(backtest_snapshot.get("profit_factor"), 1.0, 2.2),
        "backtest_sample_size": _normalize(
            backtest_snapshot.get("total_trades"),
            settings.min_backtest_trades_for_alerts,
            max(settings.min_backtest_trades_for_alerts * 3, 30),
        ),
        "backtest_recent_consistency": _clamp01(backtest_snapshot.get("recent_consistency_score")),
        "backtest_credibility": _clamp01(backtest_snapshot.get("credibility_score")),
        "regime_alignment": _clamp01(context.regime_alignment_score),
    }
    weights = {
        "setup_quality": float(settings.screener_score_weight_setup_quality),
        "trend_strength": float(settings.screener_score_weight_trend_strength),
        "momentum_confirmation": float(settings.screener_score_weight_momentum_confirmation),
        "liquidity_quality": float(settings.screener_score_weight_liquidity_quality),
        "volatility_suitability": float(settings.screener_score_weight_volatility_suitability),
        "reward_to_risk": float(settings.screener_score_weight_reward_to_risk),
        "execution_quality": float(settings.screener_score_weight_execution_quality),
        "indicator_confluence": float(settings.screener_score_weight_indicator_confluence),
        "market_regime": float(settings.screener_score_weight_market_regime),
        "higher_timeframe_alignment": float(settings.screener_score_weight_higher_timeframe_alignment),
        "relative_strength_market": float(settings.screener_score_weight_relative_strength_market),
        "relative_strength_sector": float(settings.screener_score_weight_relative_strength_sector),
        "time_of_day": float(settings.screener_score_weight_time_of_day),
        "signal_freshness": float(settings.screener_score_weight_signal_freshness),
        "backtest_win_rate": float(settings.screener_score_weight_backtest_win_rate),
        "backtest_profit_factor": float(settings.screener_score_weight_backtest_profit_factor),
        "backtest_sample_size": float(settings.screener_score_weight_backtest_sample_size),
        "backtest_recent_consistency": float(settings.screener_score_weight_backtest_recent_consistency),
        "backtest_credibility": float(settings.screener_score_weight_backtest_credibility),
        "regime_alignment": float(settings.screener_score_weight_regime_alignment),
    }
    weighted = {name: round(value * weights[name], 2) for name, value in components.items()}
    total_weight = max(sum(weights.values()), 1.0)
    final_score = round((sum(weighted.values()) / total_weight) * 100.0, 2)

    direction_label = "sell" if signal.action.value == "sell" else "buy"
    actionability = "alert"
    if final_score < float(settings.screener_min_final_score_to_keep):
        direction_label = "reject"
        actionability = "reject"
    elif watchlist_only or final_score < float(settings.screener_min_final_score_to_alert):
        direction_label = "watchlist"
        actionability = "watchlist"
        if watchlist_only:
            final_score = round(final_score * 0.82, 2)

    return {
        "final_score": final_score,
        "score_breakdown": weighted,
        "confidence_label": _confidence_label(final_score),
        "direction_label": direction_label,
        "actionability": actionability,
    }


def freshness_for_decision(
    previous_decision: Any | None,
    *,
    final_score: float,
    minimum_improvement: float,
) -> tuple[str, bool]:
    """Return a freshness label and whether the candidate should be suppressed."""

    if previous_decision is None:
        return "fresh", False

    previous_score = float(previous_decision.final_score or 0.0)
    improvement = final_score - previous_score
    if improvement >= float(minimum_improvement):
        return "repeated_upgraded", False
    return "repeated_flat", True


def _confidence_label(final_score: float) -> str:
    if final_score >= 85:
        return "high_conviction"
    if final_score >= 70:
        return "actionable"
    if final_score >= 55:
        return "watchlist"
    return "reject"


def _volatility_score(*, atr_pct: float, minimum: float, maximum: float) -> float:
    if atr_pct <= 0 or maximum <= minimum:
        return 0.0
    if atr_pct < minimum or atr_pct > maximum:
        return 0.0
    midpoint = (minimum + maximum) / 2.0
    half_range = max((maximum - minimum) / 2.0, 0.01)
    distance = abs(atr_pct - midpoint) / half_range
    return round(max(0.0, 1.0 - distance), 4)


def _normalize(value: Any, low: float, high: float) -> float:
    if value is None:
        return 0.0
    low = float(low)
    high = max(float(high), low + 0.0001)
    numeric = float(value)
    if numeric <= low:
        return 0.0
    if numeric >= high:
        return 1.0
    return round((numeric - low) / (high - low), 4)


def _clamp01(value: Any) -> float:
    if value is None:
        return 0.0
    numeric = float(value)
    if numeric <= 0:
        return 0.0
    if numeric >= 1:
        return 1.0
    return round(numeric, 4)


def _optional_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _freshness_score(freshness: str) -> float:
    normalized = freshness.strip().lower() if freshness else "fresh"
    if normalized == "fresh":
        return 1.0
    if normalized == "repeated_upgraded":
        return 0.88
    if normalized == "repeated_flat":
        return 0.35
    return 0.6
