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
``MAX_AGE_SECONDS`` is dropped (the cadence job runs every 60 s); at most ``MAX_KEPT`` are kept,
the first ones a pass refused (a pass runs best-first; a newer copy of a kept key replaces it in
place); and a pass of this origin never keeps anything, so nothing is offered twice.

Review 2026-11-13: the offering pass takes only candidates its own verdicts read still makes
exploration candidates (``ExplorationLane.likely``), and the auto-propose loop then reads those
same verdicts, so a kept candidate can only become a tagged paper-only exploration entry, never
an eToro-copyable one, even if a verdict changed meanwhile. A tick whose ledger cycle is due
(maintenance's long ungated step) leaves them for the next tick, within ``MAX_AGE_SECONDS``.
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


def keep(candidate: Any, origin: str) -> bool:
    """Keep a candidate the lane refused only for the tick's spent budget (whether it was)."""

    if origin == ORIGIN:
        return False
    key, now = _key(candidate), time.monotonic()
    with _LOCK:
        _KEPT[:] = [entry for entry in _KEPT if now - entry[0] <= MAX_AGE_SECONDS]
        for index, entry in enumerate(_KEPT):
            if _key(entry[1]) == key:
                _KEPT[index] = (now, candidate)  # the newer scan's copy, in its place
                return True
        if len(_KEPT) >= MAX_KEPT:
            return False  # the pass's better-ranked candidates came first
        _KEPT.append((now, candidate))
        return True


def kept_count() -> int:
    with _LOCK:
        return len(_KEPT)


def run_deferred_exploration(service: Any) -> int:
    """Offer the kept candidates to one auto-propose pass at the start of a tick (the proposals
    it created); never raises."""

    if not kept_count() or _ledger_cycle_due(service):
        return 0
    with _LOCK:
        kept, _KEPT[:] = list(_KEPT), []
    now = time.monotonic()
    fresh = [candidate for at, candidate in kept if now - at <= MAX_AGE_SECONDS]
    if not fresh:
        return 0
    from app.workflow.operations import auto_propose_candidates
    from app.workflow.paper_only import PaperOnlyPass

    try:
        paper_only = PaperOnlyPass(getattr(service, "auto_trading", None))
        fresh = [candidate for candidate in fresh if paper_only.exploration.likely(candidate)]
        if not fresh:
            return 0
        response = SimpleNamespace(candidates=fresh)
        return auto_propose_candidates(
            service, response, origin=ORIGIN, notify=True, paper_only=paper_only
        )
    except Exception as exc:  # noqa: BLE001 - a deferred entry never breaks the tick
        with suppress(Exception):
            service.run_logs.log("exploration_deferred_failed", {"error": str(exc)[:300]})
        return 0


def _ledger_cycle_due(service: Any) -> bool:
    due = getattr(service, "_ledger_cycle_due", None)
    try:
        return bool(callable(due) and due())
    except Exception:  # noqa: BLE001 - unknown: offer them now, as before
        return False
