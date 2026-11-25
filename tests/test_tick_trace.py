"""One log line per cadence tick (app/workflow/tick_trace.py) and the intraday scan's later
start (app/workflow/cadence_budget.py), both 2026-11-25.

On 11-25 the intraday scan did not run once: it had to start by 72 s into the tick and the quiet
ticks reached it at 78-81 s, after 64-68 s of maintenance whose slow step the schedule endpoints
could not name. The scan may now start up to 97 s in, and every tick logs where its time went.
"""

from __future__ import annotations

import logging
import threading

import pytest

from app.models.workflow import WorkflowTaskResponse
from app.workflow import cadence_budget as cb
from app.workflow import tick_trace
from tests.test_workflow_service import (
    DeadlineAwareScreener,
    FakeLogs,
    FakeState,
    _cadence_workflow,
)

LOGGER = "app.workflow.tick_trace"


@pytest.fixture(autouse=True)
def _no_trace_left_behind():
    tick_trace._local.trace = None
    yield
    tick_trace._local.trace = None


def _clock(monkeypatch):
    clock = {"t": 50.0}
    monkeypatch.setattr(tick_trace, "_now", lambda: clock["t"])
    return clock


def _lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOGGER]


# --- the trace -------------------------------------------------------------------------------


def test_outside_a_tick_a_step_is_a_plain_call() -> None:
    assert tick_trace.step("add", lambda a, *, b: a + b, 2, b=3) == 5
    with pytest.raises(KeyError):
        tick_trace.step("boom", {}.__getitem__, "missing")
    assert getattr(tick_trace._local, "trace", None) is None


def test_a_tick_logs_its_steps_in_order_with_start_and_duration(monkeypatch, caplog) -> None:
    clock = _clock(monkeypatch)

    def takes(seconds, result=None):
        clock["t"] += seconds
        return result

    def maintenance():
        tick_trace.step("alpaca_reconciliation", takes, 12.0)
        tick_trace.step("open_signal_check", takes, 60.25)
        return takes(2.0, "done")

    summary = {"buckets_run": 1}
    assert tick_trace.start_tick(1234.5) == 1234.5  # the budget's clock, passed through
    tick_trace.step("flatten", takes, 0.5)
    assert tick_trace.step("maintenance", maintenance) == "done"
    assert tick_trace.step("swing_hourly", takes, 30.0, result="scan") == "scan"
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert tick_trace.end_tick(summary, ["intraday_rotation"]) is summary
    assert _lines(caplog) == [
        "cadence_tick total=104.8s deferred=intraday_rotation steps=flatten@0.0+0.5 "
        "maintenance@0.5+74.2 >alpaca_reconciliation@0.5+12.0 >open_signal_check@12.5+60.2 "
        "swing_hourly@74.8+30.0"
    ]
    # The tick is over: a later step on this thread is a plain call and nothing more is logged.
    assert tick_trace.step("late", takes, 1.0, result="x") == "x"
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert tick_trace.end_tick("again") == "again"
    assert len(_lines(caplog)) == 1


def test_a_failing_step_is_timed_and_its_exception_passes_through(monkeypatch, caplog) -> None:
    clock = _clock(monkeypatch)

    def fails():
        clock["t"] += 3.0
        raise RuntimeError("broker down")

    tick_trace.start_tick(0.0)
    with pytest.raises(RuntimeError, match="broker down"):
        tick_trace.step("alpaca_reconciliation", fails)
    assert tick_trace.step("next", lambda: "ok") == "ok"  # depth is back where it was
    with caplog.at_level(logging.INFO, logger=LOGGER):
        tick_trace.end_tick(None)
    assert _lines(caplog) == [
        "cadence_tick total=3.0s deferred=- steps=alpaca_reconciliation@0.0+3.0 next@3.0+0.0"
    ]


def test_an_empty_tick_and_a_failed_log_write_change_nothing(monkeypatch, caplog) -> None:
    _clock(monkeypatch)
    tick_trace.start_tick(0.0)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        assert tick_trace.end_tick({"a": 1}) == {"a": 1}
    assert _lines(caplog) == ["cadence_tick total=0.0s deferred=- steps=-"]

    def broken(*_args, **_kwargs):
        raise OSError("log pipe closed")

    monkeypatch.setattr(tick_trace.logger, "info", broken)
    tick_trace.start_tick(0.0)
    assert tick_trace.end_tick("kept") == "kept"  # a log line never fails the tick
    assert tick_trace._local.trace is None


def test_the_trace_belongs_to_the_tick_s_thread(monkeypatch, caplog) -> None:
    _clock(monkeypatch)
    tick_trace.start_tick(0.0)
    worker = threading.Thread(target=lambda: tick_trace.step("manual_scan", lambda: None))
    worker.start()
    worker.join()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        tick_trace.end_tick(None)
    assert _lines(caplog) == ["cadence_tick total=0.0s deferred=- steps=-"]


# --- the workflow tick -----------------------------------------------------------------------


def _tick(tmp_path, monkeypatch, *, maintenance_seconds, due, logs=None, state=None, screener=None):
    """A scheduled tick whose maintenance takes ``maintenance_seconds`` on the budget's clock."""

    now = {"t": 1000.0}
    monkeypatch.setattr(cb.time, "monotonic", lambda: now["t"])
    workflow = _cadence_workflow(
        tmp_path,
        screener=screener or DeadlineAwareScreener([]),
        logs=logs,
        state=state,
        intraday_active_mover_shortlist_enabled=True,
    )

    def maintenance(*, notify=True):
        now["t"] += maintenance_seconds
        return WorkflowTaskResponse(task="maintenance", status="ok", detail="")

    monkeypatch.setattr(workflow, "run_maintenance", maintenance)
    monkeypatch.setattr(workflow, "_bucket_due", lambda name: name in due)
    monkeypatch.setattr(workflow, "_intraday_scan_symbols", lambda: ["NVDA", "AMD"])
    return workflow, now


