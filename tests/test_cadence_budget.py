"""Per-bucket cadence budget (timeouts review 2026-10-09): admission windows, scan stop
and thread-local exposure."""

from __future__ import annotations

import threading

from app.workflow import cadence_budget as cb
from tests.conftest import make_settings


def _clock(monkeypatch, start=0.0):
    now = {"t": start}
    monkeypatch.setattr(cb.time, "monotonic", lambda: now["t"])
    return now


def test_admission_windows_follow_bucket_cost(tmp_path, monkeypatch) -> None:
    now = _clock(monkeypatch)
    on = make_settings(tmp_path, intraday_active_mover_shortlist_enabled=True)
    off = make_settings(tmp_path / "off", intraday_active_mover_shortlist_enabled=False)
    budget = cb.CadenceBudget.for_tick(on, 0.0)
    for t, swing, intraday_on, intraday_off in [
        (72.0, True, True, True),  # the intraday limit until 11-25, with the 45 s minimum scan
        (81.0, True, True, True),  # where the quiet ticks of 11-25 reached the scans (78-81 s)
        (97.0, True, True, True),  # 165 s scan stop - 48 s pre-work - the 20 s intraday minimum
        (97.1, True, False, True),
        (107.0, True, False, True),  # swing: 165 - 13 - 45 (122 s until the reserve went 60 -> 75)
        (107.1, False, False, True),
        (132.0, False, False, True),  # intraday without the shortlist refresh: 165 - 13 - 20
        (132.1, False, False, False),
    ]:
        now["t"] = t
        assert budget.admits("swing_hourly", on) is swing, t
        assert budget.admits("intraday_rotation", on) is intraday_on, t
        assert budget.admits("intraday_rotation", off) is intraday_off, t


def test_explicit_soft_budget_and_disable(tmp_path, monkeypatch) -> None:
    now = _clock(monkeypatch)
    settings = make_settings(tmp_path, scheduler_cadence_soft_budget_seconds=100.0)
    legacy = cb.CadenceBudget.for_tick(settings, 0.0)
    now["t"] = 99.9
    assert legacy.admits("intraday_rotation", settings)
    now["t"] = 100.0
    assert not legacy.admits("swing_hourly", settings)
    disabled = make_settings(tmp_path / "z", scheduler_cadence_soft_budget_seconds=0.0)
    assert cb.CadenceBudget.for_tick(disabled, 0.0) is None


def test_scan_stop_reserves_post_scan_time(tmp_path, monkeypatch) -> None:
    now = _clock(monkeypatch)
    settings = make_settings(tmp_path)
    budget = cb.CadenceBudget.for_tick(settings, 0.0)
    assert budget.scan_stop_at == 165.0
    deadline = cb.ScanDeadline(budget.scan_stop_at)
    now["t"] = 164.9
    assert not deadline.is_set()
    now["t"] = 165.0
    assert deadline.is_set()
    assert cb.POST_SCAN_RESERVE_SECONDS >= 10 + 41 + 5
    assert settings.scheduler_job_timeout_seconds < settings.scheduler_self_heal_stale_seconds


def test_budget_is_thread_local(tmp_path) -> None:
    budget = cb.CadenceBudget.for_tick(make_settings(tmp_path), 0.0)
    seen: list[object] = []
    with cb.tick_budget(budget):
        assert cb.current_budget() is budget
        worker = threading.Thread(target=lambda: seen.append(cb.current_budget()))
        worker.start()
        worker.join()
    assert seen == [None]  # another thread (manual/API scan) sees no budget
    assert cb.current_budget() is None  # restored after the block


