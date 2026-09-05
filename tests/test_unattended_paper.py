from __future__ import annotations

from collections import Counter
from datetime import timedelta
from types import SimpleNamespace

from app.automation.reconciliation import AlpacaReconciliationService
from app.automation.service import AutomationService
from app.automation.unattended import PaperAutoTradingService
from app.models.execution import ExecutionRecord, PortfolioPosition
from app.storage.db import Database
from app.storage.repositories import (
    BrokerOrderSnapshotRepository,
    BrokerPositionSnapshotRepository,
    ExecutionRepository,
    RunLogRepository,
    RuntimeStateRepository,
    SafetyStateRepository,
)
from app.utils.time import utc_now
from tests.conftest import make_settings


class FlatAlpaca:
    def __init__(self, account_number: str):
        self.account_number = account_number
        self.cancel_calls = 0
        self.close_calls = 0

    def get_account_identity(self):
        return {
            "account_number": self.account_number,
            "trading_blocked": False,
            "equity": 100000.0,
            "cash": 100000.0,
        }

    def get_all_orders(self):
        return []

    def get_portfolio(self):
        return SimpleNamespace(positions=[])

    def cancel_all_orders(self):
        self.cancel_calls += 1
        return 0

    def close_all_positions(self):
        self.close_calls += 1
        return 0


class Router:
    def __init__(self, client):
        self.client = client

    def all_clients(self):
        return [self.client]


def reconciliation_service(tmp_path, *, expected: str, actual: str):
    settings = make_settings(
        tmp_path,
        alpaca_expected_account_number=expected,
        alpaca_reconciliation_enabled=True,
    )
    db = Database(settings)
    db.initialize()
    state = RuntimeStateRepository(db)
    logs = RunLogRepository(db)
    alpaca = FlatAlpaca(actual)
    automation = AutomationService(
        settings=settings,
        runtime_state=state,
        run_logs=logs,
        broker_router=Router(alpaca),
    )
    service = AlpacaReconciliationService(
        settings=settings,
        alpaca_client=alpaca,
        executions=ExecutionRepository(db),
        broker_orders=BrokerOrderSnapshotRepository(db),
        broker_positions=BrokerPositionSnapshotRepository(db),
        safety_state=SafetyStateRepository(db),
        runtime_state=state,
        run_logs=logs,
        automation=automation,
    )
    return service, automation


def test_reconciliation_retries_transient_broker_errors(tmp_path, monkeypatch):
    service, automation = reconciliation_service(tmp_path, expected="PAPER-1", actual="PAPER-1")
    service.settings.alpaca_reconciliation_max_attempts = 3
    service.settings.alpaca_reconciliation_retry_backoff_seconds = 0
    calls = {"count": 0}
    original_get_account = service.alpaca.get_account_identity

    def flaky_account():
        calls["count"] += 1
        if calls["count"] < 3:
            raise ConnectionError("temporary broker disconnect")
        return original_get_account()

    monkeypatch.setattr(service.alpaca, "get_account_identity", flaky_account)

    result = service.reconcile()

    assert result["status"] == "ok"
    assert calls["count"] == 3
    assert automation.status().kill_switch_enabled is False


def test_reconciliation_records_failure_only_after_retry_attempts(tmp_path):
    service, automation = reconciliation_service(tmp_path, expected="PAPER-1", actual="PAPER-1")
    service.settings.alpaca_reconciliation_max_attempts = 3
    service.settings.alpaca_reconciliation_retry_backoff_seconds = 0
    service.settings.reconciliation_failures_before_kill_switch = 1
    calls = {"count": 0}

    def failing_account():
        calls["count"] += 1
        raise ConnectionError("broker unavailable")

    service.alpaca.get_account_identity = failing_account

    result = service.reconcile()

    assert result["status"] == "error"
    assert calls["count"] == 3
    assert result["consecutive_failures"] == 1
    assert automation.status().kill_switch_enabled is True


def test_reconciliation_verifies_expected_paper_account(tmp_path):
    service, automation = reconciliation_service(tmp_path, expected="PAPER-1", actual="PAPER-1")

    result = service.reconcile()

    assert result["status"] == "ok"
    assert automation.status().account_verified is True
    assert automation.status().kill_switch_enabled is False


