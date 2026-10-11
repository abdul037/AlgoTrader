"""Wall-clock budget for one ``workflow_cadence`` tick (timeouts review 2026-10-09).

The scheduler worker abandons ``run_scheduled_tasks`` after
``scheduler_job_timeout_seconds`` (240 s). The old guard only stopped *starting*
buckets once 110 s had passed, assuming a bucket costs one 120 s batch deadline plus
10 s. Measured 10-07..10-09 a bucket costs more: intraday builds its symbol list (the
active-mover refresh, ~30 s) before its guard, setup takes ~7 s, the scan overshoots
its deadline by up to ~10 s, and alerts, safety blocks and proposal attempts take
4-36 s after it. 45 of the 52 in-session timeouts were buckets admitted under 110 s
that could not finish by 240 s.

A tick now (1) starts a bucket only if its pre-work, a useful scan and the post-scan
reserve still fit before the job cap, and (2) stops the scan core in time for that
reserve, through ``scan_universe``'s existing ``cancel_event`` hook. Which candidates
pass, and how they are sized, approved or executed, does not change: a shorter scan
evaluates fewer symbol/spec pairs. The swing scan's cursors resume where it stopped; the
intraday batch, the crypto scan and the once-a-day premarket / market-open / end-of-day
scans pass explicit lists, so the symbols past the stop are simply not scanned that run
(the intraday offset is put back only when the whole bucket is skipped).

An explicit ``scheduler_cadence_soft_budget_seconds`` > 0 restores the old rule exactly
(admission by elapsed time only, no backstop, no scan stop); <= 0 disables deferral.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

# Scan stop overshoot (<= 10 s) + post-scan alerts/safety blocks/risk-limit rejections
# (up to 41 s measured without an entry) + guard close and the run tail (~5 s), + margin.
POST_SCAN_RESERVE_SECONDS = 60.0
# The bucket guard's lock get/set and bucket-state writes (~3 s), spent before
# run_scan_task; charged at admission only, so admission and the backstop agree.
GUARD_SETUP_SECONDS = 3.0
# run_scan_task to scan core: spec batch, cursors and the weak-valid count (~7 s).
SCAN_SETUP_SECONDS = 10.0
# _intraday_scan_symbols runs before the intraday guard; the active-mover refresh is 28-31 s.
INTRADAY_PRESTART_SECONDS = 35.0
# Below this a scan reaches too few symbol/spec pairs to be worth its fixed costs.
MIN_USEFUL_SCAN_SECONDS = 45.0

_local = threading.local()


@dataclass(frozen=True)
class CadenceBudget:
    started_at: float  # time.monotonic() at entry to run_scheduled_tasks
    job_timeout: float  # the worker's wall-clock cap for the job
    legacy_soft_budget: float | None = None  # explicit scheduler_cadence_soft_budget_seconds

    @classmethod
    def for_tick(cls, settings: Any, started_at: float) -> CadenceBudget | None:
        """None when deferral is disabled (explicit soft budget <= 0)."""

        explicit = getattr(settings, "scheduler_cadence_soft_budget_seconds", None)
        if explicit is not None and float(explicit) <= 0:
            return None
        job_timeout = float(getattr(settings, "scheduler_job_timeout_seconds", 240) or 240)
        return cls(started_at, job_timeout, None if explicit is None else float(explicit))

    @property
    def enforces_scan_stop(self) -> bool:
        """False in the legacy mode (explicit soft budget > 0): then only the old
        'elapsed < N' admission applies -- no backstop and no scan stop (review 10-10)."""

        return self.legacy_soft_budget is None

    @property
    def scan_stop_at(self) -> float:
        return self.started_at + self.job_timeout - POST_SCAN_RESERVE_SECONDS

    def scan_seconds_left(self, prestart: float = 0.0) -> float:
        return self.scan_stop_at - time.monotonic() - prestart

    def admits(self, bucket_name: str, settings: Any) -> bool:
        if self.legacy_soft_budget is not None:
            return (time.monotonic() - self.started_at) < self.legacy_soft_budget
        prestart = GUARD_SETUP_SECONDS + SCAN_SETUP_SECONDS
        if bucket_name == "intraday_rotation" and bool(
            getattr(settings, "intraday_active_mover_shortlist_enabled", False)
        ):
            prestart += INTRADAY_PRESTART_SECONDS
        return self.scan_seconds_left(prestart) >= MIN_USEFUL_SCAN_SECONDS

    def scan_fits(self) -> bool:
        """Backstop in run_scan_task, where the bucket's real pre-work is already spent."""

        if not self.enforces_scan_stop:
            return True
        return self.scan_seconds_left(SCAN_SETUP_SECONDS) >= MIN_USEFUL_SCAN_SECONDS

    def describe(self) -> dict[str, float]:
        return {
            "elapsed_seconds": round(time.monotonic() - self.started_at, 1),
            "scan_stop_in_seconds": round(self.scan_seconds_left(), 1),
        }


