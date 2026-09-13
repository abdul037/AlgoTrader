"""Paper-only self-healing after a circuit-breaker trip.

2026-09-10: a false missing_bracket_protection tripped the breaker pre-market.
The kill switch is persisted runtime state, nothing cleared it, and the
unattended bot logged workflow_scheduler_paused every minute for three
sessions. The scheduler must probe reconciliation while the breaker is tripped
and resume once the broker state is clean — and must never override an
operator pause, KILL_SWITCH_ENABLED, an account mismatch, or real trading.
"""

from __future__ import annotations

from app.automation.auto_recover import RESUME_COUNT_KEY_PREFIX
from app.automation.service import AutomationService
from app.live_signal_schema import MarketQuote
from app.workflow.service import SignalWorkflowService
from tests.conftest import make_settings
from tests.test_workflow_service import (
    FakeAlertHistory,
    FakeLogs,
    FakeMarketDataEngine,
    FakeMarketScreener,
    FakeNotifier,
    FakeState,
    FakeTrackedSignals,
)


class FakeReconciliation:
    def __init__(self, results: list[dict]):
        self.results = list(results)
        self.calls = 0

    def reconcile(self) -> dict:
        self.calls += 1
        if len(self.results) > 1:
            return self.results.pop(0)
        return self.results[0]


def _build(tmp_path, *, recon_results, **overrides):
    settings = make_settings(
        tmp_path,
        screener_scheduler_enabled=False,
        ledger_enabled=False,
        ledger_cycle_enabled=False,
        open_signal_check_interval_minutes=9999,
        **overrides,
    )
    state = FakeState()
    logs = FakeLogs()
    automation = AutomationService(settings=settings, runtime_state=state, run_logs=logs, broker_router=None)
    recon = FakeReconciliation(recon_results)
    workflow = SignalWorkflowService(
        settings=settings,
        market_screener=FakeMarketScreener([]),
        market_data_engine=FakeMarketDataEngine(MarketQuote(symbol="NVDA", last_execution=101.0)),
        notifier=FakeNotifier(),
        tracked_signals=FakeTrackedSignals(),
        alert_history=FakeAlertHistory(),
        runtime_state=state,
        run_logs=logs,
        automation_service=automation,
        reconciliation_service=recon,
    )
    return workflow, automation, recon, state, logs


def _events(logs) -> list[str]:
    return [item[0] for item in logs.items]


CLEAN = {"status": "ok", "issues": []}
DIRTY = {"status": "error", "issues": ["missing_bracket_protection:GOOGL"]}


def test_breaker_trip_is_cleared_and_automation_resumes_when_reconciliation_is_clean(tmp_path) -> None:
    workflow, automation, recon, _state, logs = _build(tmp_path, recon_results=[CLEAN])
    automation.trip_circuit_breaker(reason="missing_bracket_protection:GOOGL", emergency_stop=False)
    assert automation.scan_blockers() == ["automation_kill_switch_enabled", "automation_paused"]

    workflow.run_scheduled_tasks()

    # One probe resumed automation; the maintenance bucket then ran its own
    # regular reconciliation, which is exactly the point of resuming.
    assert recon.calls >= 1
    assert _events(logs).count("automation_auto_resumed") == 1
    status = automation.status()
    assert status.kill_switch_enabled is False
    assert status.paused is False
    assert status.circuit_breaker_reason == ""
    assert status.reason.startswith("auto-recovered: reconciliation clean after missing_bracket_protection:GOOGL")
    assert "automation_auto_resumed" in _events(logs)
    assert "workflow_scheduler_paused" not in _events(logs)


def test_still_dirty_reconciliation_leaves_automation_paused(tmp_path) -> None:
    workflow, automation, recon, _state, logs = _build(tmp_path, recon_results=[DIRTY])
    automation.trip_circuit_breaker(reason="missing_bracket_protection:GOOGL", emergency_stop=False)

    workflow.run_scheduled_tasks()

    assert recon.calls == 1
    assert automation.status().kill_switch_enabled is True
    assert "automation_auto_resumed" not in _events(logs)
    assert logs.items[-1][0] == "workflow_scheduler_paused"
    probe = next(payload for event, payload in logs.items if event == "automation_auto_recover_probe")
    assert probe["outcome"] == "still_blocked"


def test_probes_are_throttled_to_the_configured_interval(tmp_path) -> None:
    workflow, automation, recon, _state, _logs = _build(
        tmp_path, recon_results=[DIRTY], paper_auto_recover_probe_interval_seconds=600
    )
    automation.trip_circuit_breaker(reason="missing_bracket_protection:GOOGL", emergency_stop=False)

    workflow.run_scheduled_tasks()
    workflow.run_scheduled_tasks()
    workflow.run_scheduled_tasks()

    assert recon.calls == 1


def test_manual_pause_is_never_auto_resumed(tmp_path) -> None:
    workflow, automation, recon, _state, logs = _build(tmp_path, recon_results=[CLEAN])
    automation.pause(reason="paused manually")

    workflow.run_scheduled_tasks()

    assert recon.calls == 0
    assert automation.status().paused is True
    assert logs.items[-1][0] == "workflow_scheduler_paused"


def test_manual_kill_switch_is_never_auto_resumed(tmp_path) -> None:
    workflow, automation, recon, _state, _logs = _build(tmp_path, recon_results=[CLEAN])
    automation.enable_kill_switch(reason="operator stop", emergency_stop=False)

    workflow.run_scheduled_tasks()

    assert recon.calls == 0
    assert automation.status().kill_switch_enabled is True


def test_account_mismatch_breaker_is_never_auto_resumed(tmp_path) -> None:
    workflow, automation, recon, _state, _logs = _build(tmp_path, recon_results=[CLEAN])
    automation.trip_circuit_breaker(reason="account_mismatch:expected=A:actual=B", emergency_stop=False)

    workflow.run_scheduled_tasks()

    assert recon.calls == 0
    assert automation.status().kill_switch_enabled is True


def test_real_trading_or_env_kill_switch_disables_auto_recovery(tmp_path) -> None:
    for overrides in ({"enable_real_trading": True}, {"kill_switch_enabled": True}, {"paper_auto_recover_circuit_breaker": False}):
        workflow, automation, recon, _state, _logs = _build(tmp_path, recon_results=[CLEAN], **overrides)
        automation.trip_circuit_breaker(reason="missing_bracket_protection:GOOGL", emergency_stop=False)

        workflow.run_scheduled_tasks()

        assert recon.calls == 0, overrides
        assert automation.status().paused is True, overrides


def test_daily_resume_cap_is_enforced(tmp_path) -> None:
    workflow, automation, recon, state, _logs = _build(
        tmp_path, recon_results=[CLEAN], paper_auto_recover_max_resumes_per_day=1, paper_auto_recover_probe_interval_seconds=0
    )
    automation.trip_circuit_breaker(reason="missing_bracket_protection:GOOGL", emergency_stop=False)
    workflow.run_scheduled_tasks()
    assert automation.status().paused is False
    calls_after_first_resume = recon.calls

    automation.trip_circuit_breaker(reason="missing_bracket_protection:GOOGL", emergency_stop=False)
    workflow.run_scheduled_tasks()

    assert recon.calls == calls_after_first_resume  # cap reached: no second probe
    assert automation.status().paused is True
    assert any(key.startswith(RESUME_COUNT_KEY_PREFIX) and value == "1" for key, value in state.values.items())
