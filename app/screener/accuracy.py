"""Accuracy scoring for live screener signals.

This layer is intentionally separate from raw strategy generation. Strategies
can identify a setup; the accuracy profile decides whether the proposed entry
location and confirmation quality are good enough to trust.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from app.indicators import compute_confluence_score, enrich_technical_indicators


@dataclass(slots=True)
class AccuracyProfile:
    """Explainable quality profile for a proposed live trade setup."""

    overall_score: float
    entry_location_score: float
    support_resistance_score: float
    confirmation_score: float
    false_positive_risk_score: float
    pass_reasons: list[str] = field(default_factory=list)
    rejection_reasons: list[str] = field(default_factory=list)
    measurements: dict[str, float | str | None] = field(default_factory=dict)


def build_accuracy_profile(
    history: pd.DataFrame,
    *,
    signal: Any,
    context: Any,
    settings: Any,
) -> AccuracyProfile:
    """Score entry quality, confirmation quality, and false-positive risk."""

    timeframe = str(signal.metadata.get("timeframe") or "1d").lower()
    frame = enrich_technical_indicators(history, timeframe=timeframe)
    last = frame.iloc[-1]
    metadata = getattr(signal, "metadata", {}) or {}
    is_short = str(metadata.get("signal_role") or "entry_long") == "entry_short"
    price = _safe_float(getattr(signal, "price", None)) or _safe_float(getattr(context, "current_price", None))
    price = price or _safe_float(last.get("close")) or 0.0

    atr = _safe_float(last.get("atr_14"))
    if atr <= 0:
        atr = max(price * (_safe_float(getattr(context, "atr_pct", 0.0)) / 100.0), price * 0.004, 0.01)

    values = _latest_values(last, price=price)
    range_high = values["range_high_20"]
    range_low = values["range_low_20"]
    swing_high = values["swing_high_10"]
    swing_low = values["swing_low_10"]
    ema_9 = values["ema_9"]
    ema_20 = values["ema_20"]
    ema_50 = values["ema_50"]
    vwap = values["vwap"]
    rsi = values["rsi_14"]
    adx = values["adx_14"]
    macd_hist = values["macd_hist"]
    relative_volume = _safe_float(getattr(context, "relative_volume", None)) or values["relative_volume"]
    confluence = _safe_float(metadata.get("indicator_confluence_score"))
    if confluence <= 0:
        confluence = compute_confluence_score(last, is_short=is_short)

    max_late_entry_atr = max(float(settings.screener_max_late_entry_atr_multiple), 0.5)
    min_barrier_distance_atr = max(float(settings.screener_min_resistance_atr_distance), 0.05)
    minimum_rvol = max(float(settings.screener_min_relative_volume), 0.1)
    if timeframe == "1m":
        minimum_rvol = max(minimum_rvol, float(settings.screener_scalp_min_relative_volume))
        max_late_entry_atr *= 0.75

    if is_short:
        anchor_distance = _nearest_distance_atr(price, [swing_high, range_high, ema_20, vwap], atr, above=True)
        barrier_distance = _nearest_distance_atr(price, [swing_low, range_low], atr, above=False)
        breakout_confirmed = price <= min(swing_low, range_low) + (atr * min_barrier_distance_atr)
        aligned_vwap = price < vwap
        ema_stack = ema_9 < ema_20 < ema_50
        rsi_constructive = 30.0 <= rsi <= 52.0
        exhausted = rsi < 24.0
        macd_confirmed = macd_hist < 0
    else:
        anchor_distance = _nearest_distance_atr(price, [swing_low, range_low, ema_20, vwap], atr, above=False)
        barrier_distance = _nearest_distance_atr(price, [swing_high, range_high], atr, above=True)
        breakout_confirmed = price >= max(swing_high, range_high) - (atr * min_barrier_distance_atr)
        aligned_vwap = price > vwap
        ema_stack = ema_9 > ema_20 > ema_50
        rsi_constructive = 48.0 <= rsi <= 72.0
        exhausted = rsi > 78.0
        macd_confirmed = macd_hist > 0

    anchor_score = _anchor_distance_score(anchor_distance)
    barrier_score = _barrier_distance_score(
        barrier_distance,
        minimum_distance=min_barrier_distance_atr,
        breakout_confirmed=breakout_confirmed,
    )
    extension_atr = _nearest_extension_atr(price, [ema_9, ema_20, vwap], atr)
    extension_score = _late_entry_score(extension_atr, max_late_entry_atr)
    entry_location_score = _clamp01((anchor_score * 0.35) + (barrier_score * 0.35) + (extension_score * 0.30))
    support_resistance_score = _clamp01((anchor_score * 0.45) + (barrier_score * 0.55))

    confirmation_checks = [
        1.0 if aligned_vwap else 0.0,
        1.0 if ema_stack else 0.0,
        1.0 if rsi_constructive else 0.35 if not exhausted else 0.0,
        _normalize(relative_volume, minimum_rvol, minimum_rvol * 2.0),
        _normalize(adx, 16.0, 30.0),
        1.0 if macd_confirmed else 0.2,
        _clamp01(confluence),
    ]
    confirmation_score = _clamp01(sum(confirmation_checks) / len(confirmation_checks))

    efficiency_ratio = _safe_float(getattr(context, "efficiency_ratio", None))
    choppy_risk = 1.0 - _normalize(efficiency_ratio, 0.18, 0.48)
    low_volume_risk = 1.0 - _normalize(relative_volume, minimum_rvol, minimum_rvol * 1.8)
    late_entry_risk = 1.0 - extension_score
    exhaustion_risk = 1.0 if exhausted else 0.0
    barrier_risk = (
        max(0.0, 1.0 - (barrier_distance / min_barrier_distance_atr))
        if barrier_distance < min_barrier_distance_atr and not breakout_confirmed
        else 0.0
    )
    weak_confirmation_risk = 1.0 - confirmation_score
    weak_trend_risk = 1.0 - _normalize(adx, 14.0, 28.0)
    false_positive_risk_score = _clamp01(
        (choppy_risk * 0.18)
        + (low_volume_risk * 0.16)
        + (late_entry_risk * 0.18)
        + (exhaustion_risk * 0.14)
        + (barrier_risk * 0.16)
        + (weak_confirmation_risk * 0.12)
        + (weak_trend_risk * 0.06)
    )

    overall_score = _clamp01(
        (entry_location_score * 0.30)
        + (support_resistance_score * 0.20)
        + (confirmation_score * 0.35)
        + ((1.0 - false_positive_risk_score) * 0.15)
    )

    pass_reasons: list[str] = []
    rejection_reasons: list[str] = []
    _append_reason(entry_location_score >= 0.52, pass_reasons, rejection_reasons, "entry_location_clean", "entry_location_weak")
    _append_reason(
        support_resistance_score >= 0.50,
        pass_reasons,
        rejection_reasons,
        "support_resistance_room_ok",
        "support_resistance_room_weak",
    )
    _append_reason(
        confirmation_score >= 0.45,
        pass_reasons,
        rejection_reasons,
        "technical_confirmation_ok",
        "technical_confirmation_weak",
    )
    _append_reason(
        false_positive_risk_score <= 0.68,
        pass_reasons,
        rejection_reasons,
        "false_positive_risk_contained",
        "false_positive_risk_elevated",
    )

    measurements = {
        "accuracy_score": round(overall_score, 4),
        "entry_location_score": round(entry_location_score, 4),
        "support_resistance_score": round(support_resistance_score, 4),
        "confirmation_score": round(confirmation_score, 4),
        "false_positive_risk_score": round(false_positive_risk_score, 4),
        "nearest_anchor_distance_atr": round(anchor_distance, 4),
        "nearest_barrier_distance_atr": round(barrier_distance, 4),
        "entry_extension_atr": round(extension_atr, 4),
        "breakout_confirmed": "yes" if breakout_confirmed else "no",
    }
    return AccuracyProfile(
        overall_score=round(overall_score, 4),
        entry_location_score=round(entry_location_score, 4),
        support_resistance_score=round(support_resistance_score, 4),
        confirmation_score=round(confirmation_score, 4),
        false_positive_risk_score=round(false_positive_risk_score, 4),
        pass_reasons=pass_reasons,
        rejection_reasons=rejection_reasons,
        measurements=measurements,
    )


def _latest_values(last: pd.Series, *, price: float) -> dict[str, float]:
    return {
        "range_high_20": _safe_float(last.get("range_high_20")) or _safe_float(last.get("high")) or price,
        "range_low_20": _safe_float(last.get("range_low_20")) or _safe_float(last.get("low")) or price,
        "swing_high_10": _safe_float(last.get("swing_high_10")) or _safe_float(last.get("high")) or price,
        "swing_low_10": _safe_float(last.get("swing_low_10")) or _safe_float(last.get("low")) or price,
        "ema_9": _safe_float(last.get("ema_9")) or price,
        "ema_20": _safe_float(last.get("ema_20")) or price,
        "ema_50": _safe_float(last.get("ema_50")) or price,
        "vwap": _safe_float(last.get("vwap")) or price,
        "rsi_14": _safe_float(last.get("rsi_14")) or 50.0,
        "adx_14": _safe_float(last.get("adx_14")) or 16.0,
        "macd_hist": _safe_float(last.get("macd_hist")),
        "relative_volume": _safe_float(last.get("relative_volume")) or 1.0,
    }


def _nearest_distance_atr(price: float, levels: list[float], atr: float, *, above: bool) -> float:
    valid_levels = [level for level in levels if level > 0 and ((level >= price) if above else (level <= price))]
    if not valid_levels:
        return 99.0
    if above:
        distance = min(level - price for level in valid_levels)
    else:
        distance = min(price - level for level in valid_levels)
    return max(float(distance) / max(atr, 0.01), 0.0)


def _nearest_extension_atr(price: float, levels: list[float], atr: float) -> float:
    valid_levels = [level for level in levels if level > 0]
    if not valid_levels:
        return 0.0
    return min(abs(price - level) for level in valid_levels) / max(atr, 0.01)


def _anchor_distance_score(distance_atr: float) -> float:
    if distance_atr <= 0.15:
        return 0.85
    if distance_atr <= 1.4:
        return 1.0
    if distance_atr <= 2.8:
        return max(0.35, 1.0 - ((distance_atr - 1.4) / 2.0))
    if distance_atr >= 50.0:
        return 0.45
    return 0.25


def _barrier_distance_score(distance_atr: float, *, minimum_distance: float, breakout_confirmed: bool) -> float:
    if distance_atr >= 50.0:
        return 0.8
    if breakout_confirmed:
        return 0.85
    if distance_atr < minimum_distance:
        return max(0.0, distance_atr / max(minimum_distance, 0.01)) * 0.45
    if distance_atr <= 2.8:
        return 1.0
    if distance_atr <= 5.0:
        return 0.8
    return 0.65


def _late_entry_score(extension_atr: float, max_late_entry_atr: float) -> float:
    if extension_atr <= 0.45:
        return 1.0
    if extension_atr >= max_late_entry_atr:
        return 0.0
    return _clamp01(1.0 - ((extension_atr - 0.45) / max(max_late_entry_atr - 0.45, 0.01)))


def _append_reason(
    condition: bool,
    pass_reasons: list[str],
    rejection_reasons: list[str],
    pass_code: str,
    fail_code: str,
) -> None:
    if condition:
        pass_reasons.append(pass_code)
    else:
        rejection_reasons.append(fail_code)


def _normalize(value: Any, low: float, high: float) -> float:
    numeric = _safe_float(value)
    high = max(float(high), float(low) + 0.0001)
    return _clamp01((numeric - float(low)) / (high - float(low)))


def _clamp01(value: Any) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    if pd.isna(numeric):
        return 0.0
    return max(0.0, min(1.0, numeric))


def _safe_float(value: Any) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    if pd.isna(numeric):
        return 0.0
    return numeric
