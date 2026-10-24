"""The paper-only riders' step of a focused swing run (operator option B, 2026-10-21).

A focused run's paper-only specs (``swing_focus.focused_spec_batch``: specs the main paper gate
admits on their pooled OOS evidence alone, on the run's own swing timeframes) never enter its
scan. The scan runs exactly as without them, only through ``KeptBars``
(app/screener/scan_riders.py), which keeps the bars and quotes it fetched. The run then does
everything it did before option B: its alerts, tracking, auto-propose pass (its eToro-eligible
entries are proposed, executed and copied exactly when they would be without riders) and the
swing due-time stamp. Only then, in what is left of the run (``ride_along``):

1. Each rider's ``generate_signal`` runs on the kept bars (no fetch, no candidate checks, no
   database write; a strategy that fetches data never rides): about 55 ms a symbol for four 1d
   specs, 1.3-1.4 s for 25 symbols (in-memory bench, 2026-10-24), none of it before the run's
   own pass.
2. If any fired and paper has room for a paper-only entry at all (``PaperOnlyPass.room``: one
   read of paper, reused by the pass), at most ``RIDER_CHECKS`` fired signals, best first by
   confidence, get the full candidate checks: a one-symbol scan of their one spec on the kept
   bars, with the intelligence step bounded at ``RIDER_INTELLIGENCE_SECONDS``. Each takes about
   ``RIDER_CHECK_SECONDS`` at most (its database round trips and that bound) and starts only
   with that much time left.
3. Their candidates are tracked (never alerted) and get their own auto-propose pass (origin
   ``<origin>:paper_only``), which considers only the candidates that are paper-only by its own
   verdicts read (``PaperOnlyPass(only=True)``) and makes at most one paper-only proposal.

The step is bounded by the run's deadline: the batch deadline counted from the scan's start and
the cadence tick's scan stop less ``paper_only.PROPOSAL_SECONDS`` (that proposal's own
allowance), whichever comes first; without either it does not run. It never raises: an error is
counted in ``coverage.paper_only`` (counts only: specs, excluded, signals_fired, checked,
candidates, errors, proposals_created, and why it stopped early), and the run's own candidates
and proposals are untouched. Riders leave no scan-decision rows or scan log lines except their
checks' (at most ``RIDER_CHECKS`` one-symbol scans a run, under the scan task
``<task>:paper_only``); the run's own scan logs and coverage are as without riders, plus
``coverage.paper_only``.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

RIDER_CHECKS = 2  # fired rider signals given the full candidate checks, a run
RIDER_INTELLIGENCE_SECONDS = 5.0  # the intelligence step's bound in a rider's checks
# One rider's checks, at most: 11-13 database transactions (counted 2026-10-24; ~0.58 s each in
# production) and the intelligence bound. Each check, and the step, needs this long left.
RIDER_CHECK_SECONDS = 13.0


def kept_bars(service: Any, spec_batch: dict[str, Any] | None, scan_params: Any) -> dict[str, Any]:
    """The run's scan arguments that keep its bars for riders: {"kept_bars": KeptBars} when the
    batch has riders and the screener can keep them, else {}."""

    if not (spec_batch or {}).get("paper_only_spec_keys") or "kept_bars" not in scan_params:
        return {}
    engine = getattr(service.market_screener, "market_data", None)
    if engine is None:  # nothing to pass through: no riders
        return {}
    from app.screener.scan_riders import KeptBars

    return {"kept_bars": KeptBars(engine)}


def ride_along(
    service: Any,
    kwargs: dict[str, Any],
    response: Any,
    spec_batch: dict[str, Any] | None,
    *,
    origin: str,
    notify: bool,
) -> int:
    """The riders' step, after the run's own pass and due-time stamp. Returns the proposals it
    created; never raises."""

    keys = list((spec_batch or {}).get("paper_only_spec_keys") or [])
    kept = kwargs.get("kept_bars")
    if not keys or kept is None:
        return 0
    outcome: dict[str, Any] = {"specs": len(keys)}
    second = None  # the riders' own auto-propose pass
    try:
        second = _ride(service, kwargs, kept, keys, outcome, origin=origin, notify=notify)
    except Exception as exc:  # noqa: BLE001 - the run's own candidates are already handled
        outcome["errors"] = int(outcome.get("errors", 0)) + 1
        outcome["error"] = str(exc)[:300]
    created = second.created_count if second is not None else 0  # also when the pass raised
    outcome["proposals_created"] = created
    coverage = getattr(response, "coverage", None)
    if isinstance(coverage, dict):  # counts only: the spec keys are in the verdicts
        coverage["paper_only"] = outcome
    return created


def _ride(
    service: Any,
    kwargs: dict[str, Any],
    kept: Any,
    keys: list[str],
    outcome: dict[str, Any],
    *,
    origin: str,
    notify: bool,
) -> Any:
    from app.screener.scan_riders import rider_signals
    from app.screener.service_diagnostics import ranking_key
    from app.workflow.cadence_budget import ScanDeadline
    from app.workflow.operations import auto_propose_candidates
    from app.workflow.paper_only import PaperOnlyPass

    stop_at = _stop_at(service, kwargs, kept)
    if stop_at is None:
        outcome["skipped"] = "no_run_deadline"  # never an unbounded step
        return None
    if stop_at - time.monotonic() < RIDER_CHECK_SECONDS:
        outcome["skipped"] = "run_deadline_too_close"
        return None
    screener = service.market_screener
    riders = rider_signals(screener, kept, keys, stop_at=stop_at - RIDER_CHECK_SECONDS)
    outcome.update(
        excluded=riders.excluded,
        signals_fired=len(riders.fired),
        checked=0,
        candidates=0,
        errors=riders.errors,
    )
    if riders.stopped:
        outcome["skipped"] = "run_deadline"
    if not riders.fired:
        return None
    second = PaperOnlyPass(getattr(service, "auto_trading", None), only=True)
    room = second.room()
    if room:
        outcome["skipped"] = room  # no paper-only entry could be placed: nothing is checked
        return second
    kept.intelligence_timeout = RIDER_INTELLIGENCE_SECONDS
    found: list[Any] = []
    best = sorted(riders.fired, key=lambda item: -float(getattr(item.signal, "confidence", 0) or 0))
    for fired in best[:RIDER_CHECKS]:
        if stop_at - time.monotonic() < RIDER_CHECK_SECONDS:
            outcome["skipped"] = "run_deadline"
            break
        outcome["checked"] += 1
        try:
            checked = screener.scan_universe(
                symbols=[fired.symbol],
                timeframes=[fired.timeframe],
                limit=1,
                validated_only=bool(kwargs.get("validated_only", False)),
                notify=False,
                force_refresh=False,
                scan_task=f"{kwargs.get('scan_task') or 'swing_scan'}:paper_only",
                strategy_spec_keys=[fired.key],
                cancel_event=ScanDeadline(stop_at),
                kept_bars=kept,
            )
            found.extend(checked.candidates)
        except Exception:  # noqa: BLE001 - one rider's checks never fail the run
            outcome["errors"] += 1
    outcome["candidates"] = len(found)
    if not found:
        return second
    ranked = sorted(found, key=ranking_key, reverse=True)
    ranks = [c.model_copy(update={"rank": i + 1}) for i, c in enumerate(ranked)]
    extra = SimpleNamespace(candidates=ranks)  # what tracking and the pass read
    service._track_candidates(extra, origin=origin)
    auto_propose_candidates(  # its own origin: the run's funnel is not counted twice
        service, extra, origin=f"{origin}:paper_only", notify=notify, paper_only=second
    )
    return second


def _stop_at(service: Any, kwargs: dict[str, Any], kept: Any) -> float | None:
    """The run's own deadline for its riders: the batch deadline from the scan's start and the
    cadence tick's scan stop less one paper-only proposal's allowance, whichever comes first."""

    from app.workflow.paper_only import PROPOSAL_SECONDS

    stops = []
    deadline = float(getattr(service.settings, "screener_batch_deadline_seconds", 0.0) or 0.0)
    if deadline > 0:
        stops.append(float(kept.started) + deadline)
    cadence = kwargs.get("cancel_event")
    if cadence is not None:
        stops.append(float(cadence.stop_at) - PROPOSAL_SECONDS)
    return min(stops) if stops else None
