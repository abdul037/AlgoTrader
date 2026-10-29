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
        (87.0, True, True, True),
        (87.1, True, False, True),
        (122.0, True, False, True),
        (122.1, False, False, False),
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
    assert budget.scan_stop_at == 180.0
    deadline = cb.ScanDeadline(budget.scan_stop_at)
    now["t"] = 179.9
    assert not deadline.is_set()
    now["t"] = 180.0
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