class ScanDeadline:
    """A ``cancel_event`` for scan_universe that reads as set once the scan stop passes."""

    def __init__(self, stop_at: float) -> None:
        self.stop_at = stop_at

    def is_set(self) -> bool:
        return time.monotonic() >= self.stop_at


@contextmanager
def tick_budget(budget: CadenceBudget | None) -> Iterator[None]:
    """Expose ``budget`` to run_scan_task on this thread only; manual/API scans see none."""

    previous = getattr(_local, "budget", None)
    _local.budget = budget
    try:
        yield
    finally:
        _local.budget = previous


def current_budget() -> CadenceBudget | None:
    return getattr(_local, "budget", None)


def stale_lock_minutes(settings: Any, task: str) -> int:
    """How old a workflow lock must be before a new run may take it over.

    A deploy or crash kills a run without releasing its lock. The shadow pre-market scan
    has a short window before its 09:20 cutoff, so its lock goes stale one minute after the
    longest a run can be waited for (the job timeout plus the overlap grace: 7 minutes by
    default) instead of the 45-minute default (review 2026-10-10). A run that outlives even
    that cannot clear the newer run's lock (release_workflow_lock), and its saves are
    guarded against overlap."""

    default = max(int(settings.workflow_lock_timeout_minutes), 1)
    if task == "premarket_scan" and bool(getattr(settings, "premarket_deep_scan_enabled", False)):
        waited = float(getattr(settings, "scheduler_job_timeout_seconds", 240) or 240) + max(
            float(getattr(settings, "scheduler_overlap_grace_seconds", 120.0) or 0.0), 0.0
        )
        return min(default, math.ceil(waited / 60.0) + 1)
    return default


def acquire_workflow_lock(runtime_state: Any, settings: Any, task: str, key: str) -> str | None:
    """Take the task's lock unless a live run holds it; the stamp identifies this run."""

    from datetime import datetime, timedelta

    from app.utils.time import utc_now

    current = runtime_state.get(key)
    if current:
        try:
            started_at = datetime.fromisoformat(current)
        except ValueError:
            started_at = None
        if started_at is not None and (utc_now() - started_at) < timedelta(
            minutes=stale_lock_minutes(settings, task)
        ):
            return None
    stamp = utc_now().isoformat()
    runtime_state.set(key, stamp)
    return stamp


def release_workflow_lock(runtime_state: Any, key: str, stamp: str | None) -> None:
    """Clear the lock only while it is still this run's: a run that outlived a stale-lock
    takeover must not clear the newer run's lock (review 2026-10-11)."""

    try:
        if stamp and runtime_state.get(key) not in (stamp, None, ""):
            return
    except Exception:  # noqa: BLE001 - an unreadable lock is cleared, as before
        pass
    runtime_state.set(key, "")
