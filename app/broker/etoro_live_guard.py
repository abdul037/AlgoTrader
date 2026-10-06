"""eToro LIVE guard thread (operator request 2026-10-04: "build the separate backup-stop job").

The backup stop and the one-off test order first ran inside the maintenance job.
The scheduler runs its jobs one after another, maintenance often hits its 240 s
limit and a backtest pass takes about 3 minutes, so a stop check could wait
several minutes or be skipped when a run was cut off. This guard runs them on its
own daemon thread every ``etoro_live_guard_interval_seconds`` (default 60 s),
independent of the scheduler. With nothing open it makes no eToro call.

It writes a heartbeat; if that goes stale (thread dead or stuck), the maintenance
hook runs the same checks as a fallback. Every live-eToro state change shares the
mirror's lock, so the guard, entries and reconcile never interleave.
"""

from __future__ import annotations

import logging
from contextlib import suppress
from datetime import datetime, timedelta
from threading import Event, Thread
from typing import Any

from app.broker.etoro_live_backup_stop import check_backup_stops
from app.broker.etoro_live_exit_copy import copy_paper_exits, paper_reader
from app.broker.etoro_live_scorecard import update_scorecard
from app.utils.time import utc_now

logger = logging.getLogger(__name__)

HEARTBEAT_KEY = "etoro_live_guard:heartbeat_at"
STALE_AFTER_SECONDS = 300
INITIAL_DELAY_SECONDS = 30.0
STOP_JOIN_SECONDS = 5.0


class EtoroLiveGuard:
    """Run the eToro live test-order watch and backup stops on a fixed short cadence."""

    def __init__(
        self,
        *,
        mirror: Any,
        tester: Any | None,
        interval_seconds: float = 60.0,
        initial_delay_seconds: float = INITIAL_DELAY_SECONDS,
    ):
        self.mirror = mirror
        self.tester = tester
        self.interval_seconds = max(float(interval_seconds or 60.0), 10.0)
        self.initial_delay_seconds = max(float(initial_delay_seconds), 0.0)
        self._stop = Event()
        self._thread: Thread | None = None

    def tick(self) -> list[str]:
        """One pass: watch the test order, then the backup stops. Never raises."""

        done = run_live_checks(self.mirror, self.tester)
        with suppress(Exception):
            self.mirror.state.set(HEARTBEAT_KEY, utc_now().isoformat())
        return done

    def recently_alive(self) -> bool:
        """True while the guard's heartbeat is fresh (the maintenance fallback then stays idle)."""

        try:
            last = datetime.fromisoformat(str(self.mirror.state.get(HEARTBEAT_KEY) or ""))
        except (TypeError, ValueError):
            return False
        return utc_now() - last < timedelta(seconds=STALE_AFTER_SECONDS)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = Thread(target=self._loop, name="etoro-live-guard", daemon=True)
        self._thread.start()
        logger.info("eToro live guard started (every %.0fs)", self.interval_seconds)

    def stop(self) -> None:
        """Stop and wait briefly for a tick in progress (Railway drains for 15 s)."""

        self._stop.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=STOP_JOIN_SECONDS)

    def _loop(self) -> None:
        # Wait out the deploy overlap (~20 s) so the old container's guard has stopped.
        if self._stop.wait(self.initial_delay_seconds):
            return
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self.interval_seconds)


def run_live_checks(mirror: Any, tester: Any | None) -> list[str]:
    """The test-order watch, the backup stops, the paper-exit copy and the live scorecard,
    each isolated so one failing can't skip the others. Shared by the guard thread and the maintenance fallback."""

    done: list[str] = []
    closes: list[dict] = []

    def backup_stops() -> bool:
        closes.extend(check_backup_stops(mirror) or [])
        return bool(closes)

    for name, step in (
        ("etoro_live_test_order", lambda: tester is not None and tester.run() is not None),
        ("etoro_live_backup_stop", backup_stops),
        ("etoro_live_exit_copy", lambda: bool(copy_paper_exits(mirror, _symbols(closes)))),
        ("etoro_live_scorecard", lambda: update_scorecard(mirror, closes) > 0),
    ):
        try:
            if step():
                done.append(name)
        except Exception as exc:  # noqa: BLE001 - the other step and the heartbeat still run
            logger.exception("eToro live check %s failed: %s", name, exc)
            with suppress(Exception):
                mirror.logs.log("etoro_live_guard_error", {"step": name, "error": str(exc)})
    return done


def _symbols(closes: list[dict]) -> set[str]:
    return {str(c.get("symbol") or "").upper() for c in closes}


def attach_live_guard(
    coordinator: Any, *, settings: Any, bars: Any | None, paper: Any | None = None
) -> None:
    """Wire the test order and the guard onto the execution coordinator (started at boot).
    ``paper`` is the Alpaca client whose positions the exit copy follows."""

    from app.broker.etoro_live_test_order import EtoroLiveTestOrder

    mirror = coordinator.etoro_live_mirror
    if paper is not None and hasattr(paper, "get_portfolio"):
        mirror.paper_reader = paper_reader(paper)
    coordinator.etoro_live_test_order = EtoroLiveTestOrder(mirror=mirror, bars=bars)
    coordinator.etoro_live_guard = EtoroLiveGuard(
        mirror=mirror,
        tester=coordinator.etoro_live_test_order,
        interval_seconds=float(getattr(settings, "etoro_live_guard_interval_seconds", 60) or 60),
    )
