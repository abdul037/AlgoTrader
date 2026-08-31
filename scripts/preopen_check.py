#!/usr/bin/env python3
"""Pre-open readiness check: is the bot set up to produce clean Stage-1 data today?

Run before the US open (or any morning):

    python -m scripts.preopen_check          # human-readable readout
    python -m scripts.preopen_check --json    # machine-readable JSON

Read-only — it changes no setting and places no trade. It answers one question
the dashboard makes you piece together: *will today actually accrue measurement
data, safely?* Four groups, each with a GO / WARN / NO-GO verdict, plus an
overall verdict that is the most-blocking of the four:

  1. Safety      — real trading OFF (paper-only), kill-switch off, not paused.
  2. Deploy      — which commit is actually running.
  3. Trading mode — shadow (proposes, places nothing) vs supervised/unattended,
                    and whether the propose/approve/execute flags let a trade
                    actually reach the paper broker.
  4. Gated base  — the three default-off features off, so Stage 1 measures the
                    base edge cleanly.

A `shadow` operating mode or a paused/kill-switched worker is the usual reason a
"running" bot produces zero closed trades — this surfaces it before the open,
not after a wasted session.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import Any

from app.main import create_app
from app.performance.gated_feature_report import collect_feature_verdicts

for _noisy in ("app.broker.etoro_market_data", "yfinance", "app.broker.alpaca_data_provider"):
    logging.getLogger(_noisy).setLevel(logging.CRITICAL)

# Most-blocking first, so the overall verdict never hides a NO-GO behind a GO.
VERDICT_RANK = {"NO-GO": 0, "WARN": 1, "GO": 2}


def _group(name: str, verdict: str, detail: str, **facts: Any) -> dict[str, Any]:
    return {"group": name, "verdict": verdict, "detail": detail, "facts": facts}


def _safety(app) -> dict[str, Any]:
    state = app.state
    settings = state.settings
    real_trading = bool(getattr(settings, "enable_real_trading", False))
    automation = getattr(state, "automation_service", None)
    paused = bool(automation.is_paused()) if automation is not None else None
    kill = bool(automation.kill_switch_enabled()) if automation is not None else None
    facts = {"enable_real_trading": real_trading, "paused": paused, "kill_switch": kill}

    if real_trading:
        return _group("safety", "NO-GO", "ENABLE_REAL_TRADING is ON — must stay paper-only", **facts)
    if kill:
        return _group("safety", "NO-GO", "kill-switch is ON — the bot will place nothing", **facts)
    if paused:
        return _group("safety", "WARN", "automation is paused — no trades until resumed", **facts)
    return _group("safety", "GO", "paper-only, not paused, kill-switch off", **facts)


def _deploy(app) -> dict[str, Any]:
    commit = os.environ.get("RAILWAY_GIT_COMMIT_SHA", "")
    branch = os.environ.get("RAILWAY_GIT_BRANCH", "")
    if commit:
        return _group("deploy", "GO", f"running {commit[:7]} on {branch or '?'}", commit=commit, branch=branch)
    return _group("deploy", "WARN", "no RAILWAY_GIT_COMMIT_SHA (local run, or env not injected)", commit="", branch=branch)


def _trading_mode(app) -> dict[str, Any]:
    s = app.state.settings
    mode = str(getattr(s, "paper_auto_operation_mode", "shadow"))
    facts = {
        "paper_auto_operation_mode": mode,
        "auto_propose_enabled": bool(getattr(s, "auto_propose_enabled", False)),
        "paper_auto_approve_proposals": bool(getattr(s, "paper_auto_approve_proposals", False)),
        "auto_execution_worker_enabled": bool(getattr(s, "auto_execution_worker_enabled", False)),
        "auto_execute_after_approval": bool(getattr(s, "auto_execute_after_approval", False)),
    }
    # For a closed paper trade to accrue today: propose -> approve -> execute must
    # all be open. Shadow never executes; supervised needs auto-approve to run
    # unattended; unattended needs the execution worker on.
    proposes = facts["auto_propose_enabled"]
    approves = facts["paper_auto_approve_proposals"] or facts["auto_execute_after_approval"]
    executes = facts["auto_execution_worker_enabled"]

    if mode == "shadow":
        return _group("trading_mode", "WARN",
                      "shadow mode — proposes but places nothing; today accrues no measurement data", **facts)
    if not proposes:
        return _group("trading_mode", "WARN",
                      f"{mode} mode but auto_propose is off — no proposals will be generated", **facts)
    if not (approves and executes):
        gaps = []
        if not approves:
            gaps.append("auto-approve off")
        if not executes:
            gaps.append("execution worker off")
        return _group("trading_mode", "WARN",
                      f"{mode} mode but {' and '.join(gaps)} — proposals won't reach the paper broker unattended", **facts)
    return _group("trading_mode", "GO",
                  f"{mode} mode with propose+approve+execute open — trades will accrue", **facts)


def _gated_baseline(app) -> dict[str, Any]:
    report = collect_feature_verdicts(app.state, force_refresh=False)
    on = [f["feature"] for f in report["features"] if f["enabled"]]
    facts = {"features_on": on, "gated_overall": report["overall"]}
    if on:
        return _group("gated_baseline", "WARN",
                      f"gated features ON ({', '.join(on)}) — Stage-1 baseline is not clean", **facts)
    return _group("gated_baseline", "GO", "all gated features off — clean Stage-1 baseline", **facts)


def collect(app) -> dict[str, Any]:
    groups = [_safety(app), _deploy(app), _trading_mode(app), _gated_baseline(app)]
    overall = min((g["verdict"] for g in groups), key=lambda v: VERDICT_RANK.get(v, 99))
    return {"groups": groups, "overall": overall}


def _print_text(report: dict[str, Any]) -> None:
    print("=" * 72)
    print("PRE-OPEN READINESS CHECK (read-only; changes nothing)")
    print("=" * 72)
    titles = {
        "safety": "[1] Safety",
        "deploy": "[2] Deploy",
        "trading_mode": "[3] Trading mode — will today accrue trades?",
        "gated_baseline": "[4] Gated-feature baseline",
    }
    for g in report["groups"]:
        print(f"\n{titles.get(g['group'], g['group'])}")
        print(f"  >> {g['verdict']}: {g['detail']}")
        for k, v in g["facts"].items():
            print(f"     {k} = {v}")
    print(f"\nOverall: {report['overall']}")
    if report["overall"] != "GO":
        print("Not fully green — resolve the WARN/NO-GO groups above before relying on today's data.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only pre-open readiness check.")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args(argv)

    app = create_app()
    report = collect(app)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_text(report)
    # Exit non-zero on a hard NO-GO so the check can gate a morning automation.
    return 2 if report["overall"] == "NO-GO" else 0


if __name__ == "__main__":
    sys.exit(main())
