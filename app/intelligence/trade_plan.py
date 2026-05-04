"""Trade-plan helpers for premium signal output."""

from __future__ import annotations

from typing import Any


def build_trade_plan(
    *,
    signal: Any,
    timeframe: str,
    current_price: float,
    entry_price: float | None,
    stop_loss: float | None,
    take_profit: float | None,
    risk_reward_ratio: float | None,
    final_score: float,
    context: Any,
    intelligence: Any,
    actionability: str,
) -> dict[str, Any]:
    """Create a structured trade plan from setup structure and market context."""

    style = str(signal.metadata.get("style") or signal.strategy_name)
    role = str(signal.metadata.get("signal_role") or "entry_long")
    is_short = role == "entry_short"
    reference_entry = float(entry_price or current_price or signal.price or 0.0)
    reference_stop = float(stop_loss) if stop_loss is not None else None
    if reference_entry <= 0 or reference_stop is None:
        return {
            "verdict": "no_trade",
            "timing_label": "no_trade",
            "preferred_entry_method": "none",
            "entry_zone_low": None,
            "entry_zone_high": None,
            "confirmation_trigger": "Data quality or setup structure is insufficient.",
            "target_1": None,
            "target_2": None,
            "stretch_target": None,
            "trailing_logic": "No active trade plan.",
            "invalidation_condition": "No edge until a new setup forms.",
            "hold_style": "watch",
            "position_quality_label": "none",
            "summary": "No trade plan because entry or stop data is missing.",
        }

    unit_risk = max(abs(reference_entry - reference_stop), reference_entry * 0.005, 0.01)
    if is_short:
        target_1 = reference_entry - unit_risk
        target_2 = reference_entry - (unit_risk * 1.8)
        stretch_target = reference_entry - (unit_risk * 2.6)
    else:
        target_1 = reference_entry + unit_risk
        target_2 = reference_entry + (unit_risk * 1.8)
        stretch_target = reference_entry + (unit_risk * 2.6)

    if take_profit is not None:
        target_2 = float(take_profit)
        extension = abs(float(take_profit) - reference_entry) * 0.6
        stretch_target = float(take_profit) - extension if is_short else float(take_profit) + extension

    if "momentum" in style:
        preferred_entry_method = "breakout_confirmation"
        zone_padding = unit_risk * 0.12
    elif "mean_reversion" in style:
        preferred_entry_method = "reclaim_after_flush" if not is_short else "fade_after_pop"
        zone_padding = unit_risk * 0.18
    else:
        preferred_entry_method = "pullback_retest"
        zone_padding = unit_risk * 0.15

    entry_zone_low = reference_entry - zone_padding if not is_short else reference_entry - zone_padding
    entry_zone_high = reference_entry + zone_padding if not is_short else reference_entry + zone_padding
    confirmation_trigger = _confirmation_trigger(
        preferred_entry_method=preferred_entry_method,
        reference_entry=reference_entry,
        context=context,
        intelligence=intelligence,
        is_short=is_short,
    )
    trailing_logic = (
        "After target 1, trail below the fast structure low / short-term EMA."
        if not is_short
        else "After target 1, trail above the fast structure high / short-term EMA."
    )
    invalidation_condition = (
        f"Reject if price closes above {reference_stop:.2f}."
        if is_short
        else f"Reject if price closes below {reference_stop:.2f}."
    )

    hold_style = "swing"
    if timeframe in {"5m", "10m", "15m"}:
        hold_style = "intraday"
    elif timeframe == "1h":
        hold_style = "intraday" if "intraday" in style else "swing"

    if final_score >= 85:
        verdict = "actionable"
        timing_label = "immediate" if intelligence.momentum_state != "exhausted" else "conditional"
        position_quality_label = "conservative"
    elif final_score >= 70 and actionability == "alert":
        verdict = "actionable"
        timing_label = "conditional"
        position_quality_label = "balanced"
    elif final_score >= 55 or actionability == "watchlist":
        verdict = "watchlist"
        timing_label = "watchlist"
        position_quality_label = "aggressive"
    else:
        verdict = "no_trade"
        timing_label = "no_trade"
        position_quality_label = "none"

    if intelligence.momentum_state == "exhausted" and verdict == "actionable":
        timing_label = "conditional"
    if intelligence.market_regime_score < 0.45 and verdict != "no_trade":
        timing_label = "watchlist"
        verdict = "watchlist"

    summary = (
        f"{timing_label.replace('_', ' ')} {preferred_entry_method.replace('_', ' ')} | "
        f"{hold_style} | {position_quality_label}"
    )
    return {
        "verdict": verdict,
        "timing_label": timing_label,
        "preferred_entry_method": preferred_entry_method,
        "entry_zone_low": round(entry_zone_low, 2),
        "entry_zone_high": round(entry_zone_high, 2),
        "confirmation_trigger": confirmation_trigger,
        "target_1": round(target_1, 2),
        "target_2": round(target_2, 2),
        "stretch_target": round(stretch_target, 2),
        "trailing_logic": trailing_logic,
        "invalidation_condition": invalidation_condition,
        "hold_style": hold_style,
        "position_quality_label": position_quality_label,
        "summary": summary,
        "estimated_reward_to_risk": risk_reward_ratio,
    }


def _confirmation_trigger(
    *,
    preferred_entry_method: str,
    reference_entry: float,
    context: Any,
    intelligence: Any,
    is_short: bool,
) -> str:
    side = "below" if is_short else "above"
    if preferred_entry_method == "breakout_confirmation":
        return (
            f"Accept only if price holds {side} {reference_entry:.2f} with relative volume "
            f"near {float(getattr(context, 'relative_volume', 0.0) or 0.0):.2f}x and alignment "
            f"{float(getattr(intelligence, 'timeframe_alignment_score', 0.0) or 0.0):.2f}+."
        )
    if preferred_entry_method == "pullback_retest":
        return (
            f"Wait for a controlled retest into the entry zone and a reclaim {side} "
            f"{reference_entry:.2f} before acting."
        )
    return (
        f"Require reversal confirmation near the zone with momentum state "
        f"{getattr(intelligence, 'momentum_state', 'mixed')} before entering."
    )
