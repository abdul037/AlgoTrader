"""Weak-valid / near-miss promotion helpers for the universe scan.

Extracted from ``service_scan`` to keep the scan orchestrator focused. These
helpers evaluate whether a rejected or weak signal should be promoted to a
paper-exploration entry, and build the diagnostic signals used along the way.
They are pure functions over the passed ``service``/``settings``/``context``
objects and hold no module state beyond the reason-alias table.
"""

from __future__ import annotations

from contextlib import suppress
from datetime import datetime
from datetime import time as local_time
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.models.signal import Signal, SignalAction
from app.screener.filters import FilterOutcome
from app.screener.profiles import (
    effective_auto_execution_min_score,
    paper_exploration_profile_enabled,
)
from app.utils.time import utc_now

def _near_miss_allowed_reasons(settings: Any) -> set[str]:
    return {
        str(item).strip().lower()
        for item in (getattr(settings, "paper_near_miss_allowed_reasons", []) or [])
        if str(item).strip()
    }


def _weak_valid_allowed_reasons(settings: Any) -> set[str]:
    return {
        str(item).strip().lower()
        for item in (getattr(settings, "paper_supervised_weak_valid_allowed_reasons", []) or [])
        if str(item).strip()
    }


_WEAK_VALID_REASON_ALIASES = {
    "momentum_not_constructive": "confirmation_too_weak",
}


def _is_strategy_emitted_weak_valid_signal(signal: Any) -> bool:
    metadata = dict(getattr(signal, "metadata", {}) or {})
    classification = str(metadata.get("signal_classification") or "").strip().lower()
    source = str(metadata.get("source") or "").strip().lower()
    return classification == "supervised_weak_valid" or source == "supervised_weak_valid"


def _effective_weak_valid_reasons(signal: Any, reasons: list[str]) -> list[str]:
    metadata = dict(getattr(signal, "metadata", {}) or {})
    if _is_strategy_emitted_weak_valid_signal(signal):
        weak_reasons = [
            _WEAK_VALID_REASON_ALIASES.get(str(item).strip().lower(), str(item).strip().lower())
            for item in (metadata.get("weak_signal_reasons") or metadata.get("supervised_weak_valid_reasons") or [])
            if str(item).strip()
        ]
        if weak_reasons:
            extras = [
                _WEAK_VALID_REASON_ALIASES.get(str(item).strip().lower(), str(item).strip().lower())
                for item in reasons
                if str(item).strip().lower() in {"final_score_below_auto_threshold", "final_score_below_keep_threshold"}
            ]
            return list(dict.fromkeys([*weak_reasons, *extras]))
    return [
        _WEAK_VALID_REASON_ALIASES.get(str(item).strip().lower(), str(item).strip().lower())
        for item in reasons
        if str(item).strip()
    ]


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _diagnostic_weak_valid_signal(
    *,
    symbol: str,
    strategy_name: str,
    timeframe: str,
    diagnostics: dict[str, Any],
    quote: Any,
    context: Any,
    rejection_reasons: list[str],
) -> Signal | None:
    measurements = dict(diagnostics.get("measurements") or {})
    side = str(measurements.get("side") or diagnostics.get("side") or "long").strip().lower()
    if side not in {"long", "buy"}:
        return None
    entry = _float_or_none(measurements.get("indicative_entry"))
    if entry is None:
        entry = _float_or_none(measurements.get("current_price"))
    if entry is None:
        entry = _float_or_none(getattr(quote, "last_execution", None) or getattr(quote, "ask", None) or getattr(quote, "bid", None))
    stop = _float_or_none(measurements.get("indicative_stop"))
    target = _float_or_none(measurements.get("indicative_target"))
    if entry is None or stop is None or target is None or not (stop < entry < target):
        return None
    rr = _float_or_none(measurements.get("indicative_rr"))
    if rr is None and entry > stop:
        rr = (target - entry) / (entry - stop)
    if rr is None or rr < 1.0:
        return None
    score = _float_or_none(diagnostics.get("score"))
    confidence = None if score is None else max(0.0, min(score / 100.0, 1.0))
    weak_reasons = [
        _WEAK_VALID_REASON_ALIASES.get(str(reason).strip().lower(), str(reason).strip().lower())
        for reason in rejection_reasons
        if str(reason).strip()
    ]
    return Signal(
        symbol=symbol,
        strategy_name=strategy_name,
        action=SignalAction.BUY,
        confidence=confidence,
        price=entry,
        stop_loss=stop,
        take_profit=target,
        rationale=(
            f"Supervised diagnostic weak-valid {strategy_name} setup on {timeframe}; "
            "strategy diagnostics supplied a real long entry, stop, and target."
        ),
        metadata={
            "signal_role": "entry_long",
            "signal_classification": "supervised_weak_valid",
            "source": "supervised_weak_valid",
            "diagnostic_supervised_weak_valid": True,
            "supervised_approval_required": True,
            "production_qualified": False,
            "weak_signal_reasons": weak_reasons or ["confirmation_too_weak"],
            "weak_signal_setup_anchor": True,
            "risk_reward_ratio": rr,
            "indicator_confluence_score": measurements.get("indicator_confluence_score"),
            "style": measurements.get("style") or "diagnostic",
            "timeframe": timeframe,
        },
    )


