"""Enhanced long-only research strategies for Alpaca Paper exploration.

These strategies are deliberately constrained: they emit BUY signals only,
attach validated stop/target plans, and carry metadata that keeps them in the
paper-research lane until governance promotes them.
"""

from __future__ import annotations

from math import isfinite
from typing import Any

import pandas as pd

from app.indicators import compute_confluence_score, enrich_technical_indicators, indicator_summary
from app.models.signal import Signal, SignalAction
from app.strategies.base import BaseStrategy
from app.strategies.weak_signals import build_supervised_weak_long_signal


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if not isfinite(result) or pd.isna(result):
        return default
    return result


def _round_price(value: float) -> float:
    return round(max(value, 0.01), 4)


def _recent_low(frame: pd.DataFrame, window: int) -> float | None:
    if frame.empty:
        return None
    return _safe_float(frame["low"].tail(max(window, 1)).min())


def _recent_high(frame: pd.DataFrame, window: int) -> float | None:
    if frame.empty:
        return None
    return _safe_float(frame["high"].tail(max(window, 1)).max())


def _liquidity_ok(row: pd.Series, minimum_dollar_volume: float) -> bool:
    dollar_volume = _safe_float(row.get("avg_dollar_volume_20"), 0.0) or 0.0
    return dollar_volume >= minimum_dollar_volume


def _diagnostic_measurements(row: pd.Series | None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    keys = [
        "close",
        "relative_volume",
        "avg_dollar_volume_20",
        "atr_14",
        "atr_pct",
        "adx_14",
        "rsi_14",
        "bb_width_pct",
        "ema_20",
        "ema_50",
        "ema_200",
        "vwap",
    ]
    measurements = {
        key: _safe_float(row.get(key)) if row is not None and key in row else None
        for key in keys
    }
    if extra:
        measurements.update(extra)
    return measurements


def _set_diagnostics(
    strategy: BaseStrategy,
    *,
    status: str,
    rejection_reasons: list[str],
    row: pd.Series | None = None,
    score: float | None = None,
    measurements: dict[str, Any] | None = None,
) -> None:
    strategy.last_diagnostics = {
        "status": status,
        "rejection_reasons": list(dict.fromkeys(rejection_reasons)),
        "reason_codes": list(dict.fromkeys(rejection_reasons)),
        "score": score,
        "measurements": _diagnostic_measurements(row, measurements),
    }


def _reject(
    strategy: BaseStrategy,
    *,
    rejection_reasons: list[str],
    row: pd.Series | None = None,
    score: float | None = 45.0,
    measurements: dict[str, Any] | None = None,
) -> None:
    _set_diagnostics(
        strategy,
        status="no_signal",
        rejection_reasons=rejection_reasons or ["no_strategy_signal"],
        row=row,
        score=score,
        measurements=measurements,
    )


def _condition_rejections(checks: dict[str, bool]) -> list[str]:
    return [name for name, passed in checks.items() if not passed]


def _metadata(
    *,
    row: pd.Series,
    style: str,
    setup_type: str,
    risk_reward: float,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "pack": "enhanced_research",
        "asset_class": "us_equity",
        "paper_stage": "research",
        "live_enabled": False,
        "signal_role": "entry_long",
        "style": style,
        "setup_type": setup_type,
        "risk_reward_ratio": round(risk_reward, 4),
        "indicator_confluence_score": compute_confluence_score(row),
        "indicator_summary": indicator_summary(row),
    }
    if extra:
        payload.update(extra)
    return payload


def _long_signal(
    strategy: BaseStrategy,
    *,
    symbol: str,
    price: float,
    stop: float,
    risk_multiple: float,
    rationale: str,
    confidence: float,
    metadata: dict[str, Any],
) -> Signal | None:
    if stop >= price:
        _set_diagnostics(
            strategy,
            status="invalid_trade_plan",
            rejection_reasons=["invalid_stop_or_target_generation"],
            measurements={"price": price, "stop": stop, "risk_multiple": risk_multiple},
        )
        return None
    risk = price - stop
    if risk <= max(price * 0.0005, 0.01):
        _set_diagnostics(
            strategy,
            status="invalid_trade_plan",
            rejection_reasons=["risk_too_small_for_trade_plan"],
            measurements={"price": price, "stop": stop, "risk": risk, "risk_multiple": risk_multiple},
        )
        return None
    target = price + (risk * risk_multiple)
    return strategy._build_signal(
        symbol=symbol.upper(),
        strategy_name=strategy.name,
        action=SignalAction.BUY,
        rationale=rationale,
        confidence=max(0.0, min(confidence, 1.0)),
        price=_round_price(price),
        stop_loss=_round_price(stop),
        take_profit=_round_price(target),
        metadata=metadata,
    )


def _weak_long_signal(
    strategy: BaseStrategy,
    *,
    symbol: str,
    row: pd.Series,
    price: float,
    stop: float,
    risk_multiple: float,
    rationale: str,
    confidence: float,
    style: str,
    setup_type: str,
    rejection_reasons: list[str],
    setup_anchor: bool,
    extra: dict[str, Any] | None = None,
) -> Signal | None:
    return build_supervised_weak_long_signal(
        strategy,
        symbol=symbol,
        price=price,
        stop=stop,
        risk_multiple=risk_multiple,
        rationale=rationale,
        confidence=confidence,
        metadata=_metadata(
            row=row,
            style=style,
            setup_type=setup_type,
            risk_reward=risk_multiple,
            extra=extra,
        ),
        rejection_reasons=rejection_reasons,
        setup_anchor=setup_anchor,
    )


