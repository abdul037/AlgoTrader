from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.automation.scheduler_worker import (
    HEARTBEAT_KEY,
    LAST_ERROR_KEY,
    TICK_COUNT_KEY,
    ScheduledJob,
    SchedulerWorker,
)


class FakeRuntimeState:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value


class Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def _worker(jobs, clock, state=None):
    return SchedulerWorker(jobs, runtime_state=state, clock=clock)


def test_first_run_executes_all_jobs_and_heartbeats() -> None:
    clock = Clock(datetime(2026, 1, 1, tzinfo=UTC))
    state = FakeRuntimeState()
    calls: list[str] = []
    jobs = [
        ScheduledJob("a", 60, lambda: calls.append("a")),
        ScheduledJob("b", 120, lambda: calls.append("b")),
    ]
    worker = _worker(jobs, clock, state)

    ran = worker.run_due_jobs()

    assert ran == ["a", "b"]  # both due on first tick (last_run is None)
    assert calls == ["a", "b"]
    assert state.get(HEARTBEAT_KEY) == clock.now.isoformat()
    assert state.get(TICK_COUNT_KEY) == "1"


def test_heartbeat_refreshes_between_jobs_in_a_long_tick() -> None:
    # A tick that runs several jobs back-to-back must refresh the heartbeat
    # between them, so a long-but-healthy job can't age the heartbeat past the
    # staleness window and trigger a spurious self-heal restart.
    clock = Clock(datetime(2026, 1, 1, tzinfo=UTC))
    state = FakeRuntimeState()
    seen: dict[str, str | None] = {}

    def slow_a() -> None:
        clock.advance(200)  # a long-running job

    def read_b() -> None:
        # Heartbeat as observed just before job "b" runs its body.
        seen["hb_before_b"] = state.get(HEARTBEAT_KEY)

    jobs = [
        ScheduledJob("a", 60, slow_a),
        ScheduledJob("b", 60, read_b),
    ]
    worker = _worker(jobs, clock, state)

    worker.run_due_jobs()

    # Without the per-job heartbeat the value here would be the stale tick-start
    # time (or None); with it, the heartbeat already reflects the advanced clock.
    assert seen["hb_before_b"] == clock.now.isoformat()


def test_jobs_respect_independent_cadences() -> None:
    clock = Clock(datetime(2026, 1, 1, tzinfo=UTC))
    calls: list[str] = []
    jobs = [
        ScheduledJob("fast", 60, lambda: calls.append("fast")),
        ScheduledJob("slow", 120, lambda: calls.append("slow")),
    ]
    worker = _worker(jobs, clock)

    worker.run_due_jobs()  # both run
    calls.clear()

    clock.advance(60)
    ran = worker.run_due_jobs()
    assert ran == ["fast"]  # only fast is due at +60s
    assert calls == ["fast"]

    calls.clear()
    clock.advance(60)  # now at +120s from first run
    ran = worker.run_due_jobs()
    assert set(ran) == {"fast", "slow"}


def test_one_failing_job_does_not_stop_others_or_heartbeat() -> None:
    clock = Clock(datetime(2026, 1, 1, tzinfo=UTC))
    state = FakeRuntimeState()
    calls: list[str] = []

    def boom() -> None:
        raise RuntimeError("kaboom")

    jobs = [
        ScheduledJob("bad", 60, boom),
        ScheduledJob("good", 60, lambda: calls.append("good")),
    ]
    worker = _worker(jobs, clock, state)

    ran = worker.run_due_jobs()

    assert ran == ["bad", "good"]  # both attempted
    assert calls == ["good"]  # good still ran after bad raised
    assert state.get(HEARTBEAT_KEY) == clock.now.isoformat()  # heartbeat still advanced
    assert "kaboom" in (state.get(LAST_ERROR_KEY) or "")
    status = worker.status()
    bad = next(j for j in status["jobs"] if j["name"] == "bad")
    good = next(j for j in status["jobs"] if j["name"] == "good")
    assert "kaboom" in (bad["last_error"] or "")
    assert good["last_error"] is None


def test_job_timeout_abandons_blocking_job_and_keeps_ticking() -> None:
    import threading

    clock = Clock(datetime(2026, 1, 1, tzinfo=UTC))
    state = FakeRuntimeState()
    release = threading.Event()
    calls: list[str] = []

    def hang() -> None:
        release.wait(2)  # bounded so the abandoned thread cannot wedge the test

    jobs = [
        ScheduledJob("slow", 60, hang, timeout_seconds=0.3),
        ScheduledJob("fast", 60, lambda: calls.append("fast")),
    ]
    worker = SchedulerWorker(jobs, runtime_state=state, clock=clock)

    ran = worker.run_due_jobs()

    assert ran == ["slow", "fast"]  # both attempted despite the slow one blocking
    assert calls == ["fast"]  # the fast job still ran after the slow one timed out
    assert state.get(HEARTBEAT_KEY) == clock.now.isoformat()  # heartbeat still advanced
    status = worker.status()
    slow = next(j for j in status["jobs"] if j["name"] == "slow")
    assert "timeout" in (slow["last_error"] or "").lower()
    release.set()