def _diagnostic_intelligence(context: Any, diagnostics: dict[str, Any]) -> Any:
    measurements = dict(diagnostics.get("measurements") or {})
    return SimpleNamespace(
        summary="Diagnostic supervised weak-valid setup.",
        market_regime_label=measurements.get("market_regime_label") or "unknown",
        risk_mode=measurements.get("risk_mode") or "paper_exploration",
        volatility_environment=measurements.get("volatility_environment") or "unknown",
        market_regime_score=_float_or_none(measurements.get("market_regime_score")) or getattr(context, "regime_alignment_score", 0.5),
        higher_timeframe_alignment_score=_float_or_none(measurements.get("higher_timeframe_alignment_score")) or 0.5,
        lower_timeframe_alignment_score=_float_or_none(measurements.get("lower_timeframe_alignment_score")) or 0.5,
        timeframe_alignment_score=_float_or_none(measurements.get("timeframe_alignment_score")) or 0.5,
        relative_strength_vs_market=_float_or_none(measurements.get("relative_strength_vs_market")) or 0.0,
        relative_strength_vs_sector=_float_or_none(measurements.get("relative_strength_vs_sector")) or 0.0,
        sector_strength_score=_float_or_none(measurements.get("sector_strength_score")) or 0.5,
        benchmark_strength_score=_float_or_none(measurements.get("benchmark_strength_score")) or 0.5,
        time_of_day_score=_float_or_none(measurements.get("time_of_day_score")) or 0.75,
        momentum_state=measurements.get("momentum_state") or "diagnostic",
        measurements=measurements,
    )


def _regular_market_hours_open(settings: Any) -> bool:
    if not bool(getattr(settings, "paper_exploration_require_regular_hours", True)):
        return True
    try:
        zone = ZoneInfo(str(getattr(settings, "schedule_timezone", "America/New_York") or "America/New_York"))
    except ZoneInfoNotFoundError:
        zone = ZoneInfo("America/New_York")
    now = utc_now().astimezone(zone)
    return now.weekday() < 5 and local_time(9, 30) <= now.time() < local_time(16, 0)


def _weak_valid_daily_count(service: Any) -> int:
    repository = getattr(service, "scan_decisions", None)
    if repository is None or not hasattr(repository, "list"):
        return 0
    today = utc_now().date()
    count = 0
    with suppress(Exception):
        for row in repository.list(limit=5000):
            if str(getattr(row, "status", "") or "").lower() != "candidate":
                continue
            payload = dict(getattr(row, "payload", {}) or {})
            metadata = dict(payload.get("metadata") or {})
            if str(metadata.get("source") or "").lower() != "supervised_weak_valid":
                continue
            if not bool(metadata.get("supervised_weak_valid_promoted_to_candidate")):
                continue
            created_at = getattr(row, "created_at", None)
            if created_at is None:
                count += 1
                continue
            if isinstance(created_at, str):
                created_date = created_at.replace("Z", "+00:00")
                created_date = datetime.fromisoformat(created_date).date()
            else:
                created_date = created_at.date()
            if created_date == today:
                count += 1
    return count