def test_account_mismatch_immediately_trips_kill_switch(tmp_path):
    service, automation = reconciliation_service(tmp_path, expected="PAPER-1", actual="PAPER-2")

    result = service.reconcile()

    assert result["status"] == "error"
    assert "account_mismatch:expected=PAPER-1:actual=PAPER-2" in result["issues"]
    assert automation.status().kill_switch_enabled is True
    assert automation.status().paused is True
    assert service.alpaca.cancel_calls == 0
    assert service.alpaca.close_calls == 0


def test_reconciliation_persists_and_closes_position_snapshots(tmp_path):
    service, _automation = reconciliation_service(tmp_path, expected="PAPER-1", actual="PAPER-1")
    service.alpaca.get_portfolio = lambda: SimpleNamespace(
        positions=[
            PortfolioPosition(
                symbol="AAPL",
                quantity=2,
                average_price=190,
                market_value=382,
                unrealized_pnl=2,
            )
        ]
    )

    result = service.reconcile()
    active = service.broker_positions.list_active()

    assert result["status"] == "error"
    assert "unknown_position:AAPL" in result["issues"]
    assert active[0]["symbol"] == "AAPL"
    assert active[0]["quantity"] == 2
    assert service.alpaca.close_calls == 1

    service.alpaca.get_portfolio = lambda: SimpleNamespace(positions=[])
    service.reconcile()

    assert service.broker_positions.list_active() == []


def test_loss_limits_use_reconciled_exit_fill_time(tmp_path):
    settings = make_settings(tmp_path)
    db = Database(settings)
    db.initialize()
    executions = ExecutionRepository(db)
    closed_at = utc_now()
    executions.create(
        ExecutionRecord(
            proposal_id="proposal_old_entry",
            mode="alpaca_paper",
            realized_pnl_usd=-60,
            created_at=(closed_at - timedelta(days=2)).isoformat(),
            updated_at=closed_at.isoformat(),
            response_payload={
                "broker_execution": {
                    "legs": [{"status": "filled", "filled_at": closed_at.isoformat()}]
                }
            },
        )
    )

    assert executions.daily_loss_stats(closed_at) == (-60.0, 1)
    assert executions.period_realized_pnl(days=1) == -60.0
    assert executions.consecutive_losses() == 1


def test_unattended_candidate_requires_explicit_opt_in(tmp_path):
    settings = make_settings(
        tmp_path,
        paper_auto_approve_proposals=False,
        auto_execution_worker_enabled=False,
    )
    safety = SimpleNamespace(
        is_blacklisted=lambda _symbol: False,
        strategy_active=lambda _strategy: True,
    )
    automation = SimpleNamespace(execution_blockers=lambda: [])
    reconciliation = SimpleNamespace(account_verified=lambda: True)
    service = PaperAutoTradingService(
        settings=settings,
        proposal_service=None,
        execution_coordinator=None,
        automation=automation,
        reconciliation=reconciliation,
        safety_state=safety,
        executions=None,
        run_logs=None,
        notifier=None,
        alpaca_client=SimpleNamespace(
            is_regular_market_open=lambda: True,
            is_supported_equity=lambda _symbol: True,
        ),
    )
    candidate = SimpleNamespace(
        symbol="AAPL",
        strategy_name="ma_crossover",
        execution_ready=True,
        signal_role="entry_long",
        score=80,
        stop_loss=95,
        take_profit=110,
        metadata={"alert_eligible": True, "backtest_validated": True},
    )

    blockers = service.candidate_blockers(candidate)

    assert "paper_auto_approve_disabled" in blockers
    assert "auto_execution_worker_disabled" in blockers


