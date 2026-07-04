from __future__ import annotations

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