def _weak_valid_symbol_blockers(service: Any, symbol: str) -> list[str]:
    blockers: list[str] = []
    normalized = str(symbol or "").upper().strip()
    if not normalized:
        return ["supervised_weak_valid_missing_symbol"]
    if normalized in {str(item).upper().strip() for item in getattr(service.settings, "blocked_instruments", []) or []}:
        blockers.append("symbol_blocked")
    auto_trading = getattr(service, "auto_trading", None)
    safety = getattr(auto_trading, "safety", None)
    if safety is not None and hasattr(safety, "is_blacklisted"):
        try:
            if safety.is_blacklisted(normalized):
                blockers.append("symbol_blacklisted")
        except Exception:  # noqa: BLE001
            blockers.append("symbol_blacklist_check_failed")
    alpaca = getattr(auto_trading, "alpaca", None)
    if alpaca is not None and hasattr(alpaca, "is_supported_equity"):
        try:
            if not bool(alpaca.is_supported_equity(normalized)):
                blockers.append("unsupported_equity")
        except Exception:  # noqa: BLE001
            blockers.append("unsupported_equity_check_failed")
    return blockers


def _paper_near_miss_blockers(
    service: Any,
    *,
    signal: Any,
    context: Any,
    market_data_status: dict[str, Any],
    filter_outcome: FilterOutcome,
    ranking: dict[str, Any],
    reasons: list[str],
) -> list[str]:
    settings = service.settings
    blockers: list[str] = []
    if not bool(getattr(settings, "paper_near_miss_promotion_enabled", False)):
        blockers.append("paper_near_miss_disabled")
    if not paper_exploration_profile_enabled(settings):
        blockers.append("paper_exploration_profile_inactive")
    action_value = getattr(getattr(signal, "action", None), "value", getattr(signal, "action", ""))
    if str(action_value).lower() != "buy":
        blockers.append("paper_near_miss_long_only")
    if str(getattr(signal, "metadata", {}).get("signal_role") or "entry_long").lower() == "entry_short":
        blockers.append("paper_near_miss_short_blocked")
    try:
        entry = float(getattr(signal, "price", None) or getattr(context, "current_price", 0.0) or 0.0)
    except (TypeError, ValueError):
        entry = 0.0
    stop = getattr(signal, "stop_loss", None)
    target = getattr(signal, "take_profit", None)
    if stop is None or target is None:
        blockers.append("paper_near_miss_bracket_missing")
    else:
        try:
            if not (float(stop) < entry < float(target)):
                blockers.append("paper_near_miss_invalid_bracket")
        except (TypeError, ValueError):
            blockers.append("paper_near_miss_invalid_bracket")
    if not bool(market_data_status.get("verified", False)):
        blockers.append(str(market_data_status.get("verification_reason") or "market_data_unverified"))
    spread_bps = getattr(context, "spread_bps", None)
    if spread_bps is None:
        blockers.append("paper_near_miss_spread_unavailable")
    elif float(spread_bps) > float(getattr(settings, "screener_max_spread_bps", 50.0)):
        blockers.append("paper_near_miss_spread_too_wide")
    risk_reward = getattr(signal, "metadata", {}).get("risk_reward_ratio")
    if risk_reward is None:
        risk_reward = service._compute_risk_reward(signal)
    if risk_reward is None or float(risk_reward) < float(service.effective_settings.screener_min_reward_to_risk):
        blockers.append("paper_near_miss_reward_to_risk_too_low")
    near_miss_rvol_floor = float(getattr(settings, "paper_exploration_near_miss_min_relative_volume", 0.75))
    if float(getattr(context, "relative_volume", 0.0) or 0.0) < near_miss_rvol_floor:
        blockers.append("paper_near_miss_relative_volume_too_low")
    allowed = _near_miss_allowed_reasons(settings)
    normalized_reasons = {str(reason).strip().lower() for reason in reasons if str(reason).strip()}
    unsupported_reasons = sorted(normalized_reasons - allowed)
    if unsupported_reasons:
        blockers.append("paper_near_miss_unsupported_reasons:" + ",".join(unsupported_reasons))
    if filter_outcome.watchlist_only:
        blockers.append("paper_near_miss_watchlist_filter")
    score = float(ranking.get("final_score") or 0.0)
    minimum = effective_auto_execution_min_score(settings) - float(getattr(settings, "paper_near_miss_max_score_gap", 5.0) or 0.0)
    if score < minimum:
        blockers.append("paper_near_miss_score_gap_too_large")
    return blockers


