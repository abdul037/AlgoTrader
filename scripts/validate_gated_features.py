#!/usr/bin/env python3
"""Monday validation: check the three default-off features against real data
before enabling them.

Run on the deployed bot (or anywhere with market-data + the paper DB):

    python -m scripts.validate_gated_features          # human-readable readout
    python -m scripts.validate_gated_features --json    # machine-readable JSON

It reports, without changing any setting or placing any trade:

  1. Regime router — the current market regime and which strategy families it
     would switch off right now, per timeframe. Decide whether that matches your
     intent before flipping ``regime_router_enabled``.
  2. Drawdown governor — a replay of the closed paper-trade history with vs
     without the governor, so you can see whether it would have cut drawdowns.
  3. Cross-sectional momentum — the configured concentration setting and the
     honest note that its effect is measured at scan time (next scan's kept
     count), since it is a live selection-layer filter over scan candidates.

Each feature yields a one-line GO / NO-GO / REVIEW / NEED-DATA verdict. The
``--json`` form emits ``{"features": [...], "overall": "..."}`` so the dashboard
or notification layer can surface the verdicts automatically. Nothing is enabled
by this script; it only measures.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

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


def _regime_result(app) -> dict[str, Any]:
    state = app.state
    settings = state.settings
    enabled = bool(getattr(settings, "regime_router_enabled", False))
    screener = getattr(state, "screener_service", None) or getattr(state, "market_screener_service", None)
    intelligence = getattr(screener, "intelligence", None)
    if intelligence is None:
        return _result("regime_router", "regime_router_enabled", enabled, "NEED-DATA",
                       "screener/intelligence unavailable — cannot compute regime")
    try:
        regime = intelligence.market_regime_signal(force_refresh=True)
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


def _governor_result(app) -> dict[str, Any]:
    state = app.state
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


def _momentum_result(app) -> dict[str, Any]:
    settings = app.state.settings
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


def collect(app) -> dict[str, Any]:
    features = [_regime_result(app), _governor_result(app), _momentum_result(app)]
    overall = min((f["verdict"] for f in features), key=lambda v: VERDICT_RANK.get(v, 99))
    return {"features": features, "overall": overall}


def _print_text(report: dict[str, Any]) -> None:
    titles = {
        "regime_router": "[1] Regime router — what enabling it would change right now:",
        "drawdown_governor": "[2] Drawdown governor — replay on the paper track record:",
        "cross_sectional_momentum": "[3] Cross-sectional momentum — configured concentration:",
    }
    print("=" * 72)
    print("GATED-FEATURE VALIDATION (read-only; enables nothing)")
    print("=" * 72)
    for feat in report["features"]:
        print("\n" + titles.get(feat["feature"], feat["feature"]))
        print(f"  Flag: {feat['flag']} = {feat['enabled']}")
        m = feat["metrics"]
        if feat["feature"] == "regime_router" and "regime" in m:
            r = m["regime"]
            print(f"  Regime: trend={r['trend']} volatility={r['volatility']} breadth={r['breadth']}")
            print(f"  Families the router would run: {', '.join(m['allowed_families'])}")
            print(f"  Specs kept overall: {m['specs_after']}/{m['specs_before']}")
            for tf, row in m.get("per_timeframe", {}).items():
                print(f"    {tf}: keep {row['after']}/{row['before']} (drops {row['dropped']} in {', '.join(row['dropped_families'])})")
        elif feat["feature"] == "drawdown_governor" and "trades" in m:
            print(f"  Trades replayed: {m['trades']}")
            print(f"  Baseline P&L: ${m['baseline_pnl_usd']:,.2f}  |  Governed P&L: ${m['governed_pnl_usd']:,.2f}  (delta ${m['delta_usd']:,.2f})")
            print(f"  Max drawdown  baseline ${m['baseline_max_drawdown_usd']:,.2f} -> governed ${m['governed_max_drawdown_usd']:,.2f}")
        elif feat["feature"] == "cross_sectional_momentum":
            print(f"  Keep top {m['top_pct']:g}% by momentum. {m['note']}")
        print(f"  >> {feat['verdict']}: {feat['detail']}")
    print(f"\nOverall: {report['overall']}")
    print("Neither feature was enabled. Flip a flag only if its verdict matches intent.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only gated-feature validation.")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON instead of text")
    args = parser.parse_args(argv)

    app = create_app()
    report = collect(app)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_text(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
