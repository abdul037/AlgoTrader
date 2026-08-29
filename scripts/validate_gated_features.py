#!/usr/bin/env python3
"""Monday validation: check the two default-off features against real data before
enabling them.

Run on the deployed bot (or anywhere with market-data + the paper DB):

    python -m scripts.validate_gated_features

It reports, without changing any setting or placing any trade:

  1. Regime router — the current market regime and which strategy families it
     would switch off right now, per timeframe. Decide whether that matches your
     intent before flipping ``regime_router_enabled``.
  2. Drawdown governor — a replay of the closed paper-trade history with vs
     without the governor, so you can see whether it would have cut drawdowns.

Neither feature is enabled by this script; it only measures.
"""

from __future__ import annotations

import sys

from app.main import create_app
from app.performance.gated_feature_validation import (
    regime_router_impact,
    simulate_drawdown_governor,
)


def _regime_section(app) -> None:
    state = app.state
    screener = getattr(state, "screener_service", None) or getattr(state, "market_screener_service", None)
    intelligence = getattr(screener, "intelligence", None)
    if intelligence is None:
        print("  (screener/intelligence unavailable — cannot compute regime)")
        return
    regime = None
    try:
        regime = intelligence.market_regime_signal(force_refresh=True)
    except Exception as exc:  # noqa: BLE001
        print(f"  (regime computation failed: {exc})")
        return
    if regime is None:
        print("  (no benchmark history — regime unavailable; router would run every family)")
        return

    from app.backtesting.strategy_selection import strategy_specs_for

    timeframes = ["1m", "5m", "10m", "15m", "1h", "1d", "1w"]
    specs_by_tf = {tf: strategy_specs_for(state.settings, timeframe=tf) for tf in timeframes}
    impact = regime_router_impact(specs_by_tf, regime)

    print(f"  Regime: trend={impact.regime['trend']} volatility={impact.regime['volatility']} breadth={impact.regime['breadth']}")
    print(f"  Families the router would run: {', '.join(impact.allowed_families)}")
    print(f"  Specs kept overall: {impact.total_specs_after}/{impact.total_specs_before}")
    for tf, row in impact.per_timeframe.items():
        if row["dropped"]:
            print(f"    {tf}: keep {row['after']}/{row['before']} (drops {row['dropped']} in {', '.join(row['dropped_families'])})")


def _governor_section(app) -> None:
    state = app.state
    trade_repo = getattr(state, "paper_trade_repository", None)
    if trade_repo is None:
        print("  (paper_trade_repository unavailable)")
        return
    trades = trade_repo.list(limit=5000)
    if not trades:
        print("  (no closed paper trades yet — governor can't be validated until Stage 1 accrues trades)")
        return
    settings = state.settings
    sim = simulate_drawdown_governor(
        trades,
        capital_usd=float(getattr(settings, "paper_account_balance_usd", 100_000.0) or 100_000.0),
        soft_pct=float(getattr(settings, "drawdown_governor_soft_pct", 2.0)),
        hard_pct=float(getattr(settings, "drawdown_governor_hard_pct", 5.0)),
        floor=float(getattr(settings, "drawdown_governor_floor", 0.25)),
    )
    print(f"  Trades replayed: {sim.trades}")
    print(f"  Baseline P&L: ${sim.baseline_pnl_usd:,.2f}  |  Governed P&L: ${sim.governed_pnl_usd:,.2f}  (delta ${sim.delta_usd:,.2f})")
    print(f"  Max drawdown  baseline ${sim.baseline_max_drawdown_usd:,.2f} -> governed ${sim.governed_max_drawdown_usd:,.2f}")
    print(f"  Recommendation: {sim.recommendation}")


def main() -> int:
    app = create_app()
    print("=" * 72)
    print("GATED-FEATURE VALIDATION (read-only; enables nothing)")
    print("=" * 72)
    print("\n[1] Regime router — what enabling it would change right now:")
    _regime_section(app)
    print("\n[2] Drawdown governor — replay on the paper track record:")
    _governor_section(app)
    print("\nNeither feature was enabled. Flip the flags only if the above matches intent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
