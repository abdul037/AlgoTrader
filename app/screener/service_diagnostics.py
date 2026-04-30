"""Diagnostics and operator guidance helpers for the market screener service."""

from __future__ import annotations

from typing import Any

from app.live_signal_schema import LiveSignalSnapshot
from app.screener.filters import FilterOutcome


def ranking_key(snapshot: LiveSignalSnapshot) -> tuple[int, int, float, float]:
    alert_eligible = 1 if bool(snapshot.metadata.get("alert_eligible")) else 0
    validated = 1 if bool(snapshot.metadata.get("backtest_validated")) else 0
    confidence = float(snapshot.confidence or 0.0)
    return alert_eligible, validated, snapshot.score, confidence


def add_scan_diagnostic(
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
    reasons = normalize_rejection_reasons(rejection_reasons)
    for reason in reasons:
        increment_rejection(rejection_summary, reason)
    closest_rejections.append(
        {
            "symbol": symbol.upper(),
            "timeframe": timeframe,
            "strategy_name": strategy_name,
            "status": status,
            "score": round(float(final_score), 2) if final_score is not None else None,
            "rejection_reasons": reasons[:5],
            "measurements": diagnostic_measurements(measurements or {}),
        }
    )


def increment_rejection(rejection_summary: dict[str, int], reason: str) -> None:
    rejection_summary[reason] = rejection_summary.get(reason, 0) + 1


def normalize_rejection_reasons(rejection_reasons: list[str]) -> list[str]:
    normalized: list[str] = []
    for raw_reason in rejection_reasons or ["unknown_rejection"]:
        for reason in str(raw_reason or "unknown_rejection").split(","):
            cleaned = reason.strip() or "unknown_rejection"
            if cleaned not in normalized:
                normalized.append(cleaned)
    return normalized or ["unknown_rejection"]


def rank_closest_rejections(closest_rejections: list[dict[str, Any]], *, limit: int = 5) -> list[dict[str, Any]]:
    return sorted(
        closest_rejections,
        key=lambda item: (
            item.get("score") is not None,
            float(item.get("score") or 0.0),
            item.get("symbol") or "",
        ),
        reverse=True,
    )[:limit]


def diagnostic_measurements(measurements: dict[str, Any]) -> dict[str, Any]:
    keys = [
        "side",
        "near_miss_side",
        "alternate_side",
        "alternate_side_score",
        "accuracy_score",
        "confirmation_score",
        "false_positive_risk_score",
        "indicator_confluence_score",
        "relative_volume",
        "rsi",
        "adx",
        "current_price",
        "atr",
        "ema20",
        "vwap",
        "watchlist_trigger",
        "indicative_entry",
        "indicative_stop",
        "indicative_target",
        "indicative_rr",
        "indicative_target_move_pct",
        "extension_atr",
        "body_to_range",
        "close_location",
        "close_location_short",
        "pass_ratio",
        "passed_checks",
        "total_checks",
        "breakout_level",
        "breakout_gap_atr",
        "breakout_tolerance_atr",
        "breakout_confirmed",
        "minimum_relative_volume",
        "minimum_relative_volume_relaxed",
        "session_volume_ratio",
        "session_volume_floor",
        "volume_check_mode",
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


def record_scan_decision(
    service: Any,
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
    if service.scan_decisions is None:
        return
    service.scan_decisions.create(
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


def market_data_status(service: Any, *, history: Any, quote: Any) -> dict[str, Any]:
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
    if service.settings.require_primary_provider_for_alerts and quote_used_fallback:
        quote_failures.append("fallback_quote_provider_used")
    if service.settings.require_direct_quote_for_alerts and quote_derived:
        quote_failures.append("quote_derived_from_history")
    if service.settings.require_uncached_market_data_for_alerts and quote_from_cache:
        quote_failures.append("cached_quote_used")
    if quote_age_seconds is not None and float(quote_age_seconds) > float(service.settings.max_market_data_age_seconds):
        quote_failures.append("quote_too_old")
    if service.settings.require_uncached_market_data_for_alerts and history_from_cache:
        history_failures.append("cached_market_data_used")
    if history_age_seconds is not None and float(history_age_seconds) > float(service.settings.max_market_data_age_seconds):
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


def execution_blockers(
    service: Any,
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
    if final_score < float(service.settings.screener_min_final_score_to_alert):
        blockers.append("final_score_below_alert_threshold")
    if risk_reward_ratio is None or float(risk_reward_ratio) < float(service.settings.screener_min_reward_to_risk):
        blockers.append("reward_to_risk_below_threshold")
    return blockers


def recent_scan_decisions(service: Any, *, symbol: str, limit: int) -> list[Any]:
    if service.scan_decisions is None or not hasattr(service.scan_decisions, "list"):
        return []
    try:
        return list(service.scan_decisions.list(limit=limit, symbol=symbol, scan_task="single_symbol_analysis"))
    except TypeError:
        try:
            return list(service.scan_decisions.list(limit=limit, symbol=symbol))
        except TypeError:
            return []


def best_rejected_setup(decisions: list[Any]) -> dict[str, Any] | None:
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


def no_trade_improvement_guidance(
    service: Any,
    *,
    reasons: list[str],
    measurements: dict[str, Any],
    market_data_status: dict[str, Any],
) -> str:
    if not market_data_status["verified"]:
        return "Wait for a direct verified eToro quote and fresh bars before evaluating a trade."

    guidance: list[str] = []
    for reason in reasons:
        note = guidance_for_rejection(service, reason, measurements)
        if note and note not in guidance:
            guidance.append(note)
        if len(guidance) >= 3:
            break
    if not guidance:
        guidance.append("Wait for a cleaner setup with stronger confirmation, better risk/reward, and lower false-positive risk.")
    return " ".join(guidance)


def guidance_for_rejection(service: Any, reason: str, measurements: dict[str, Any]) -> str | None:
    def fmt(value: Any) -> str:
        try:
            return f"{float(value):.2f}"
        except (TypeError, ValueError):
            return "n/a"

    mapping = {
        "confidence_below_threshold": f"Confidence must rise above {service.settings.screener_min_confidence:.2f}.",
        "volatility_out_of_range": (
            "ATR volatility must move inside "
            f"{service.settings.screener_min_atr_pct:.2f}-{service.settings.screener_max_atr_pct:.2f}% "
            f"(now {fmt(measurements.get('atr_pct'))}%)."
        ),
        "structure_too_choppy": (
            f"Trend structure must clean up; efficiency ratio needs >= {service.settings.screener_min_efficiency_ratio:.2f} "
            f"(now {fmt(measurements.get('efficiency_ratio'))})."
        ),
        "relative_strength_market_too_low": (
            f"Relative strength vs market must improve above {service.settings.screener_min_relative_strength_vs_market:.2f}% "
            f"(now {fmt(measurements.get('relative_strength_vs_market'))}%)."
        ),
        "relative_strength_sector_too_low": (
            f"Relative strength vs sector must improve above {service.settings.screener_min_relative_strength_vs_sector:.2f}% "
            f"(now {fmt(measurements.get('relative_strength_vs_sector'))}%)."
        ),
        "entry_too_extended": (
            "Wait for a pullback closer to EMA/VWAP; extension must be <= "
            f"{service.settings.screener_max_extension_atr_multiple:.2f} ATR "
            f"(now {fmt(measurements.get('extension_atr_multiple') or measurements.get('entry_extension_atr'))})."
        ),
        "reward_to_risk_too_low": f"Risk/reward must improve above {service.settings.screener_min_reward_to_risk:.2f}.",
        "confirmation_too_weak": (
            f"RSI/VWAP/EMA/MACD/RVOL confirmation must improve above {service.settings.screener_min_confirmation_score:.2f} "
            f"(now {fmt(measurements.get('confirmation_score'))})."
        ),
        "accuracy_score_too_low": (
            f"Entry accuracy must improve above {service.settings.screener_min_accuracy_score:.2f} "
            f"(now {fmt(measurements.get('accuracy_score'))})."
        ),
        "false_positive_risk_too_high": (
            f"False-positive risk must drop below {service.settings.screener_max_false_positive_risk:.2f} "
            f"(now {fmt(measurements.get('false_positive_risk_score'))})."
        ),
        "indicator_confluence_too_low": (
            f"Indicator confluence must improve above {service.settings.screener_min_indicator_confluence:.2f} "
            f"(now {fmt(measurements.get('indicator_confluence_score'))})."
        ),
        "execution_quality_too_low": (
            f"Execution quality must improve above {service.settings.screener_min_execution_quality:.2f} "
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


def scan_cancelled(cancel_event: Any | None) -> bool:
    return bool(cancel_event is not None and hasattr(cancel_event, "is_set") and cancel_event.is_set())
