from __future__ import annotations

from types import SimpleNamespace

from app.automation.unattended import PaperAutoTradingService
from tests.conftest import make_settings


def _candidate(*, strategy_name: str = "swing_trend", backtest_validated: bool = True):
    return SimpleNamespace(
        symbol="AAPL",
        strategy_name=strategy_name,
        execution_ready=True,
        signal_role="entry_long",
        score=80,
        stop_loss=95,
        take_profit=110,
        metadata={"alert_eligible": True, "backtest_validated": backtest_validated},
    )


def _service(
    tmp_path,
    *,
    operation_mode,
    strategy_approved,
    rollout_ready,
    strategy_paper_approved=False,
    exploration_enabled=False,
    bypass_production_approval=False,
    allowed_strategies=None,
    require_backtest_validated=False,
    execution_mode="paper",
    enable_real_trading=False,
):
    settings = make_settings(
        tmp_path,
        execution_mode=execution_mode,
        enable_real_trading=enable_real_trading,
        paper_auto_approve_proposals=True,
        auto_execution_worker_enabled=True,
        paper_auto_operation_mode=operation_mode,
        alpaca_expected_account_number="PAPER-1",
        paper_scanner_exploration_enabled=exploration_enabled,
        paper_scanner_bypass_production_approval=bypass_production_approval,
        paper_scanner_allowed_strategies=allowed_strategies or ["all"],
        paper_exploration_require_backtest_validated=require_backtest_validated,
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
            strategy_production_approved=lambda _strategy: strategy_approved,
            strategy_paper_exploration_approved=lambda _strategy: strategy_paper_approved,
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
    assert "paper_auto_operation_mode_supervised" in blockers
    assert "institutional_rollout_not_ready" not in blockers


def test_unattended_auto_requires_signed_rollout_readiness(tmp_path):
    blockers = _service(
        tmp_path,
        operation_mode="unattended",
        strategy_approved=True,
        rollout_ready=False,
    ).candidate_blockers(_candidate())

    assert "institutional_rollout_not_ready" in blockers


def test_paper_exploration_bypasses_only_strategy_production_gate(tmp_path):
    service = _service(
        tmp_path,
        operation_mode="unattended",
        strategy_approved=False,
        strategy_paper_approved=True,
        rollout_ready=False,
        exploration_enabled=True,
        bypass_production_approval=True,
    )
    candidate = _candidate(backtest_validated=False)

    assert service.candidate_proposal_blockers(candidate) == []
    assert service.candidate_blockers(candidate) == []


def test_paper_exploration_bypass_is_blocked_for_live_or_real_trading(tmp_path):
    blockers = _service(
        tmp_path,
        operation_mode="unattended",
        strategy_approved=False,
        strategy_paper_approved=True,
        rollout_ready=False,
        exploration_enabled=True,
        bypass_production_approval=True,
        execution_mode="live",
        enable_real_trading=True,
    ).candidate_blockers(_candidate())

    assert "paper_only_policy" in blockers
    assert "strategy_not_production_approved" in blockers


def test_paper_exploration_requires_strategy_allowlist_match(tmp_path):
    blockers = _service(
        tmp_path,
        operation_mode="unattended",
        strategy_approved=False,
        strategy_paper_approved=True,
        rollout_ready=False,
        exploration_enabled=True,
        bypass_production_approval=True,
        allowed_strategies=["other_strategy"],
    ).candidate_blockers(_candidate())

    assert "strategy_not_production_approved" in blockers


def test_paper_exploration_requires_paper_stage_approval(tmp_path):
    blockers = _service(
        tmp_path,
        operation_mode="unattended",
        strategy_approved=False,
        strategy_paper_approved=False,
        rollout_ready=False,
        exploration_enabled=True,
        bypass_production_approval=True,
    ).candidate_blockers(_candidate())

    assert "strategy_not_production_approved" in blockers


def test_paper_exploration_can_require_backtest_validation(tmp_path):
    blockers = _service(
        tmp_path,
        operation_mode="unattended",
        strategy_approved=False,
        strategy_paper_approved=True,
        rollout_ready=False,
        exploration_enabled=True,
        bypass_production_approval=True,
        require_backtest_validated=True,
    ).candidate_blockers(_candidate(backtest_validated=False))

    assert "candidate_not_backtest_validated" in blockers
    assert "strategy_not_production_approved" not in blockers
