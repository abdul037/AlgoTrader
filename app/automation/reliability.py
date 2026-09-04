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


def candidate_propose_drop_reason(candidate: Any) -> str | None:
    """Classify why a scan candidate is not eligible to become an auto-proposal.

    Pure, side-effect-free mirror of the pre-proposal ``continue`` checks in the
    auto-propose loop. Returns a stable funnel drop-reason label, or ``None`` when
    the candidate should proceed to proposal creation. This exists so the scan
    funnel can attribute silent drops without duplicating the loop's logic, and
    so the mapping is unit-testable. It does NOT gate anything on its own — the
    caller still enforces every check — it only names the reason for diagnostics.
    """

    if not bool(getattr(candidate, "execution_ready", False)):
        return "not_execution_ready"
    if not bool((getattr(candidate, "metadata", {}) or {}).get("alert_eligible", False)):
        return "not_alert_eligible"
    if str(getattr(candidate, "signal_role", "") or "").lower() == "entry_short":
        return "entry_short"
    if getattr(candidate, "stop_loss", None) is None:
        return "missing_stop"
    return None


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
    # PAPER-ONLY explicit opt-in: fully autonomous near-miss trading. When on,
    # the approval-tier POLICY and the clean-lifecycle bootstrap are bypassed for
    # near-miss candidates so they auto-execute unattended. The lifecycle-failure
    # circuit-breaker below still applies, and every HARD gate (spread,
    # reward:risk, bracket, liquidity, universe, asset support, blacklist,
    # regular hours, score) is enforced by the caller regardless.
    paper_unattended_near_miss = (
        quality == PAPER_NEAR_MISS
        and bool(getattr(settings, "paper_unattended_near_miss_auto_exec_enabled", False))
        and str(getattr(settings, "execution_mode", "paper") or "").lower() == "paper"
        and not bool(getattr(settings, "enable_real_trading", False))
        and str(getattr(settings, "paper_auto_operation_mode", "") or "").lower() == "unattended"
    )
    if not paper_unattended_near_miss:
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
    # The lifecycle-failure circuit-breaker is a hard safety stop; it always runs.
    blockers.extend(lifecycle_safety_blockers(lifecycles or []))
    return sorted(set(blockers))


def daily_items(items: list[Any], *, now: datetime | None = None) -> list[Any]:
    """Filter repository models to records created during the current UTC date."""

    today = (now or datetime.now(tz=UTC)).date().isoformat()
    return [item for item in items if str(getattr(item, "created_at", "") or "").startswith(today)]


def aggregate_scan_funnel(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Roll up per-scan ``auto_propose_funnel`` payloads into one day-level view.

    Each row is a run-log entry ``{"created_at": ..., "payload": {...}}`` (or a
    bare payload dict). The integer counters are summed across scans and grouped
    by prefix so the whole promotion->proposal->execution pipeline reads at a
    glance: headline totals (entered, proposals_created, executed), plus
    ``dropped`` / ``safety_blocked`` / ``exec_blocked`` breakdowns keyed by
    reason. Pure and defensive — non-integer or unexpected values are ignored,
    never raised on.
    """

    totals: Counter[str] = Counter()
    origins: Counter[str] = Counter()
    scans = 0
    for row in rows:
        payload = row.get("payload") if isinstance(row, dict) and "payload" in row else row
        if not isinstance(payload, dict):
            continue
        scans += 1
        origin = str(payload.get("origin") or "")
        if origin:
            origins[origin] += 1
        for key, value in payload.items():
            if key == "origin" or isinstance(value, bool) or not isinstance(value, int):
                continue
            totals[key] += value

    dropped = {k.split(":", 1)[1]: v for k, v in totals.items() if k.startswith("dropped:")}
    safety = {k.split(":", 1)[1]: v for k, v in totals.items() if k.startswith("safety_blocked:")}
    exec_blocked = {k.split(":", 1)[1]: v for k, v in totals.items() if k.startswith("exec_blocked:")}
    return {
        "scans": scans,
        "origins": dict(origins),
        "entered": int(totals.get("entered", 0)),
        "proposals_created": int(totals.get("proposals_created", 0)),
        "executed": int(totals.get("executed", 0)),
        "proposal_failed": int(totals.get("proposal_failed", 0)),
        "safety_blocked_total": int(totals.get("safety_blocked", 0)),
        "exec_blocked_candidates": int(totals.get("exec_blocked_candidates", 0)),
        "dropped": dict(sorted(dropped.items())),
        "safety_blocked": dict(sorted(safety.items())),
        "exec_blocked": dict(sorted(exec_blocked.items())),
    }


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
