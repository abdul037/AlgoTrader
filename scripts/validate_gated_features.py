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
from app.performance.gated_feature_report import collect_feature_verdicts

# Market-data providers log failed fetches at ERROR with full tracebacks. In a
# read-only validation run those are expected (no key / no network) and drown
# the readout, so quiet them; the collector below already handles missing data.
for _noisy in ("app.broker.etoro_market_data", "yfinance", "app.broker.alpaca_data_provider"):
    logging.getLogger(_noisy).setLevel(logging.CRITICAL)


def collect(app) -> dict[str, Any]:
    # The Monday run wants the freshest regime, so force a refresh.
    return collect_feature_verdicts(app.state, force_refresh=True)


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
