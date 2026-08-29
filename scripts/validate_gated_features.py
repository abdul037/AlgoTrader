#!/usr/bin/env python3
"""Monday validation: check the three default-off features against real data
before enabling them.

Run on the deployed bot (or anywhere with market-data + the paper DB):

    python -m scripts.validate_gated_features

It reports, without changing any setting or placing any trade:

  1. Regime router — the current market regime and which strategy families it
     would switch off right now, per timeframe. Decide whether that matches your
     intent before flipping ``regime_router_enabled``.
  2. Drawdown governor — a replay of the closed paper-trade history with vs
     without the governor, so you can see whether it would have cut drawdowns.
  3. Cross-sectional momentum — the configured concentration setting and the
     honest note that its effect is measured at scan time (next scan's kept
     count), since it is a live selection-layer filter over scan candidates.

Each section ends with a one-line GO / NO-GO / NEED-DATA verdict. Nothing is
enabled by this script; it only measures.
"""

from __future__ import annotations

import logging
import sys

from app.main import create_app
from app.performance.gated_feature_validation import (
    regime_router_impact,
    simulate_drawdown_governor,
)

# Market-data providers log failed fetches at ERROR with full tracebacks. In a
# read-only validation run those are expected (no key / no network) and drown
# the readout, so quiet them; the sections below already handle missing data.
for _noisy in ("app.broker.etoro_market_data", "yfinance", "app.broker.alpaca_data_provider"):
    logging.getLogger(_noisy).setLevel(logging.CRITICAL)


def _verdict(label: str, decision: str, detail: str) -> None:
    print(f"  >> {decision}: {label} — {detail}")


def _regime_section(app) -> str:
    state = app.state
    settings = state.settings
    enabled = bool(getattr(settings, "regime_router_enabled", False))
    print(f"  Flag: regime_router_enabled = {enabled}")
    screener = getattr(state, "screener_service", None) or getattr(state, "market_screener_service", None)
    intelligence = getattr(screener, "intelligence", None)
    if intelligence is None:
        _verdict("regime router", "NEED-DATA", "screener/intelligence unavailable — cannot compute regime")
        return "NEED-DATA"
    regime = None
    try:
        regime = intelligence.market_regime_signal(force_refresh=True)
    except Exception as exc:  # noqa: BLE001
        _verdict("regime router", "NEED-DATA", f"regime computation failed: {exc}")
        return "NEED-DATA"
    if regime is None:
        _verdict("regime router", "NEED-DATA", "no benchmark history — router would run every family")
        return "NEED-DATA"

    from app.backtesting.strategy_selection import strategy_specs_for

    timeframes = ["1m", "5m", "10m", "15m", "1h", "1d", "1w"]
    specs_by_tf = {tf: strategy_specs_for(settings, timeframe=tf) for tf in timeframes}
    impact = regime_router_impact(specs_by_tf, regime)

    print(f"  Regime: trend={impact.regime['trend']} volatility={impact.regime['volatility']} breadth={impact.regime['breadth']}")
    print(f"  Families the router would run: {', '.join(impact.allowed_families)}")
    print(f"  Specs kept overall: {impact.total_specs_after}/{impact.total_specs_before}")
    for tf, row in impact.per_timeframe.items():
        if row["dropped"]:
            print(f"    {tf}: keep {row['after']}/{row['before']} (drops {row['dropped']} in {', '.join(row['dropped_families'])})")

    dropped_total = impact.total_specs_before - impact.total_specs_after
    if dropped_total == 0:
        _verdict("regime router", "GO", "regime is broad — router drops nothing now; enabling is a no-op until regime narrows")
    else:
        _verdict(
            "regime router",
            "REVIEW",
            f"router would drop {dropped_total} specs in this regime — enable only if those families should be off now",
        )
    return "OK"


def _governor_section(app) -> str:
    state = app.state
    trade_repo = getattr(state, "paper_trade_repository", None)
    if trade_repo is None:
        _verdict("drawdown governor", "NEED-DATA", "paper_trade_repository unavailable")
        return "NEED-DATA"
    trades = trade_repo.list(limit=5000)
    settings = state.settings
    enabled = bool(getattr(settings, "drawdown_governor_enabled", False))
    print(f"  Flag: drawdown_governor_enabled = {enabled}  "
          f"(soft {getattr(settings, 'drawdown_governor_soft_pct', 2.0)}% / "
          f"hard {getattr(settings, 'drawdown_governor_hard_pct', 5.0)}% / "
          f"floor {getattr(settings, 'drawdown_governor_floor', 0.25)})")
    if not trades:
        _verdict("drawdown governor", "NEED-DATA", "no closed paper trades yet — accrues during Stage 1")
        return "NEED-DATA"
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
    dd_cut = sim.baseline_max_drawdown_usd - sim.governed_max_drawdown_usd
    if dd_cut > 0 and sim.delta_usd >= -abs(sim.baseline_pnl_usd) * 0.25:
        _verdict("drawdown governor", "GO", f"cuts max drawdown by ${dd_cut:,.2f} for acceptable P&L give-up (${sim.delta_usd:,.2f})")
    elif dd_cut <= 0:
        _verdict("drawdown governor", "NO-GO", "does not reduce drawdown on this history — leave off")
    else:
        _verdict("drawdown governor", "REVIEW", f"cuts drawdown ${dd_cut:,.2f} but gives up ${abs(sim.delta_usd):,.2f} P&L — judge the trade-off")
    print(f"  (module recommendation: {sim.recommendation})")
    return "OK"


def _momentum_section(app) -> str:
    settings = app.state.settings
    enabled = bool(getattr(settings, "cross_sectional_momentum_enabled", False))
    top_pct = float(getattr(settings, "cross_sectional_momentum_top_pct", 30.0))
    print(f"  Flag: cross_sectional_momentum_enabled = {enabled}  (keep top {top_pct:g}% by momentum)")
    print("  This is a live selection-layer filter over scan candidates, not a")
    print("  historical replay: with it on, the next scan keeps only the top")
    print(f"  {top_pct:g}% of ranked candidates. Effect is visible in that scan's kept count.")
    _verdict(
        "cross-sectional momentum",
        "GO" if 0 < top_pct < 100 else "REVIEW",
        f"concentrates the book to the top {top_pct:g}% — enable when you want leader-only exposure; watch the next scan's kept count",
    )
    return "OK"


def main() -> int:
    app = create_app()
    print("=" * 72)
    print("GATED-FEATURE VALIDATION (read-only; enables nothing)")
    print("=" * 72)
    print("\n[1] Regime router — what enabling it would change right now:")
    _regime_section(app)
    print("\n[2] Drawdown governor — replay on the paper track record:")
    _governor_section(app)
    print("\n[3] Cross-sectional momentum — configured concentration:")
    _momentum_section(app)
    print("\nNeither feature was enabled. Flip a flag only if its verdict matches intent.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