def test_status_reports_heartbeat_age() -> None:
    clock = Clock(datetime(2026, 1, 1, tzinfo=UTC))
    worker = _worker([ScheduledJob("a", 60, lambda: None)], clock)
    worker.run_due_jobs()
    clock.advance(45)
    status = worker.status()
    assert status["heartbeat_age_seconds"] == 45
    assert status["running"] is False  # thread never started
    assert status["tick_count"] == 1


def test_ensure_alive_noop_when_never_started() -> None:
    worker = _worker([ScheduledJob("a", 60, lambda: None)], Clock(datetime(2026, 1, 1, tzinfo=UTC)))
    assert worker.ensure_alive() is False  # no thread to revive


def test_ensure_alive_noop_when_stopped() -> None:
    worker = SchedulerWorker([ScheduledJob("a", 0.05, lambda: None)], tick_interval_seconds=1)
    worker.start()
    worker.stop()
    assert worker.ensure_alive() is False  # intentionally stopped, do not revive


def test_ensure_alive_restarts_dead_thread() -> None:
    import threading

    worker = SchedulerWorker(
        [ScheduledJob("a", 0.05, lambda: None)], tick_interval_seconds=1, self_heal_enabled=False
    )
    worker.start()
    # Simulate the loop thread having died without an intentional stop.
    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join()
    worker._thread = dead
    worker._stop_event.clear()

    assert worker.ensure_alive() is True
    assert worker._thread is not dead
    assert worker.status()["restart_count"] == 1
    assert worker.status()["last_restart_reason"] == "thread_died"
    worker.stop()


def test_self_heal_restarts_hung_worker_on_stale_heartbeat() -> None:
    import threading

    clock = Clock(datetime(2026, 1, 1, tzinfo=UTC))
    started = threading.Event()
    release = threading.Event()

    def hang() -> None:
        started.set()
        release.wait(2)  # bounded so an orphaned thread can never wedge the test

    worker = SchedulerWorker(
        [ScheduledJob("hang", 0.01, hang)],
        tick_interval_seconds=1,
        clock=clock,
        self_heal_enabled=True,
        stale_restart_seconds=100,
        monitor_interval_seconds=1000,  # exercise ensure_alive() directly, not the monitor
    )
    worker.start()
    assert started.wait(2)  # the tick is now blocked inside the job -> no more heartbeats

    # A healthy live thread is left alone...
    assert worker.ensure_alive() is False
    # ...but once the beat is stale past the threshold, the hung thread is replaced.
    clock.advance(200)
    assert worker.ensure_alive() is True
    assert worker.status()["restart_count"] == 1
    assert worker.status()["last_restart_reason"] == "hung_heartbeat_stale"

    release.set()
    worker.stop()


def test_self_heal_disabled_leaves_hung_thread_untouched() -> None:
    import threading

    clock = Clock(datetime(2026, 1, 1, tzinfo=UTC))
    started = threading.Event()
    release = threading.Event()

    def hang() -> None:
        started.set()
        release.wait(2)

    worker = SchedulerWorker(
        [ScheduledJob("hang", 0.01, hang)],
        tick_interval_seconds=1,
        clock=clock,
        self_heal_enabled=False,
    )
    worker.start()
    assert started.wait(2)
    assert worker._monitor_thread is None  # no monitor when self-heal is off

    clock.advance(10_000)
    assert worker.ensure_alive() is False  # hung thread is NOT restarted when disabled
    assert worker.status()["restart_count"] == 0

    release.set()
    worker.stop()


def test_monitor_thread_runs_only_when_self_heal_enabled() -> None:
    on = SchedulerWorker(
        [ScheduledJob("a", 1, lambda: None)], tick_interval_seconds=1, self_heal_enabled=True
    )
    on.start()
    assert on._monitor_thread is not None and on._monitor_thread.is_alive()
    on.stop()
    assert not on._monitor_thread.is_alive()

    off = SchedulerWorker(
        [ScheduledJob("a", 1, lambda: None)], tick_interval_seconds=1, self_heal_enabled=False
    )
    off.start()
    assert off._monitor_thread is None
    off.stop()
