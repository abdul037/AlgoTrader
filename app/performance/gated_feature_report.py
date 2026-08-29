"""Collect GO / NO-GO verdicts for the three default-off features from live state.

This is the shared collector behind both the Monday validation CLI
(``scripts/validate_gated_features.py``) and the operator dashboard. It reads
the app's runtime state (screener intelligence for the current regime, the paper
trade repository for the governor replay, settings for the momentum config) and
returns a structured report the CLI renders as text and the dashboard renders as
a panel — one source of truth for the verdicts.

Pure verdict/impact math lives in ``gated_feature_validation``; this module only
gathers the live inputs and applies the enable-decision thresholds. It never
enables a feature or places a trade.
"""

from __future__ import annotations

from typing import Any

from app.performance.gated_feature_validation import (
    regime_router_impact,
    simulate_drawdown_governor,
)

# Ordered most-blocking first, so the "overall" verdict is the worst across
# features: a single NO-GO or NEED-DATA should not be hidden behind a GO.
VERDICT_RANK = {"NO-GO": 0, "REVIEW": 1, "NEED-DATA": 2, "GO": 3}


def _result(feature: str, flag: str, enabled: bool, verdict: str, detail: str, **metrics: Any) -> dict[str, Any]:
    return {
        "feature": feature,
        "flag": flag,
        "enabled": enabled,
        "verdict": verdict,
        "detail": detail,
        "metrics": metrics,
    }


def regime_verdict(state: Any, *, force_refresh: bool = False) -> dict[str, Any]:
    settings = state.settings
    enabled = bool(getattr(settings, "regime_router_enabled", False))
    screener = getattr(state, "screener_service", None) or getattr(state, "market_screener_service", None)
    intelligence = getattr(screener, "intelligence", None)
    if intelligence is None:
        return _result("regime_router", "regime_router_enabled", enabled, "NEED-DATA",
                       "screener/intelligence unavailable — cannot compute regime")
    try:
        regime = intelligence.market_regime_signal(force_refresh=force_refresh)
    except Exception as exc:  # noqa: BLE001
        return _result("regime_router", "regime_router_enabled", enabled, "NEED-DATA",
                       f"regime computation failed: {exc}")
    if regime is None:
        return _result("regime_router", "regime_router_enabled", enabled, "NEED-DATA",
                       "no benchmark history — router would run every family")

    from app.backtesting.strategy_selection import strategy_specs_for

    timeframes = ["1m", "5m", "10m", "15m", "1h", "1d", "1w"]
    specs_by_tf = {tf: strategy_specs_for(settings, timeframe=tf) for tf in timeframes}
    impact = regime_router_impact(specs_by_tf, regime)
    dropped_total = impact.total_specs_before - impact.total_specs_after
    per_tf = {tf: row for tf, row in impact.per_timeframe.items() if row["dropped"]}

    if dropped_total == 0:
        verdict = "GO"
        detail = "regime is broad — router drops nothing now; enabling is a no-op until regime narrows"
    else:
        verdict = "REVIEW"
        detail = f"router would drop {dropped_total} specs in this regime — enable only if those families should be off now"

    return _result(
        "regime_router", "regime_router_enabled", enabled, verdict, detail,
        regime=impact.regime,
        allowed_families=impact.allowed_families,
        specs_before=impact.total_specs_before,
        specs_after=impact.total_specs_after,
        dropped_total=dropped_total,
        per_timeframe=per_tf,
    )


def governor_verdict(state: Any) -> dict[str, Any]:
    settings = state.settings
    enabled = bool(getattr(settings, "drawdown_governor_enabled", False))
    trade_repo = getattr(state, "paper_trade_repository", None)
    thresholds = {
        "soft_pct": float(getattr(settings, "drawdown_governor_soft_pct", 2.0)),
        "hard_pct": float(getattr(settings, "drawdown_governor_hard_pct", 5.0)),
        "floor": float(getattr(settings, "drawdown_governor_floor", 0.25)),
    }
    if trade_repo is None:
        return _result("drawdown_governor", "drawdown_governor_enabled", enabled, "NEED-DATA",
                       "paper_trade_repository unavailable", thresholds=thresholds)
    trades = trade_repo.list(limit=5000)
    if not trades:
        return _result("drawdown_governor", "drawdown_governor_enabled", enabled, "NEED-DATA",
                       "no closed paper trades yet — accrues during Stage 1", thresholds=thresholds)
    sim = simulate_drawdown_governor(
        trades,
        capital_usd=float(getattr(settings, "paper_account_balance_usd", 100_000.0) or 100_000.0),
        soft_pct=thresholds["soft_pct"],
        hard_pct=thresholds["hard_pct"],
        floor=thresholds["floor"],
    )
    dd_cut = sim.baseline_max_drawdown_usd - sim.governed_max_drawdown_usd
    if dd_cut > 0 and sim.delta_usd >= -abs(sim.baseline_pnl_usd) * 0.25:
        verdict = "GO"
        detail = f"cuts max drawdown by ${dd_cut:,.2f} for acceptable P&L give-up (${sim.delta_usd:,.2f})"
    elif dd_cut <= 0:
        verdict = "NO-GO"
        detail = "does not reduce drawdown on this history — leave off"
    else:
        verdict = "REVIEW"
        detail = f"cuts drawdown ${dd_cut:,.2f} but gives up ${abs(sim.delta_usd):,.2f} P&L — judge the trade-off"

    return _result(
        "drawdown_governor", "drawdown_governor_enabled", enabled, verdict, detail,
        thresholds=thresholds,
        trades=sim.trades,
        baseline_pnl_usd=sim.baseline_pnl_usd,
        governed_pnl_usd=sim.governed_pnl_usd,
        delta_usd=sim.delta_usd,
        baseline_max_drawdown_usd=sim.baseline_max_drawdown_usd,
        governed_max_drawdown_usd=sim.governed_max_drawdown_usd,
        drawdown_cut_usd=dd_cut,
        recommendation=sim.recommendation,
    )


def momentum_verdict(state: Any) -> dict[str, Any]:
    settings = state.settings
    enabled = bool(getattr(settings, "cross_sectional_momentum_enabled", False))
    top_pct = float(getattr(settings, "cross_sectional_momentum_top_pct", 30.0))
    verdict = "GO" if 0 < top_pct < 100 else "REVIEW"
    detail = (
        f"concentrates the book to the top {top_pct:g}% — enable when you want "
        "leader-only exposure; watch the next scan's kept count"
    )
    return _result(
        "cross_sectional_momentum", "cross_sectional_momentum_enabled", enabled, verdict, detail,
        top_pct=top_pct,
        note="live selection-layer filter; effect shows in the next scan's kept count, not a historical replay",
    )


def collect_feature_verdicts(state: Any, *, force_refresh: bool = False) -> dict[str, Any]:
    """All three feature verdicts plus the most-blocking ``overall`` verdict.

    ``force_refresh`` forces a fresh regime fetch (the Monday CLI run wants the
    latest; the dashboard uses the cached regime to stay fast).
    """

    features = [
        regime_verdict(state, force_refresh=force_refresh),
        governor_verdict(state),
        momentum_verdict(state),
    ]
    overall = min((f["verdict"] for f in features), key=lambda v: VERDICT_RANK.get(v, 99))
    return {"features": features, "overall": overall}
