"""Where one ``workflow_cadence`` tick spent its seconds: one log line per tick (2026-11-25).

On 11-25 the intraday scan ran once in 3 hours: almost every tick reached it after its admission
window had closed. The schedule endpoints showed maintenance taking 64-68 s on a quiet tick, not
which of its steps did, because the per-step events go to the run-log table only. This writes
one line to the process log per tick, for example::

    cadence_tick total=152.3s deferred=intraday_rotation steps=flatten@0.0+0.1
    maintenance@0.4+66.2 >alpaca_reconciliation@0.9+12.1 swing_hourly@67.0+56.1

``name@start+seconds``, in start order; ``>`` marks a step inside the one before it at the
level above. Timing only: nothing reads these numbers back, a step's result and exceptions pass
through unchanged, and outside a tick (a manual run, a test) ``step`` is a plain call. Steps are
timed on ``time.perf_counter``, apart from the budget's ``time.monotonic`` clock.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable
from contextlib import suppress
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

_local = threading.local()
_now = time.perf_counter


class _Trace:
    def __init__(self) -> None:
        self.started = _now()
        self.steps: list[list[Any]] = []  # [depth, name, start offset, seconds or None]
        self.depth = 0


def start_tick(started_at: float) -> float:
    """Begin this thread's trace; returns ``started_at`` (the tick's budget clock) unchanged."""

    _local.trace = _Trace()
    return started_at


def step(name: str, func: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    """Call ``func`` and, inside a tick, note how long it took."""

    trace: _Trace | None = getattr(_local, "trace", None)
    if trace is None:
        return func(*args, **kwargs)
    began = _now()
    entry: list[Any] = [trace.depth, name, began - trace.started, None]
    trace.steps.append(entry)
    trace.depth += 1
    try:
        return func(*args, **kwargs)
    finally:
        trace.depth -= 1
        entry[3] = _now() - began


def describe(trace: _Trace, deferred: Iterable[str] = ()) -> str:
    parts = [
        f"{'>' * depth}{name}@{start:.1f}+{seconds if seconds is not None else 0.0:.1f}"
        for depth, name, start, seconds in trace.steps
    ]
    return (
        f"cadence_tick total={_now() - trace.started:.1f}s "
        f"deferred={','.join(deferred) or '-'} steps={' '.join(parts) or '-'}"
    )


def end_tick(summary: T, deferred: Iterable[str] = ()) -> T:
    """Log the tick's line and return ``summary`` unchanged; never raises."""

    trace: _Trace | None = getattr(_local, "trace", None)
    _local.trace = None
    if trace is not None:
        with suppress(Exception):  # a log line never fails the tick
            logger.info("%s", describe(trace, deferred))
    return summary