def test_late_maintenance_steps_start_only_if_they_fit(tmp_path, monkeypatch) -> None:
    """11-04..11-10: maintenance ticks ran the open-signal check and the daily summary after
    ~130 s of ledger cycle and reconciliation and overran the 240 s cap."""

    now = _clock(monkeypatch)
    settings = make_settings(tmp_path)
    assert settings.scheduler_job_timeout_seconds == 240
    now["t"] = 160.0  # 160 + 70 + 10 = 240: the open-signal check still fits
    assert cb.late_step_fits(settings, 0.0, cb.OPEN_SIGNAL_CHECK_SECONDS)
    now["t"] = 160.5
    assert not cb.late_step_fits(settings, 0.0, cb.OPEN_SIGNAL_CHECK_SECONDS)
    assert cb.late_step_fits(settings, 0.0, cb.DAILY_SUMMARY_SECONDS)  # 160.5 + 60 + 10 <= 240
    now["t"] = 171.0
    assert not cb.late_step_fits(settings, 0.0, cb.DAILY_SUMMARY_SECONDS)
    assert cb.late_step_fits(settings, None, cb.DAILY_SUMMARY_SECONDS)  # a manual run: no tick cap


def test_a_late_step_that_would_overrun_is_logged_and_stays_due(tmp_path, monkeypatch) -> None:
    now = _clock(monkeypatch)
    logged: list[tuple[str, dict]] = []
    service = type("S", (), {})()
    service.settings = make_settings(tmp_path)
    service.run_logs = type("L", (), {"log": lambda self, event, payload: logged.append((event, payload))})()
    service._tick_started_at = 0.0
    now["t"] = 100.0
    assert cb.CadenceBudget.late_step_runs(service, "open_signal_check") and logged == []
    now["t"] = 175.0
    assert not cb.CadenceBudget.late_step_runs(service, "daily_summary")
    assert logged == [("maintenance_step_deferred", {"step": "daily_summary"})]
    del service._tick_started_at  # a manual maintenance run: no tick, no deferral
    assert cb.CadenceBudget.late_step_runs(service, "open_signal_check")


def test_the_intraday_minimum_is_shorter_and_the_backstop_agrees(tmp_path, monkeypatch) -> None:
    """11-25: the intraday scan ran once in 3 hours (72 s limit, reached at 78-81 s). Only its minimum
    scan changed: the scan stop and the post-scan reserve are where they were."""

    now = _clock(monkeypatch)
    settings = make_settings(tmp_path, intraday_active_mover_shortlist_enabled=True)
    budget = cb.CadenceBudget.for_tick(settings, 0.0)
    assert budget.scan_stop_at == 165.0 and cb.POST_SCAN_RESERVE_SECONDS == 75.0
    assert cb.min_scan_seconds("intraday_rotation") == cb.INTRADAY_MIN_SCAN_SECONDS == 20.0
    assert cb.min_scan_seconds("intraday_scan") == 20.0  # the task name run_scan_task passes
    for name in ("swing_hourly", "swing_scan", "premarket_scan", "crypto_rotation", None):
        assert cb.min_scan_seconds(name) == cb.MIN_USEFUL_SCAN_SECONDS == 45.0
    # Admitted at the limit, then the shortlist refresh and the guard at their budgeted cost:
    # the backstop in run_scan_task still passes it (and not a second later).
    at_backstop = 97.0 + cb.INTRADAY_PRESTART_SECONDS + cb.GUARD_SETUP_SECONDS
    now["t"] = at_backstop
    assert budget.scan_fits("intraday_scan")
    assert not budget.scan_fits("swing_scan") and not budget.scan_fits()
    now["t"] = at_backstop + 0.1
    assert not budget.scan_fits("intraday_scan")
    now["t"] = 110.0  # every other task keeps the 45 s minimum
    assert budget.scan_fits("swing_scan") and budget.scan_fits()
    now["t"] = 110.1
    assert not budget.scan_fits("swing_scan") and not budget.scan_fits()
    legacy = cb.CadenceBudget.for_tick(
        make_settings(tmp_path / "legacy", scheduler_cadence_soft_budget_seconds=100.0), 0.0
    )
    now["t"] = 500.0
    assert legacy.scan_fits("intraday_scan") and legacy.scan_fits()  # legacy mode: no backstop