def test_the_quiet_tick_of_11_25_now_runs_the_intraday_scan(tmp_path, monkeypatch, caplog) -> None:
    logs, screener = FakeLogs(), DeadlineAwareScreener([])
    workflow, _ = _tick(
        tmp_path,
        monkeypatch,
        maintenance_seconds=80.0,  # 78-81 s measured; the limit was 72 s
        due={"maintenance", "intraday_rotation"},
        logs=logs,
        screener=screener,
    )
    with caplog.at_level(logging.INFO, logger=LOGGER):
        summary = workflow.run_scheduled_tasks()
    assert summary["buckets_run"] == 2  # maintenance and the intraday scan
    assert [event for event, _ in logs.items if event == "workflow_cadence_deferred"] == []
    (call,) = screener.calls
    assert call["symbols"] == ["NVDA", "AMD"]
    # The scan core still stops where it did, leaving the same post-scan reserve.
    assert call["cancel_event"].stop_at == 1000.0 + 240.0 - cb.POST_SCAN_RESERVE_SECONDS
    (line,) = _lines(caplog)
    assert line.startswith("cadence_tick total=") and " deferred=- " in line
    steps = line.split("steps=")[1].split()
    assert [s.split("@")[0] for s in steps] == [
        "flatten",
        "max_hold",
        "deferred_exploration",
        "maintenance",
        "intraday_rotation",
        ">intraday_symbols",
        "open_shadow",
    ]


def test_a_tick_past_the_intraday_window_defers_it_and_says_so(
    tmp_path, monkeypatch, caplog
) -> None:
    logs, screener = FakeLogs(), DeadlineAwareScreener([])
    workflow, _ = _tick(
        tmp_path,
        monkeypatch,
        maintenance_seconds=97.5,
        due={"maintenance", "intraday_rotation", "swing_hourly"},
        logs=logs,
        screener=screener,
    )
    with caplog.at_level(logging.INFO, logger=LOGGER):
        workflow.run_scheduled_tasks()
    (deferred,) = [payload for event, payload in logs.items if event == "workflow_cadence_deferred"]
    assert deferred["deferred_buckets"] == ["intraday_rotation"]
    assert len(screener.calls) == 1  # swing (its limit is 107 s) still ran
    (line,) = _lines(caplog)
    assert " deferred=intraday_rotation " in line
    assert "swing_hourly@" in line and "intraday_rotation@" not in line


def test_the_backstop_passes_a_late_intraday_scan_and_still_stops_a_swing_scan(
    tmp_path, monkeypatch
) -> None:
    state, screener = FakeState(), DeadlineAwareScreener([])
    workflow, now = _tick(
        tmp_path, monkeypatch, maintenance_seconds=0.0, due=set(), state=state, screener=screener
    )
    budget = cb.CadenceBudget.for_tick(workflow.settings, 1000.0)
    now["t"] = 1000.0 + 135.0  # admitted at 97 s, then the refresh and the guard: 20 s of scan left
    with cb.tick_budget(budget):
        assert workflow.run_swing_scan(notify=False).status == "skipped"
        assert screener.calls == []
        assert workflow.run_intraday_scan(notify=False).status == "ok"
    assert len(screener.calls) == 1 and state.get("workflow:last_intraday_scan_at")

    state.set("workflow:intraday_scan_offset", "7")
    now["t"] = 1000.0 + 135.1  # past it: skipped, and the batch is offered again next tick
    with cb.tick_budget(budget):
        result = workflow.run_intraday_scan(notify=False)
    assert result.status == "skipped" and len(screener.calls) == 1
    assert state.get("workflow:intraday_scan_offset") == "7"
    skipped = [p for e, p in workflow.run_logs.items if e == "workflow_scan_budget_deferred"]
    assert [p["task"] for p in skipped] == ["swing_scan", "intraday_scan"]


def test_a_paused_tick_ends_its_trace(tmp_path, monkeypatch, caplog) -> None:
    workflow, _ = _tick(tmp_path, monkeypatch, maintenance_seconds=0.0, due={"maintenance"})
    workflow.automation = type(
        "Paused", (), {"scan_blockers": lambda self: ["kill_switch_enabled"]}
    )()
    monkeypatch.setattr("app.workflow.service.try_auto_recover", lambda *_a, **_k: False)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        workflow.run_scheduled_tasks()
    (line,) = _lines(caplog)
    assert " deferred=paused " in line and "maintenance@" not in line
    assert tick_trace._local.trace is None


def test_maintenance_names_its_own_steps_inside_a_tick(tmp_path, monkeypatch, caplog) -> None:
    """The real maintenance run: the steps that were due appear under it, one level in."""

    now = {"t": 0.0}
    monkeypatch.setattr(cb.time, "monotonic", lambda: now["t"])
    workflow = _cadence_workflow(tmp_path)
    workflow.settings.screener_scheduler_enabled = False
    workflow.reconciliation = type("R", (), {"reconcile": lambda self: {"status": "ok"}})()
    monkeypatch.setattr(workflow, "_bucket_due", lambda name: name == "maintenance")
    with caplog.at_level(logging.INFO, logger=LOGGER):
        workflow.run_scheduled_tasks()
    (line,) = _lines(caplog)
    names = [s.split("@")[0] for s in line.split("steps=")[1].split()]
    assert names[:4] == ["flatten", "max_hold", "deferred_exploration", "maintenance"]
    assert ">alpaca_reconciliation" in names and ">open_signal_check" in names
    assert all(name.startswith(">") for name in names[4:])  # scans are off: nothing after it
