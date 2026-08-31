"""Verdict logic for the pre-open readiness check."""

from __future__ import annotations

import json
from types import SimpleNamespace

from scripts import preopen_check as pc


def _app(*, real_trading=False, paused=False, kill=False, mode="unattended",
         propose=True, approve=True, execute=True):
    settings = SimpleNamespace(
        enable_real_trading=real_trading,
        paper_auto_operation_mode=mode,
        auto_propose_enabled=propose,
        paper_auto_approve_proposals=approve,
        auto_execution_worker_enabled=execute,
        auto_execute_after_approval=False,
        cross_sectional_momentum_enabled=False,
        cross_sectional_momentum_top_pct=30.0,
        regime_router_enabled=False,
        drawdown_governor_enabled=False,
        drawdown_governor_soft_pct=2.0,
        drawdown_governor_hard_pct=5.0,
        drawdown_governor_floor=0.25,
    )
    automation = SimpleNamespace(is_paused=lambda: paused, kill_switch_enabled=lambda: kill)
    state = SimpleNamespace(
        settings=settings,
        automation_service=automation,
        screener_service=None,
        market_screener_service=None,
        paper_trade_repository=None,
    )
    return SimpleNamespace(state=state)


def test_safety_flags_real_trading_as_nogo():
    assert pc._safety(_app(real_trading=True))["verdict"] == "NO-GO"


def test_safety_flags_kill_switch_as_nogo():
    assert pc._safety(_app(kill=True))["verdict"] == "NO-GO"


def test_safety_flags_paused_as_warn():
    assert pc._safety(_app(paused=True))["verdict"] == "WARN"


def test_safety_clean_is_go():
    assert pc._safety(_app())["verdict"] == "GO"


def test_shadow_mode_warns_no_data():
    r = pc._trading_mode(_app(mode="shadow"))
    assert r["verdict"] == "WARN"
    assert "accrues no measurement data" in r["detail"]


def test_unattended_all_open_is_go():
    assert pc._trading_mode(_app(mode="unattended"))["verdict"] == "GO"


def test_unattended_missing_execute_warns():
    r = pc._trading_mode(_app(mode="unattended", execute=False))
    assert r["verdict"] == "WARN"
    assert "execution worker off" in r["detail"]


def test_overall_is_most_blocking_and_json_serialisable():
    # real trading on -> NO-GO must dominate the overall verdict.
    report = pc.collect(_app(real_trading=True))
    assert report["overall"] == "NO-GO"
    json.dumps(report, default=str)


def test_overall_go_when_everything_ready():
    report = pc.collect(_app())
    # deploy WARNs locally (no RAILWAY env), so overall is WARN, never NO-GO.
    assert report["overall"] in {"GO", "WARN"}
    assert all(g["verdict"] != "NO-GO" for g in report["groups"])
