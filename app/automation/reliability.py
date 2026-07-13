"""Reliability helpers for proposal flow and paper-auto gates."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from typing import Any

STRICT_VALID = "strict_valid"
SUPERVISED_WEAK_VALID = "supervised_weak_valid"
PAPER_NEAR_MISS = "paper_near_miss"
NOT_TRADEABLE = "not_tradeable"

AUTO_TIER_PENDING_ONLY = "tier0_pending_only"
AUTO_TIER_SUPERVISED_ONLY = "tier1_supervised_only"
AUTO_TIER_STRICT_VALID = "tier2_strict_valid"
AUTO_TIER_STRATEGY_QUALIFIED = "tier3_strategy_qualified"


def proposal_quality_label(
    candidate: Any | None = None,
    *,
    metadata: dict[str, Any] | None = None,
    execution_ready: bool | None = None,
    alert_eligible: bool | None = None,
    signal_role: str | None = None,
    stop_loss: Any = None,
    take_profit: Any = None,
) -> str:
    """Classify proposal quality without making an ineligible trade eligible."""

    payload = dict(metadata or getattr(candidate, "metadata", {}) or {})
    ready = bool(getattr(candidate, "execution_ready", False)) if execution_ready is None else bool(execution_ready)
    eligible = bool(payload.get("alert_eligible", False)) if alert_eligible is None else bool(alert_eligible)
    role = str(signal_role if signal_role is not None else getattr(candidate, "signal_role", "") or "").lower()
    stop = stop_loss if stop_loss is not None else getattr(candidate, "stop_loss", None)
    target = take_profit if take_profit is not None else getattr(candidate, "take_profit", None)
    classification = str(payload.get("signal_classification") or payload.get("source") or "").lower()
    if not ready or not eligible or role == "entry_short" or stop is None or target is None:
        return NOT_TRADEABLE
    if classification == SUPERVISED_WEAK_VALID:
        return SUPERVISED_WEAK_VALID
    if classification == PAPER_NEAR_MISS:
        return PAPER_NEAR_MISS
    return STRICT_VALID


def lifecycle_complete(record: Any, *, require_autonomous: bool = True) -> bool:
    """Return whether a paper lifecycle has complete evidence for reliability gates."""

    flags = getattr(record, "flags", None)
    if require_autonomous and not bool(getattr(record, "autonomous", False)):
        return False
    if flags is None:
        return False
    return all(
        bool(getattr(flags, name, False))
        for name in (
            "entry_submitted",
            "entry_filled",
            "bracket_legs_verified",
            "exit_filled_or_position_flat",
            "reconciled",
            "review_created",
            "duplicate_order_absent",
        )
    )


def lifecycle_safety_blockers(lifecycles: list[Any]) -> list[str]:
    """Summarize lifecycle evidence that must block auto-approval."""

    blockers: list[str] = []
    for lifecycle in lifecycles:
        flags = getattr(lifecycle, "flags", None)
        if flags is None:
            blockers.append("lifecycle_flags_missing")
            continue
        if not bool(getattr(flags, "duplicate_order_absent", True)):
            blockers.append("duplicate_broker_orders_present")
        if bool(getattr(flags, "entry_filled", False)) and not bool(getattr(flags, "bracket_legs_verified", False)):
            blockers.append("missing_bracket_protection")
        if bool(getattr(flags, "entry_filled", False)) and not bool(getattr(flags, "reconciled", False)):
            blockers.append("unreconciled_lifecycles_present")
        if bool(getattr(flags, "exit_filled_or_position_flat", False)) and getattr(lifecycle, "realized_pnl_usd", None) is None:
            blockers.append("unresolved_lifecycle_pnl")
        for blocker in list(getattr(lifecycle, "blockers", []) or []):
            if str(blocker).startswith("unknown_position"):
                blockers.append("unknown_broker_position")
    return sorted(set(blockers))


def lifecycle_stats(lifecycles: list[Any]) -> dict[str, Any]:
    """Return compact lifecycle completeness statistics."""

    total = len(lifecycles)
    autonomous = [item for item in lifecycles if bool(getattr(item, "autonomous", False))]
    complete = [item for item in lifecycles if lifecycle_complete(item)]
    incomplete = [item for item in lifecycles if not lifecycle_complete(item, require_autonomous=False)]
    by_source = Counter(str(getattr(item, "source", "unknown") or "unknown") for item in lifecycles)
    return {
        "total": total,
        "autonomous": len(autonomous),
        "complete": len(complete),
        "incomplete": len(incomplete),
        "by_source": dict(by_source),
        "safety_blockers": lifecycle_safety_blockers(lifecycles),
    }


def auto_approval_tier_blockers(
    *,
    settings: Any,
    candidate: Any,
    lifecycles: list[Any] | None,
) -> list[str]:
    """Policy blockers for paper auto-approval tiers."""

    tier = str(getattr(settings, "paper_auto_approval_tier", AUTO_TIER_SUPERVISED_ONLY) or "").lower()
    quality = proposal_quality_label(candidate)
    blockers: list[str] = []
    if tier == AUTO_TIER_PENDING_ONLY:
        blockers.append("paper_auto_tier_pending_only")
    if tier == AUTO_TIER_SUPERVISED_ONLY:
        blockers.append("paper_auto_tier_supervised_only")
    if quality == SUPERVISED_WEAK_VALID:
        blockers.append("weak_valid_requires_human_approval")
    if quality == PAPER_NEAR_MISS:
        blockers.append("near_miss_requires_human_approval")
    if tier in {AUTO_TIER_STRICT_VALID, AUTO_TIER_STRATEGY_QUALIFIED} and quality != STRICT_VALID:
        blockers.append("paper_auto_requires_strict_valid_quality")
    if tier == AUTO_TIER_STRATEGY_QUALIFIED:
        blockers.extend(_strategy_evidence_blockers(settings=settings, candidate=candidate, lifecycles=lifecycles or []))
    minimum = max(int(getattr(settings, "paper_auto_min_clean_supervised_lifecycles", 10) or 0), 0)
    if minimum and lifecycles is None:
        blockers.append("paper_lifecycle_evidence_unavailable")
    if minimum and lifecycles is not None:
        clean_count = sum(1 for item in lifecycles if lifecycle_complete(item))
        if clean_count < minimum:
            blockers.append("insufficient_clean_supervised_lifecycles")
    blockers.extend(lifecycle_safety_blockers(lifecycles or []))
    return sorted(set(blockers))


def daily_items(items: list[Any], *, now: datetime | None = None) -> list[Any]:
    """Filter repository models to records created during the current UTC date."""

    today = (now or datetime.now(tz=UTC)).date().isoformat()
    return [item for item in items if str(getattr(item, "created_at", "") or "").startswith(today)]


def _strategy_evidence_blockers(*, settings: Any, candidate: Any, lifecycles: list[Any]) -> list[str]:
    strategy = str(getattr(candidate, "strategy_name", "") or "")
    closed = [
        item
        for item in lifecycles
        if str(getattr(item, "strategy_name", "") or "") == strategy and lifecycle_complete(item)
    ]
    minimum = max(int(getattr(settings, "paper_auto_min_strategy_closed_trades", 30) or 0), 0)
    blockers: list[str] = []
    if len(closed) < minimum:
        blockers.append("insufficient_strategy_closed_trade_evidence")
    pnls = [float(getattr(item, "realized_pnl_usd", 0.0) or 0.0) for item in closed]
    if pnls:
        expectancy = sum(pnls) / len(pnls)
        gains = sum(value for value in pnls if value > 0)
        losses = abs(sum(value for value in pnls if value < 0))
        profit_factor = gains if losses == 0 else gains / losses
        if expectancy <= 0:
            blockers.append("strategy_expectancy_not_positive")
        if profit_factor < float(getattr(settings, "paper_auto_min_strategy_profit_factor", 1.20) or 1.20):
            blockers.append("strategy_profit_factor_below_auto_gate")
    return blockers