def test_paper_near_miss_uses_configured_score_gap_without_bypassing_safety(tmp_path):
    settings = make_settings(
        tmp_path,
        execution_mode="paper",
        enable_real_trading=False,
        alpaca_expected_account_number="PAPER-1",
        paper_auto_approve_proposals=True,
        auto_execution_worker_enabled=True,
        paper_auto_operation_mode="unattended",
        paper_scanner_exploration_enabled=True,
        paper_scanner_bypass_production_approval=True,
        paper_exploration_signal_profile="balanced_loose",
        paper_near_miss_promotion_enabled=True,
        paper_near_miss_max_score_gap=5.0,
        # This test asserts the default safety path (near-miss requires human
        # approval), so pin the unattended-auto-exec bypass OFF independent of
        # the deployed .env default.
        paper_unattended_near_miss_auto_exec_enabled=False,
        market_universe_symbols=["NVDA"],
    )
    safety = SimpleNamespace(
        is_blacklisted=lambda _symbol: False,
        strategy_active=lambda _strategy: True,
    )
    governance = SimpleNamespace(
        strategy_production_approved=lambda _strategy: False,
        strategy_paper_exploration_approved=lambda _strategy: True,
    )
    service = PaperAutoTradingService(
        settings=settings,
        proposal_service=None,
        execution_coordinator=None,
        automation=SimpleNamespace(execution_blockers=lambda: []),
        reconciliation=SimpleNamespace(account_verified=lambda: True),
        safety_state=safety,
        executions=None,
        run_logs=None,
        notifier=None,
        alpaca_client=SimpleNamespace(
            is_regular_market_open=lambda: True,
            is_supported_equity=lambda _symbol: True,
        ),
        strategy_governance=governance,
    )
    candidate = SimpleNamespace(
        symbol="NVDA",
        strategy_name="momentum_breakout",
        execution_ready=True,
        signal_role="entry_long",
        score=56.0,
        stop_loss=95.0,
        take_profit=110.0,
        metadata={
            "alert_eligible": True,
            "backtest_validated": False,
            "signal_classification": "paper_near_miss",
            "source": "paper_near_miss",
        },
    )

    blockers = service.candidate_blockers(candidate)

    assert "candidate_score_below_auto_threshold" not in blockers
    assert "near_miss_requires_human_approval" in blockers
    assert "paper_auto_tier_supervised_only" in blockers
    assert "paper_lifecycle_evidence_unavailable" in blockers


def test_approve_enqueue_execute_records_blocked_candidate_into_funnel(tmp_path):
    # A blocked candidate must tally into the diagnostic funnel (exec_blocked
    # count + a per-blocker label) without executing anything. Pure observability.
    settings = make_settings(
        tmp_path,
        execution_mode="paper",
        enable_real_trading=False,
        paper_auto_operation_mode="unattended",
        paper_unattended_near_miss_auto_exec_enabled=False,
    )
    logged: list[tuple[str, dict]] = []
    run_logs = SimpleNamespace(log=lambda event, payload: logged.append((event, payload)))
    service = PaperAutoTradingService(
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
        run_logs=run_logs,
        notifier=None,
        alpaca_client=SimpleNamespace(
            is_regular_market_open=lambda: True,
            is_supported_equity=lambda _symbol: True,
        ),
    )
    near_miss_candidate = SimpleNamespace(
        symbol="NVDA",
        strategy_name="momentum_breakout",
        execution_ready=True,
        signal_role="entry_long",
        score=56.0,
        stop_loss=95.0,
        take_profit=110.0,
        metadata={
            "alert_eligible": True,
            "signal_classification": "paper_near_miss",
            "source": "paper_near_miss",
        },
    )
    proposal = SimpleNamespace(id="prop_1", order=SimpleNamespace(symbol="NVDA"))
    funnel: Counter[str] = Counter()

    result = service.approve_enqueue_execute(proposal, near_miss_candidate, funnel=funnel)

    assert result is None
    assert funnel["exec_blocked_candidates"] == 1
    assert funnel["executed"] == 0
    # The specific gate is attributed under an exec_blocked:<blocker> key.
    assert funnel["exec_blocked:near_miss_requires_human_approval"] == 1
    assert any(event == "paper_auto_candidate_blocked" for event, _ in logged)


