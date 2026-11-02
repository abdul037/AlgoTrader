"""The boot-time funnel preflight must reproduce, from configuration alone, every
config-mismatch blocker that cost a live session in Sep 2026."""

from __future__ import annotations

from app.automation.funnel_preflight import run_funnel_preflight
from tests.conftest import make_settings

UNIVERSE = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AMD", "INTC", "CSCO"]

UNATTENDED = dict(
    execution_mode="paper",
    enable_real_trading=False,
    auto_propose_enabled=True,
    paper_auto_approve_proposals=True,
    auto_execution_worker_enabled=True,
    auto_execute_after_approval=True,
    paper_auto_operation_mode="unattended",
    alpaca_enabled=True,
    paper_broker="alpaca",
    market_universe_symbols=UNIVERSE,
    allowed_instruments=UNIVERSE,
    max_open_positions=8,
    max_trades_per_day=15,
    default_trade_amount_usd=12_500.0,
    max_trade_amount_usd=12_500.0,
    max_risk_per_trade_pct=1.0,
    auto_propose_risk_based_sizing=True,
)


def test_aggressive_paper_profile_is_fully_open(tmp_path) -> None:
    report = run_funnel_preflight(make_settings(tmp_path, **UNATTENDED), quote_fn=lambda s: 250.0)

    assert report["global_blockers"] == []
    assert report["blocked_symbols"] == 0
    assert report["open_symbols"] == len(UNIVERSE)
    assert report["funnel_open"] is True
    assert report["details"]["AAPL"]["shares"] >= 1


def test_allowlist_narrower_than_universe_is_reported_per_symbol(tmp_path) -> None:
    # The 2026-09-08 failure: 25-name universe, 6-name allowlist.
    settings = make_settings(tmp_path, **{**UNATTENDED, "allowed_instruments": ["NVDA", "GOOGL", "AMD"]})

    report = run_funnel_preflight(settings)

    assert report["symbols"]["NVDA"] == "open"
    assert report["symbols"]["INTC"].startswith("instrument:")
    assert "not in the allowed instrument list" in report["symbols"]["INTC"]
    assert report["blocked_symbols"] == len(UNIVERSE) - 3


def test_per_trade_cap_below_share_price_is_reported(tmp_path) -> None:
    # The 2026-09-08 AMD failure: $500 cap, $501 share.
    settings = make_settings(tmp_path, **{**UNATTENDED, "default_trade_amount_usd": 500.0, "max_trade_amount_usd": 500.0})

    report = run_funnel_preflight(settings, quote_fn=lambda s: 501.0 if s == "AMD" else 100.0)

    assert report["symbols"]["AMD"] == "one_share_exceeds_max_trade_amount"
    assert report["symbols"]["AAPL"] == "open"


def test_global_switches_are_reported(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        **{**UNATTENDED, "paper_auto_operation_mode": "supervised", "auto_execution_worker_enabled": False},
    )

    report = run_funnel_preflight(settings)

    assert "paper_auto_operation_mode_not_unattended" in report["global_blockers"]
    assert "auto_execution_worker_disabled" in report["global_blockers"]
    assert report["funnel_open"] is False


def test_automation_scan_blockers_surface_as_global(tmp_path) -> None:
    class Automation:
        def scan_blockers(self):
            return ["circuit_breaker:missing_bracket_protection:GOOGL"]

    report = run_funnel_preflight(make_settings(tmp_path, **UNATTENDED), automation=Automation())

    assert "automation:circuit_breaker:missing_bracket_protection:GOOGL" in report["global_blockers"]


def test_real_trading_flag_is_a_global_blocker_for_the_paper_funnel(tmp_path) -> None:
    report = run_funnel_preflight(make_settings(tmp_path, **{**UNATTENDED, "enable_real_trading": True}))

    assert "enable_real_trading_is_true" in report["global_blockers"]
