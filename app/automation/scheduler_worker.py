"""Dedicated background scheduler worker.

Owns the autonomous cadence (scans, proposals, queue processing, reconciliation,
paper position refresh) in a single supervised loop that is decoupled from the
Telegram polling loop. Each registered job runs on its own independent cadence,
one failing job never stops the others or the loop, and a liveness heartbeat is
persisted after every tick so the readiness probe can detect a stalled or dead
worker.

This replaces the previous ``TelegramAlertScheduler`` daemon thread, which
piggybacked the whole cadence on the Telegram alert loop, swallowed exceptions
with no liveness signal, and did not start at all when Telegram ran in polling
mode.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from threading import Event, Thread
from typing import Any

from app.utils.time import utc_now

logger = logging.getLogger(__name__)

# runtime_state keys used for the cross-process liveness signal.
HEARTBEAT_KEY = "scheduler_worker:heartbeat_at"
STARTED_KEY = "scheduler_worker:started_at"
TICK_COUNT_KEY = "scheduler_worker:tick_count"
LAST_ERROR_KEY = "scheduler_worker:last_error"
LAST_ERROR_AT_KEY = "scheduler_worker:last_error_at"
RESTART_COUNT_KEY = "scheduler_worker:restart_count"


@dataclass
class ScheduledJob:
    """A single periodic job with its own independent cadence."""

    name: str
    interval_seconds: float
    func: Callable[[], Any]
    last_run_at: datetime | None = field(default=None, init=False)
    last_error: str | None = field(default=None, init=False)
    run_count: int = field(default=0, init=False)

    def is_due(self, now: datetime) -> bool:
        if self.last_run_at is None:
            return True
        return (now - self.last_run_at).total_seconds() >= self.interval_seconds


class SchedulerWorker:
    """Run a set of :class:`ScheduledJob` on a supervised background thread."""

    def __init__(
        self,
        jobs: Iterable[ScheduledJob],
        *,
        runtime_state: Any | None = None,
        run_logs: Any | None = None,
        tick_interval_seconds: float = 5.0,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.jobs: list[ScheduledJob] = list(jobs)
        self.runtime_state = runtime_state
        self.run_logs = run_logs
        self.tick_interval_seconds = max(float(tick_interval_seconds), 1.0)
        self._clock = clock
        self._stop_event = Event()
        self._thread: Thread | None = None
        self._tick_count = 0
        self._last_heartbeat_at: datetime | None = None
        self._last_error: str | None = None
        self._last_error_at: datetime | None = None
        self._restart_count = 0

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Start the supervised loop once (idempotent)."""

        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        now = self._clock()
        self._persist(STARTED_KEY, now.isoformat())
        self._thread = Thread(target=self._run, name="scheduler-worker", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signal the loop to stop and wait briefly for it to exit."""

        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self.tick_interval_seconds + 2)

    def ensure_alive(self) -> bool:
        """Restart the loop if its thread died unexpectedly. Returns True if restarted.

        A watchdog for unattended operation: called on each readiness probe, it
        revives a worker whose thread crashed out of the loop. It does not start
        a worker that was never started or one intentionally stopped -- a hung
        (alive but not heart-beating) thread is left for the readiness probe to
        surface as not-ready so the orchestrator can restart the process.
        """

        if self._stop_event.is_set() or self._thread is None:
            return False
        if self._thread.is_alive():
            return False
        self._restart_count += 1
        self._persist(RESTART_COUNT_KEY, str(self._restart_count))
        if self.run_logs is not None:
            try:
                self.run_logs.log(
                    "scheduler_worker_restarted", {"restart_count": self._restart_count}
                )
            except Exception:  # noqa: BLE001 - logging must never block the restart
                logger.debug("scheduler worker could not log restart", exc_info=True)
        logger.warning("scheduler worker thread died; restarting (#%d)", self._restart_count)
        self._thread = Thread(target=self._run, name="scheduler-worker", daemon=True)
        self._thread.start()
        return True

    # -- execution -----------------------------------------------------------

    def run_due_jobs(self, now: datetime | None = None) -> list[str]:
        """Run every job whose cadence is due, once. Returns the jobs that ran.

        Exposed separately from the thread loop so it can be driven directly in
        tests and by an external cron entrypoint. A job that raises is recorded
        and skipped; it never stops the other jobs or the loop.
        """

        now = now or self._clock()
        ran: list[str] = []
        for job in self.jobs:
            if not job.is_due(now):
                continue
            ran.append(job.name)
            try:
                job.func()
                job.last_error = None
            except Exception as exc:  # noqa: BLE001 - isolation is the whole point
                job.last_error = f"{type(exc).__name__}: {exc}"
                self._record_error(job.name, exc)
                logger.exception("scheduler job %r failed: %s", job.name, exc)
            finally:
                job.last_run_at = now
                job.run_count += 1
        self._heartbeat(now)
        return ran

    def _run(self) -> None:
        # Emit an immediate heartbeat so readiness reflects a live worker at once.
        self._heartbeat(self._clock())
        while not self._stop_event.is_set():
            try:
                self.run_due_jobs()
            except Exception as exc:  # noqa: BLE001 - never let the loop die
                self._record_error("scheduler_worker", exc)
                logger.exception("scheduler worker tick failed: %s", exc)
            self._stop_event.wait(self.tick_interval_seconds)

    # -- heartbeat / status --------------------------------------------------

    def _heartbeat(self, now: datetime) -> None:
        self._tick_count += 1
        self._last_heartbeat_at = now
        self._persist(HEARTBEAT_KEY, now.isoformat())
        self._persist(TICK_COUNT_KEY, str(self._tick_count))

    def _record_error(self, source: str, exc: Exception) -> None:
        now = self._clock()
        self._last_error = f"{source}: {type(exc).__name__}: {exc}"
        self._last_error_at = now
        self._persist(LAST_ERROR_KEY, self._last_error)
        self._persist(LAST_ERROR_AT_KEY, now.isoformat())
        if self.run_logs is not None:
            try:
                self.run_logs.log(
                    "scheduler_worker_error", {"source": source, "error": str(exc)}
                )
            except Exception:  # noqa: BLE001 - logging must never break the loop
                logger.debug("scheduler worker could not record run log", exc_info=True)

    def _persist(self, key: str, value: str) -> None:
        if self.runtime_state is None:
            return
        try:
            self.runtime_state.set(key, value)
        except Exception:  # noqa: BLE001 - persistence must never break the loop
            logger.debug("scheduler worker could not persist %s", key, exc_info=True)

    def status(self) -> dict[str, Any]:
        """Return an in-memory snapshot of worker liveness and per-job state."""

        now = self._clock()
        last = self._last_heartbeat_at
        age = None if last is None else (now - last).total_seconds()
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "tick_count": self._tick_count,
            "restart_count": self._restart_count,
            "last_heartbeat_at": last.isoformat() if last else None,
            "heartbeat_age_seconds": age,
            "last_error": self._last_error,
            "last_error_at": self._last_error_at.isoformat() if self._last_error_at else None,
            "jobs": [
                {
                    "name": job.name,
                    "interval_seconds": job.interval_seconds,
                    "run_count": job.run_count,
                    "last_run_at": job.last_run_at.isoformat() if job.last_run_at else None,
                    "last_error": job.last_error,
                }
                for job in self.jobs
            ],
        }