def _clean_paper_auto_settings(tmp_path):
    # Every gate in candidate_blockers configured to PASS for a fully-validated,
    # strict-valid production candidate — the exact conditions that must hold for
    # the first autonomous paper trade to fire on Monday.
    return make_settings(
        tmp_path,
        execution_mode="paper",
        enable_real_trading=False,
        alpaca_expected_account_number="PAPER-1",
        paper_auto_approve_proposals=True,
        auto_execution_worker_enabled=True,
        paper_auto_operation_mode="unattended",
        paper_auto_approval_tier="tier2_strict_valid",
        # Drop the clean-lifecycle bootstrap so a strict-valid candidate can auto
        # -execute without a pre-existing supervised track record (that bootstrap
        # is a separate policy, exercised elsewhere).
        paper_auto_min_clean_supervised_lifecycles=0,
        market_universe_symbols=["NVDA"],
    )


def _strict_valid_candidate():
    # No signal_classification/source -> classifies as STRICT_VALID; ready +
    # alert_eligible + backtest_validated + a full bracket + a score above the
    # 65.0 auto-exec floor.
    return SimpleNamespace(
        symbol="NVDA",
        strategy_name="momentum_breakout",
        execution_ready=True,
        signal_role="entry_long",
        score=95.0,
        stop_loss=95.0,
        take_profit=110.0,
        metadata={"alert_eligible": True, "backtest_validated": True},
    )


def _clean_paper_auto_service(settings, *, run_logs, notifier, proposals, execution):
    return PaperAutoTradingService(
        settings=settings,
        proposal_service=proposals,
        execution_coordinator=execution,
        automation=SimpleNamespace(execution_blockers=lambda: []),
        reconciliation=SimpleNamespace(account_verified=lambda: True),
        safety_state=SimpleNamespace(
            is_blacklisted=lambda _symbol: False,
            strategy_active=lambda _strategy: True,
        ),
        executions=None,
        run_logs=run_logs,
        notifier=notifier,
        alpaca_client=SimpleNamespace(
            is_regular_market_open=lambda: True,
            is_supported_equity=lambda _symbol: True,
        ),
    )


def test_fully_validated_candidate_has_no_blockers(tmp_path):
    # The Monday-critical invariant: a strict-valid candidate with every gate
    # satisfied must clear candidate_blockers entirely. If this ever regresses,
    # the bot silently stops trading with no error — exactly the failure we spent
    # this session hunting — so pin the whole clean path.
    service = _clean_paper_auto_service(
        _clean_paper_auto_settings(tmp_path),
        run_logs=SimpleNamespace(log=lambda *a, **k: None),
        notifier=SimpleNamespace(send_text=lambda *a, **k: None),
        proposals=None,
        execution=None,
    )

    assert service.candidate_blockers(_strict_valid_candidate()) == []


def test_approve_enqueue_execute_executes_clean_candidate(tmp_path):
    # The green path end to end: no blockers -> approve -> enqueue -> process ->
    # a processed execution is returned, the funnel tallies one execution (and
    # zero blocks), and the processed event is logged.
    logged: list[tuple[str, dict]] = []
    approved = SimpleNamespace(id="prop_1")
    queued = SimpleNamespace(id="queue_1")
    processed = SimpleNamespace(
        id="queue_1", symbol="NVDA", status="submitted", validation_reason=None
    )
    approve_calls: list[str] = []
    proposals = SimpleNamespace(
        approve_proposal=lambda pid, req: approve_calls.append(pid) or approved
    )
    execution = SimpleNamespace(
        enqueue_approved_proposal=lambda aid: queued,
        process_queue_item=lambda qid: processed,
    )
    service = _clean_paper_auto_service(
        _clean_paper_auto_settings(tmp_path),
        run_logs=SimpleNamespace(log=lambda event, payload: logged.append((event, payload))),
        notifier=SimpleNamespace(send_text=lambda *a, **k: None),
        proposals=proposals,
        execution=execution,
    )
    proposal = SimpleNamespace(id="prop_1", order=SimpleNamespace(symbol="NVDA"))
    funnel: Counter[str] = Counter()

    result = service.approve_enqueue_execute(proposal, _strict_valid_candidate(), funnel=funnel)

    assert result is processed
    assert result.status == "submitted"
    assert approve_calls == ["prop_1"]  # the proposal was actually approved
    assert funnel["executed"] == 1
    assert funnel["exec_blocked_candidates"] == 0
    assert any(event == "paper_auto_execution_processed" for event, _ in logged)
