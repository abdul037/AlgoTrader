"""Paper exploration entries a scan found too late in its cadence tick (2026-11-13).

The exploration lane stands aside within ``PROPOSAL_SECONDS`` of the tick's scan stop
(``exploration_lane_cadence_budget_spent``), so its reads and one proposal never push the tick
past the scheduler's cap. A scan that uses its whole budget therefore leaves the lane no time at
all: by 16:53 UTC on 11-13, 4 of the 5 refused candidates were refused that way, and the lane's
three proposals since 11-11 all came from short scans.

Such a candidate is kept here (in memory, this process only) and offered again at the start of
the next cadence tick, after the pause check and before maintenance and the scans spend that
tick's budget, through the normal auto-propose loop with every gate in it: the safety check, the
lane's rules (one entry a pass, its open and daily caps, the 15:30 New York cut-off, the eToro
symbol check), the unattended checks and the execution checks all run again on that pass's own
reads. Paper only, as the lane is; no cap, gate or bypass changes. A candidate older than
``MAX_AGE_SECONDS`` is dropped (the cadence job runs every 60 s), at most ``MAX_KEPT`` are kept
(the newest scan's copy replaces an older one of the same key), and a pass of this origin never
keeps anything, so nothing is offered twice.
"""

from __future__ import annotations

import threading
import time
from contextlib import suppress
from types import SimpleNamespace
from typing import Any

MAX_AGE_SECONDS = 300.0
MAX_KEPT = 3
ORIGIN = "exploration_deferred"

_LOCK = threading.Lock()
_KEPT: list[tuple[float, Any]] = []  # (time.monotonic() when kept, candidate), oldest first


def _key(candidate: Any) -> tuple[str, str, str]:
    return (
        str(getattr(candidate, "symbol", "") or "").upper(),
        str(getattr(candidate, "strategy_name", "") or ""),
        str(getattr(candidate, "timeframe", "") or "").lower(),
    )


def keep(candidate: Any, origin: str) -> None:
    """Keep a candidate the lane refused only for the tick's spent budget."""

    if origin == ORIGIN:
        return
    key = _key(candidate)
    with _LOCK:
        _KEPT[:] = [entry for entry in _KEPT if _key(entry[1]) != key]
        _KEPT.append((time.monotonic(), candidate))
        del _KEPT[:-MAX_KEPT]


def kept_count() -> int:
    with _LOCK:
        return len(_KEPT)


def run_deferred_exploration(service: Any) -> int:
    """Offer the kept candidates to one auto-propose pass at the start of a tick (the proposals
    it created); never raises."""

    with _LOCK:
        kept, _KEPT[:] = list(_KEPT), []
    now = time.monotonic()
    fresh = [candidate for at, candidate in kept if now - at <= MAX_AGE_SECONDS]
    if not fresh:
        return 0
    from app.workflow.operations import auto_propose_candidates

    try:
        response = SimpleNamespace(candidates=fresh)
        return auto_propose_candidates(service, response, origin=ORIGIN, notify=True)
    except Exception as exc:  # noqa: BLE001 - a deferred entry never breaks the tick
        with suppress(Exception):
            service.run_logs.log("exploration_deferred_failed", {"error": str(exc)[:300]})
        return 0
