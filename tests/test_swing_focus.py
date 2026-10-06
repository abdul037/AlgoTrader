"""Evidence-focused swing scan: only passing specs, rotating symbols, a 10-minute cadence."""

from __future__ import annotations

import json

from app.live_signal_schema import MarketQuote
from app.performance.strategy_evidence import VERDICTS_KEY
from app.workflow.service import SignalWorkflowService
from app.workflow.swing_focus import FOCUS_RUNS_KEY, swing_interval_minutes
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

SPECS = ["momentum_breakout:1h", "mean_reversion:1h", "momentum_breakout:1d", "ma_crossover:1d"]


def _workflow(tmp_path, state, **settings):
    values = dict(
        require_strategy_oos_evidence=True,
        screener_spec_batch_mode="rotating",
        screener_spec_batch_size=2,
        screener_spec_coverage_mode="scheduled_all",
        market_universe_limit=25,
    )
    values.update(settings)
    screener = FakeMarketScreener([], spec_keys=SPECS)
    workflow = SignalWorkflowService(
        settings=make_settings(tmp_path, **values),
        market_screener=screener,
        market_data_engine=FakeMarketDataEngine(MarketQuote(symbol="NVDA", last_execution=1.0)),
        notifier=FakeNotifier(),
        tracked_signals=FakeTrackedSignals(),
        alert_history=FakeAlertHistory(),
        runtime_state=state,
        run_logs=FakeLogs(),
    )
    return workflow, screener


def _state_with_verdicts():
    state = FakeState()
    verdicts = {"momentum_breakout:1d": {"passed": True}, "ma_crossover:1d": {"passed": False}}
    state.set(VERDICTS_KEY, json.dumps({"verdicts": verdicts}))
    return state


def test_swing_scan_checks_only_passing_specs(tmp_path) -> None:
    workflow, screener = _workflow(tmp_path, _state_with_verdicts())
    workflow.run_swing_scan(notify=False)
    assert screener.calls[-1]["strategy_spec_keys"] == ["momentum_breakout:1d"]
    assert screener.calls[-1]["timeframes"] == ["1d"]


def test_every_sixth_run_rotates_for_shadow_signals(tmp_path) -> None:
    state = _state_with_verdicts()
    state.set(FOCUS_RUNS_KEY, "5")
    workflow, screener = _workflow(tmp_path, state)
    workflow.run_swing_scan(notify=False)
    assert screener.calls[-1]["strategy_spec_keys"] == ["momentum_breakout:1h", "mean_reversion:1h"]


def test_gate_off_or_no_verdicts_keeps_the_rotation(tmp_path) -> None:
    workflow, screener = _workflow(tmp_path, FakeState())  # no verdicts cached
    workflow.run_swing_scan(notify=False)
    assert screener.calls[-1]["strategy_spec_keys"] == ["momentum_breakout:1h", "mean_reversion:1h"]
    workflow, screener = _workflow(
        tmp_path / "off", _state_with_verdicts(), require_strategy_oos_evidence=False
    )
    workflow.run_swing_scan(notify=False)
    assert screener.calls[-1]["strategy_spec_keys"] != ["momentum_breakout:1d"]


def test_symbol_start_rotates_across_runs(tmp_path) -> None:
    state = _state_with_verdicts()
    workflow, screener = _workflow(tmp_path, state)
    workflow.run_swing_scan(notify=False)  # the fake evaluates 1 symbol per run
    workflow.run_swing_scan(notify=False)
    first, second = screener.calls[-2]["symbols"], screener.calls[-1]["symbols"]
    assert second == first[1:] + first[:1] and sorted(first) == sorted(second)


def test_focused_cadence(tmp_path) -> None:
    assert swing_interval_minutes(make_settings(tmp_path, require_strategy_oos_evidence=True)) == 10
    assert swing_interval_minutes(make_settings(tmp_path)) == 60


def test_a_long_maintenance_run_defers_scans_to_the_next_tick(tmp_path, monkeypatch) -> None:
    # 2026-10-06: the soft budget clock started after maintenance, so a 150 s maintenance
    # run plus a 180 s scan still crossed the 240 s job limit.
    import app.workflow.service as service_module
    from app.models.workflow import WorkflowTaskResponse

    elapsed = {"t": 0.0}
    monkeypatch.setattr(service_module.time, "monotonic", lambda: elapsed["t"])
    workflow, _ = _workflow(
        tmp_path,
        FakeState(),
        screener_scheduler_enabled=True,
        ledger_enabled=False,
        ledger_cycle_enabled=False,
        scheduler_cadence_soft_budget_seconds=50.0,
    )
    monkeypatch.setattr(workflow, "_bucket_due", lambda name: True)

    def slow_maintenance(*, notify=True):
        elapsed["t"] += 150.0
        return WorkflowTaskResponse(task="maintenance", status="ok", detail="")

    ran: list[str] = []
    monkeypatch.setattr(workflow, "run_maintenance", slow_maintenance)
    monkeypatch.setattr(
        workflow,
        "run_bucket",
        lambda name, **kw: (
            ran.append(name) or WorkflowTaskResponse(task=name, status="ok", detail="")
        ),
    )
    workflow.run_scheduled_tasks()
    assert ran == []  # every scan waits for the next tick instead of overrunning the job
