"""Paper exploration entries a scan found too late in its cadence tick (2026-11-13).

The exploration lane stands aside within ``PROPOSAL_SECONDS`` of the tick's scan stop
(``exploration_lane_cadence_budget_spent``), so its reads and one proposal never push the tick
past the scheduler's cap. A scan that uses its whole budget therefore leaves the lane no time at
all: by 16:53 UTC on 11-13, 4 of the 5 refused candidates were refused that way, and the lane's
three proposals since 11-11 all came from short scans.

Such a candidate is kept here (in memory, this process only) and offered again at the start of
the next cadence tick, after the pause check and before maintenance and the scans spend that
tick's budget, through the normal auto-propose loop with every gate in it: the safety check, the
lane's rules (its entries a pass, its open and daily caps, the 15:30 New York cut-off, the eToro
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

Focus (operator 2026-11-22). The lane's focus signals come in clusters (5 symbols in one scan on
11-17 and on 11-20), and that week every one of them was refused for the spent budget first, so
this pass placed all the focus entries there were: 3 of 18 signals. The lane now makes up to 2
entries a pass (``paper_exploration_lane_max_per_pass``) and ``MAX_KEPT`` is 5 (was 3), so a
cluster is kept whole; before 15:30 New York the lane refuses a key outside its focus ahead of
its budget rule, so only focus candidates are kept then. An entry takes about 40 s from its
proposal to its fill (measured 36 to 41 s, 11-19 and 11-20), and this pass runs ahead of
reconciliation and the scans with no scan stop of its own, so it gets one:
no exploration entry of the pass starts later than ``ENTRY_WINDOW_SECONDS`` into the tick
(``pass_time_spent``), which bounds the pass at about 100 s; a second entry then leaves the
intraday scan (93 s of pre-work and useful scan before the scan stop) to the next tick.

Review 2026-11-22: the ledger cycle (every 15 minutes, 100 to 130 s with reconciliation, not
budgeted) could fall due while a pass of that length ran, and maintenance then ran it in the same
tick, up to about 230 s of the 240 s cap. So no entry starts within ``ENTRY_SECONDS`` of the
ledger cycle falling due: a pass that close to it leaves the candidates for the next tick (as a
tick with the cycle due always did), and the cycle's due time caps the pass's entry deadline.
"""

from __future__ import annotations

import threading
import time
from contextlib import suppress
from types import SimpleNamespace
from typing import Any

MAX_AGE_SECONDS = 300.0
MAX_KEPT = 5  # 3 until 2026-11-22: a focus cluster is 5 symbols in one scan
ENTRY_WINDOW_SECONDS = 60.0  # into the tick; no exploration entry of this pass starts later
ENTRY_SECONDS = 45.0  # one entry, proposal to fill (36 to 41 s measured), with a margin
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

    ledger_in = _ledger_due_in(service) if kept_count() else None
    if not kept_count() or (ledger_in is not None and ledger_in <= ENTRY_SECONDS):
        return 0  # the ledger cycle is due or nearly: they stay kept for the next tick
    with _LOCK:
        kept, _KEPT[:] = list(_KEPT), []
    now = time.monotonic()
    fresh = [candidate for at, candidate in kept if now - at <= MAX_AGE_SECONDS]
    if not fresh:
        return 0
    from app.workflow.operations import auto_propose_candidates
    from app.workflow.paper_only import PaperOnlyPass

    try:
        started = getattr(service, "_tick_started_at", None)  # None outside a scheduled tick
        deadlines = [] if started is None else [float(started) + ENTRY_WINDOW_SECONDS]
        if ledger_in is not None:  # an entry started later would still run when the cycle is due
            deadlines.append(now + ledger_in - ENTRY_SECONDS)
        deadline = min(deadlines) if deadlines else None
        paper_only = PaperOnlyPass(getattr(service, "auto_trading", None), entry_deadline=deadline)
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


def _ledger_due_in(service: Any) -> float | None:
    """Seconds until the tick's ledger cycle is due (<= 0: due now); None when it never runs or
    cannot be told (then they are offered now, as before)."""

    due = getattr(service, "_ledger_cycle_due", None)
    try:
        if callable(due) and due():
            return 0.0
        from app.workflow.schedule import ledger_cycle_due_in

        return ledger_cycle_due_in(service)
    except Exception:  # noqa: BLE001 - unknown: offer them now, as before
        return None
