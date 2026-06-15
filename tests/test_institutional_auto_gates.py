from __future__ import annotations

from types import SimpleNamespace

from app.automation.unattended import PaperAutoTradingService
from tests.conftest import make_settings


def _candidate():
    return SimpleNamespace(
        symbol="AAPL",
        strategy_name="swing_trend",
        execution_ready=True,
        signal_role="entry_long",
        score=80,
        stop_loss=95,
        take_profit=110,
        metadata={"alert_eligible": True, "backtest_validated": True},
    )


def _service(tmp_path, *, operation_mode, strategy_approved, rollout_ready):
    settings = make_settings(
        tmp_path,
        paper_auto_approve_proposals=True,
        auto_execution_worker_enabled=True,
        paper_auto_operation_mode=operation_mode,
        alpaca_expected_account_number="PAPER-1",
    )
    return PaperAutoTradingService(
        settings=settings,
        proposal_service=None,
        execution_coordinator=None,
        automation=SimpleNamespace(execution_blockers=lambda: []),
        reconciliation=SimpleNamespace(account_verified=lambda: True),
        safety_state=SimpleNamespace(
            is_blacklisted=lambda _symbol: False,
            strategy_active=lambda _strategy: True,
        ),
        executions=None,
        run_logs=None,
        notifier=None,
        alpaca_client=SimpleNamespace(
            is_regular_market_open=lambda: True,
            is_supported_equity=lambda _symbol: True,
        ),
        strategy_governance=SimpleNamespace(
            strategy_production_approved=lambda _strategy: strategy_approved
        ),
        institutional_governance=SimpleNamespace(
            readiness=lambda: {"ready": rollout_ready}
        ),
    )


def test_supervised_auto_blocks_unapproved_strategy(tmp_path):
    blockers = _service(
        tmp_path,
        operation_mode="supervised",
        strategy_approved=False,
        rollout_ready=False,
    ).candidate_blockers(_candidate())

    assert "strategy_not_production_approved" in blockers
    assert "institutional_rollout_not_ready" not in blockers


def test_unattended_auto_requires_signed_rollout_readiness(tmp_path):
    blockers = _service(
        tmp_path,
        operation_mode="unattended",
        strategy_approved=True,
        rollout_ready=False,
    ).candidate_blockers(_candidate())

    assert "institutional_rollout_not_ready" in blockers