def _maybe_promote_paper_near_miss(
    service: Any,
    *,
    signal: Any,
    quote: Any,
    timeframe: str,
    context: Any,
    intelligence: Any,
    market_data_status: dict[str, Any],
    filter_outcome: FilterOutcome,
    backtest_snapshot: dict[str, Any],
    ranking: dict[str, Any],
    freshness: str,
    reasons: list[str],
) -> Any | None:
    blockers = _paper_near_miss_blockers(
        service,
        signal=signal,
        context=context,
        market_data_status=market_data_status,
        filter_outcome=filter_outcome,
        ranking=ranking,
        reasons=reasons,
    )
    attempt_payload = {
        "symbol": getattr(signal, "symbol", None),
        "strategy_name": getattr(signal, "strategy_name", None),
        "timeframe": timeframe,
        "promoted_to_candidate": not blockers,
        "promotion_blockers": list(blockers),
        "reasons": list(dict.fromkeys(reasons)),
        "final_score": float(ranking.get("final_score") or 0.0),
        "relative_volume": float(getattr(context, "relative_volume", 0.0) or 0.0),
        "min_relative_volume": float(
            getattr(service.settings, "paper_exploration_near_miss_min_relative_volume", 0.75)
        ),
        "score_gap": round(
            effective_auto_execution_min_score(service.settings) - float(ranking.get("final_score") or 0.0),
            4,
        ),
    }
    logger = getattr(service, "logs", None)
    if logger is not None:
        with suppress(Exception):
            logger.log("paper_near_miss_promotion_attempt", attempt_payload)
    if blockers:
        return None
    snapshot = service._snapshot_from_signal(
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
    original_metadata = dict(snapshot.metadata or {})
    metadata = {
        **original_metadata,
        "alert_eligible": True,
        "execution_ready": True,
        "execution_blockers": [],
        "paper_near_miss_original_execution_blockers": list(original_metadata.get("execution_blockers") or []),
        "paper_near_miss_original_actionability": ranking.get("actionability"),
        "paper_near_miss_reasons": list(dict.fromkeys(reasons)),
        "paper_near_miss_score_gap": round(
            effective_auto_execution_min_score(service.settings) - float(ranking.get("final_score") or 0.0),
            4,
        ),
        "paper_near_miss_min_relative_volume": float(
            getattr(service.settings, "paper_exploration_near_miss_min_relative_volume", 0.75)
        ),
        "paper_near_miss_promoted_to_candidate": True,
        "paper_near_miss_promotion_blockers": [],
        "production_qualified": False,
        "signal_classification": "paper_near_miss",
        "source": "paper_near_miss",
    }
    return snapshot.model_copy(
        update={
            "execution_ready": True,
            "tradable": True,
            "direction_label": "buy",
            "metadata": metadata,
            "reject_reasons": list(dict.fromkeys(reasons)),
        }
    )


def _paper_supervised_weak_valid_blockers(
    service: Any,
    *,
    signal: Any,
    context: Any,
    market_data_status: dict[str, Any],
    filter_outcome: FilterOutcome,
    ranking: dict[str, Any],
    reasons: list[str],
    weak_valid_scan_count: int,
    weak_valid_daily_count: int,
) -> list[str]:
    settings = service.settings
    blockers: list[str] = []
    if not bool(getattr(settings, "paper_supervised_weak_valid_enabled", False)):
        blockers.append("supervised_weak_valid_disabled")
    if str(getattr(settings, "paper_supervised_weak_valid_profile", "aggressive")).lower() != "aggressive":
        blockers.append("supervised_weak_valid_profile_unsupported")
    if str(getattr(settings, "execution_mode", "paper")).lower() != "paper" or bool(
        getattr(settings, "enable_real_trading", False)
    ):
        blockers.append("supervised_weak_valid_paper_only")
    if not bool(getattr(settings, "paper_scanner_exploration_enabled", False)):
        blockers.append("paper_scanner_exploration_inactive")
    if not paper_exploration_profile_enabled(settings):
        blockers.append("paper_exploration_profile_inactive")
    if str(getattr(settings, "paper_auto_operation_mode", "shadow")).lower() != "supervised":
        blockers.append("paper_auto_operation_mode_not_supervised")
    if bool(getattr(settings, "paper_auto_approve_proposals", False)):
        blockers.append("paper_auto_approve_must_be_disabled")
    if not bool(getattr(settings, "auto_propose_enabled", False)):
        blockers.append("auto_propose_disabled")
    if not _regular_market_hours_open(settings):
        blockers.append("regular_market_hours_closed")

    max_scan = int(getattr(settings, "paper_supervised_weak_valid_max_proposals_per_scan", 1) or 0)
    if max_scan <= 0 or weak_valid_scan_count >= max_scan:
        blockers.append("supervised_weak_valid_scan_limit_reached")
    max_day = int(getattr(settings, "paper_supervised_weak_valid_max_proposals_per_day", 4) or 0)
    if max_day <= 0 or weak_valid_daily_count + weak_valid_scan_count >= max_day:
        blockers.append("supervised_weak_valid_daily_limit_reached")

    action_value = getattr(getattr(signal, "action", None), "value", getattr(signal, "action", ""))
    if str(action_value).lower() != "buy":
        blockers.append("supervised_weak_valid_long_only")
    signal_metadata = dict(getattr(signal, "metadata", {}) or {})
    if str(signal_metadata.get("signal_role") or "entry_long").lower() == "entry_short":
        blockers.append("supervised_weak_valid_short_blocked")
    blockers.extend(_weak_valid_symbol_blockers(service, getattr(signal, "symbol", "")))

    entry = float(getattr(signal, "price", None) or getattr(context, "current_price", 0.0) or 0.0)
    stop = getattr(signal, "stop_loss", None)
    target = getattr(signal, "take_profit", None)
    if stop is None or target is None or entry <= 0:
        blockers.append("supervised_weak_valid_bracket_missing")
    else:
        try:
            if not (float(stop) < entry < float(target)):
                blockers.append("supervised_weak_valid_invalid_bracket")
        except (TypeError, ValueError):
            blockers.append("supervised_weak_valid_invalid_bracket")

    if not bool(market_data_status.get("verified", False)):
        blockers.append(str(market_data_status.get("verification_reason") or "market_data_unverified"))
    spread_bps = getattr(context, "spread_bps", None)
    if spread_bps is None:
        blockers.append("supervised_weak_valid_spread_unavailable")
    elif float(spread_bps) > float(getattr(settings, "screener_max_spread_bps", 50.0)):
        blockers.append("supervised_weak_valid_spread_too_wide")

    risk_reward = signal_metadata.get("risk_reward_ratio")
    if risk_reward is None:
        risk_reward = service._compute_risk_reward(signal)
    try:
        risk_reward_value = float(risk_reward) if risk_reward is not None else None
    except (TypeError, ValueError):
        risk_reward_value = None
    min_rr = float(getattr(settings, "paper_supervised_weak_valid_min_reward_to_risk", 1.0) or 1.0)
    if risk_reward_value is None or risk_reward_value < min_rr:
        blockers.append("supervised_weak_valid_reward_to_risk_too_low")
    min_rvol = float(getattr(settings, "paper_supervised_weak_valid_min_relative_volume", 0.30) or 0.30)
    if float(getattr(context, "relative_volume", 0.0) or 0.0) < min_rvol:
        blockers.append("supervised_weak_valid_relative_volume_too_low")
    min_score = float(getattr(settings, "paper_supervised_weak_valid_min_score", 45.0) or 45.0)
    score = float(ranking.get("final_score") or 0.0)
    if score < min_score:
        blockers.append("supervised_weak_valid_score_too_low")

    allowed = _weak_valid_allowed_reasons(settings)
    effective_reasons = _effective_weak_valid_reasons(signal, reasons)
    normalized_reasons = {str(reason).strip().lower() for reason in effective_reasons if str(reason).strip()}
    score_reasons = {"final_score_below_auto_threshold", "final_score_below_keep_threshold"}
    unsupported_reasons = sorted(normalized_reasons - allowed - score_reasons)
    if unsupported_reasons:
        blockers.append("supervised_weak_valid_unsupported_reasons:" + ",".join(unsupported_reasons))
    return list(dict.fromkeys(blockers))


def _maybe_promote_supervised_weak_valid(
    service: Any,
    *,
    signal: Any,
    quote: Any,
    timeframe: str,
    context: Any,
    intelligence: Any,
    market_data_status: dict[str, Any],
    filter_outcome: FilterOutcome,
    backtest_snapshot: dict[str, Any],
    ranking: dict[str, Any],
    freshness: str,
    reasons: list[str],
    weak_valid_scan_count: int,
    weak_valid_daily_count: int,
) -> Any | None:
    blockers = _paper_supervised_weak_valid_blockers(
        service,
        signal=signal,
        context=context,
        market_data_status=market_data_status,
        filter_outcome=filter_outcome,
        ranking=ranking,
        reasons=reasons,
        weak_valid_scan_count=weak_valid_scan_count,
        weak_valid_daily_count=weak_valid_daily_count,
    )
    signal_metadata = dict(getattr(signal, "metadata", {}) or {})
    risk_reward = signal_metadata.get("risk_reward_ratio")
    if risk_reward is None:
        risk_reward = service._compute_risk_reward(signal)
    try:
        risk_reward_value = float(risk_reward) if risk_reward is not None else None
    except (TypeError, ValueError):
        risk_reward_value = None
    effective_reasons = _effective_weak_valid_reasons(signal, reasons)
    attempt_payload = {
        "symbol": getattr(signal, "symbol", None),
        "strategy_name": getattr(signal, "strategy_name", None),
        "timeframe": timeframe,
        "promoted_to_candidate": not blockers,
        "promotion_blockers": list(blockers),
        "reasons": list(dict.fromkeys(effective_reasons)),
        "raw_reasons": list(dict.fromkeys(reasons)),
        "final_score": float(ranking.get("final_score") or 0.0),
        "min_score": float(getattr(service.settings, "paper_supervised_weak_valid_min_score", 45.0) or 45.0),
        "relative_volume": float(getattr(context, "relative_volume", 0.0) or 0.0),
        "min_relative_volume": float(
            getattr(service.settings, "paper_supervised_weak_valid_min_relative_volume", 0.30) or 0.30
        ),
        "reward_to_risk": risk_reward_value,
        "min_reward_to_risk": float(
            getattr(service.settings, "paper_supervised_weak_valid_min_reward_to_risk", 1.0) or 1.0
        ),
        "scan_count": weak_valid_scan_count,
        "daily_count": weak_valid_daily_count,
    }
    logger = getattr(service, "logs", None)
    if logger is not None:
        with suppress(Exception):
            logger.log("paper_supervised_weak_valid_promotion_attempt", attempt_payload)
    if blockers:
        return None
    snapshot = service._snapshot_from_signal(
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
    original_metadata = dict(snapshot.metadata or {})
    metadata = {
        **original_metadata,
        "alert_eligible": True,
        "execution_ready": True,
        "execution_blockers": [],
        "supervised_approval_required": True,
        "supervised_weak_valid_original_execution_blockers": list(original_metadata.get("execution_blockers") or []),
        "supervised_weak_valid_original_actionability": ranking.get("actionability"),
        "supervised_weak_valid_reasons": list(dict.fromkeys(effective_reasons)),
        "supervised_weak_valid_raw_reasons": list(dict.fromkeys(reasons)),
        "supervised_weak_valid_watchlist_only": bool(getattr(filter_outcome, "watchlist_only", False)),
        "supervised_weak_valid_min_score": float(
            getattr(service.settings, "paper_supervised_weak_valid_min_score", 45.0) or 45.0
        ),
        "supervised_weak_valid_min_relative_volume": float(
            getattr(service.settings, "paper_supervised_weak_valid_min_relative_volume", 0.30) or 0.30
        ),
        "supervised_weak_valid_min_reward_to_risk": float(
            getattr(service.settings, "paper_supervised_weak_valid_min_reward_to_risk", 1.0) or 1.0
        ),
        "supervised_weak_valid_promoted_to_candidate": True,
        "supervised_weak_valid_promotion_blockers": [],
        "production_qualified": False,
        "signal_classification": "supervised_weak_valid",
        "source": "supervised_weak_valid",
    }
    return snapshot.model_copy(
        update={
            "execution_ready": True,
            "tradable": True,
            "direction_label": "buy",
            "metadata": metadata,
            "reject_reasons": list(dict.fromkeys(reasons)),
        }
    )

