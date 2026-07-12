"""Paper-only weak-signal helpers for supervised proposal generation."""

from __future__ import annotations

from math import isfinite
from typing import Any

from app.models.signal import Signal, SignalAction
from app.strategies.base import BaseStrategy


def configure_weak_signal_emission(strategy: Any, settings: Any) -> Any:
    """Attach paper-only weak-signal policy to a strategy instance."""

    strategy_name = str(getattr(strategy, "name", "") or "").lower()
    allowed = {
        str(item).strip().lower()
        for item in (getattr(settings, "paper_strategy_weak_signal_allowed_strategies", []) or [])
        if str(item).strip()
    }
    enabled = (
        bool(getattr(settings, "paper_strategy_weak_signal_emission_enabled", False))
        and str(getattr(settings, "execution_mode", "paper")).lower() == "paper"
        and not bool(getattr(settings, "enable_real_trading", False))
        and bool(getattr(settings, "paper_supervised_weak_valid_enabled", False))
        and strategy_name in allowed
    )
    strategy._paper_weak_signal_enabled = enabled
    strategy._paper_weak_signal_min_reward_to_risk = float(
        getattr(settings, "paper_supervised_weak_valid_min_reward_to_risk", 1.0) or 1.0
    )
    return strategy


def build_supervised_weak_long_signal(
    strategy: BaseStrategy,
    *,
    symbol: str,
    price: Any,
    stop: Any,
    risk_multiple: Any,
    rationale: str,
    confidence: float,
    metadata: dict[str, Any],
    rejection_reasons: list[str],
    setup_anchor: bool,
) -> Signal | None:
    """Return a real long signal for supervised paper review, never a fake setup."""

    if not bool(getattr(strategy, "_paper_weak_signal_enabled", False)):
        return None
    if not setup_anchor:
        return None
    entry = _finite_float(price)
    stop_value = _finite_float(stop)
    rr = _finite_float(risk_multiple)
    if entry is None or stop_value is None or rr is None:
        return None
    minimum_rr = float(getattr(strategy, "_paper_weak_signal_min_reward_to_risk", 1.0) or 1.0)
    if rr < minimum_rr or not (stop_value < entry):
        return None
    risk = entry - stop_value
    if risk <= max(entry * 0.0005, 0.01):
        return None
    target = entry + (risk * rr)
    if not (stop_value < entry < target):
        return None
    payload = {
        **metadata,
        "signal_role": "entry_long",
        "signal_classification": "supervised_weak_valid",
        "source": "supervised_weak_valid",
        "supervised_approval_required": True,
        "production_qualified": False,
        "weak_signal_reasons": list(dict.fromkeys(rejection_reasons)),
        "weak_signal_setup_anchor": True,
        "risk_reward_ratio": round(rr, 4),
    }
    return strategy._build_signal(
        symbol=symbol.upper(),
        strategy_name=strategy.name,
        action=SignalAction.BUY,
        rationale=rationale,
        confidence=max(0.45, min(float(confidence), 1.0)),
        price=round(entry, 4),
        stop_loss=round(stop_value, 4),
        take_profit=round(target, 4),
        metadata=payload,
    )


def _finite_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) and result > 0 else None
