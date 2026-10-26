"""The paper-only riders' step of a focused swing run (operator option B, 2026-10-21).

A focused run's paper-only specs (``swing_focus.focused_spec_batch``: specs the main paper gate
admits on their pooled OOS evidence alone, on the run's own swing timeframes) never enter its
scan. The scan runs exactly as without them, with ``KeptBars`` (app/screener/scan_riders.py)
passing its market data through and keeping the bars and quote of each (symbol, timeframe) the
run evaluated. The run then does everything it did before option B: its alerts, tracking,
auto-propose pass (its eToro-eligible entries are proposed, executed and copied exactly when they
would be without riders) and the swing due-time stamp. Only then, in what is left of the run
(``ride_along``):

1. Each rider's ``generate_signal`` runs on the kept bars (no fetch, no candidate checks, no
   database write; a strategy that fetches data never rides): about 55 ms a symbol for four 1d
   specs, 1.3-1.4 s for 25 symbols (in-memory bench, 2026-10-24).
2. If any fired, time is left for a check and paper has room for a paper-only entry at all
   (``PaperOnlyPass.room``: the pass's own reads), the fired signals are queued: never crypto, a
   symbol eToro or paper holds or has in flight (``taken``), a symbol with an open proposal, or a
   (spec, symbol, timeframe) checked within ``screener_duplicate_alert_window_minutes`` (the
   scan's own repeat window, which would suppress it; in-process memo); one a symbol, best first
   by confidence. So the next best rotate in, run after run.
3. At most ``RIDER_CHECKS`` queued signals get the full candidate checks: a one-symbol scan of
   their one spec on the kept bars and quote (never fetched again: the quote is the run's, which
   is fine for a paper-only entry; execution prices the order on a fresh quote), with the
   intelligence step bounded at ``RIDER_INTELLIGENCE_SECONDS``. A check is expected to take up
   to ``RIDER_CHECK_SECONDS`` and starts only with that long left; once one takes longer, no
   other starts.
4. Unless the stop has passed, their candidates are tracked (never alerted) and get their own
   auto-propose pass (origin ``<origin>:paper_only``), which considers only the candidates that
   are paper-only by its own verdicts read (``PaperOnlyPass(only=True)``), reads paper again
   (``refresh``) and makes at most one paper-only proposal.

The step's stop is the run's: the batch deadline counted from the scan's start and the cadence
tick's scan stop less ``paper_only.PROPOSAL_SECONDS`` (that proposal's own allowance), whichever
comes first; without either, or under the legacy cadence budget (no scan stop), it does not run.
It never raises: errors are counted in ``coverage.paper_only`` (specs, excluded, signals_fired,
queued, checked, candidates, errors, proposals_created, seconds_left at its start, seconds it
took, each check's outcome, and why it stopped early), and the run's own candidates and
proposals are untouched. Riders leave no scan-decision rows or scan log lines except their
checks' (at most ``RIDER_CHECKS`` one-symbol scans a run, under the scan task
``<task>:paper_only``); the run's own scan logs and coverage are as without riders, plus
``coverage.paper_only``.
"""

from __future__ import annotations

import time
from types import SimpleNamespace
from typing import Any

from app.workflow.paper_only import PaperOnlyPass

RIDER_CHECKS = 2  # queued rider signals given the full candidate checks, a run
RIDER_INTELLIGENCE_SECONDS = 5.0  # the intelligence step's bound in a rider's checks
# One rider's checks: 11-13 database transactions (counted 2026-10-24; ~0.58 s each in
# production) and the intelligence bound. A check starts only with this long left.
RIDER_CHECK_SECONDS = 13.0
_CHECKED: dict[tuple[str, str, str], float] = {}  # (key, symbol, timeframe) -> when, this process


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
    kept.replay = True  # the run's scan has returned: only its kept bars from here on
    began = time.monotonic()
    outcome: dict[str, Any] = {"specs": len(keys)}
    second = PaperOnlyPass(getattr(service, "auto_trading", None), only=True)  # the riders' pass
    try:
        _ride(service, kwargs, kept, keys, outcome, second, origin=origin, notify=notify)
    except Exception as exc:  # noqa: BLE001 - the run's own candidates are already handled
        outcome["errors"] = int(outcome.get("errors", 0)) + 1
        outcome["error"] = str(exc)[:200]
    outcome["proposals_created"] = second.created_count  # also when the pass raised
    outcome["seconds"] = round(time.monotonic() - began, 1)
    coverage = getattr(response, "coverage", None)
    if isinstance(coverage, dict):  # counts and outcomes: the spec keys are in the verdicts
        coverage["paper_only"] = outcome
    return second.created_count


def _ride(
    service: Any,
    kwargs: dict[str, Any],
    kept: Any,
    keys: list[str],
    outcome: dict[str, Any],
    second: PaperOnlyPass,
    *,
    origin: str,
    notify: bool,
) -> None:
    from app.screener.scan_riders import rider_signals
    from app.screener.service_diagnostics import ranking_key
    from app.workflow.cadence_budget import ScanDeadline
    from app.workflow.operations import auto_propose_candidates

    stop_at, why = _stop_at(service, kwargs, kept)
    if stop_at is None:
        outcome["skipped"] = why  # never an unbounded step
        return
    outcome["seconds_left"] = round(stop_at - time.monotonic(), 1)
    if stop_at - time.monotonic() < RIDER_CHECK_SECONDS:
        outcome["skipped"] = "run_deadline_too_close"
        return
    screener = service.market_screener
    riders = rider_signals(screener, kept, keys, stop_at=stop_at - RIDER_CHECK_SECONDS)
    outcome.update(
        excluded=riders.excluded,
        signals_fired=len(riders.fired),
        queued=0,
        checked=0,
        candidates=0,
        errors=riders.errors,
        checks=[],
    )
    if not riders.fired:
        return
    if stop_at - time.monotonic() < RIDER_CHECK_SECONDS:  # e.g. the signals ran to their stop
        outcome["skipped"] = "run_deadline"
        return
    room = second.room()
    if room:
        outcome["skipped"] = room  # no paper-only entry could be placed: nothing is checked
        return
    window = 60.0 * float(getattr(service.settings, "screener_duplicate_alert_window_minutes", 0) or 0)
    queue = _queue(riders.fired, second.taken() | _open_proposal_symbols(service), window)
    outcome["queued"] = len(queue)
    kept.intelligence_timeout = RIDER_INTELLIGENCE_SECONDS
    found: list[Any] = []
    for fired in queue[:RIDER_CHECKS]:
        if stop_at - time.monotonic() < RIDER_CHECK_SECONDS:
            outcome["skipped"] = "run_deadline"
            break
        outcome["checked"] += 1
        began = time.monotonic()
        check = {"key": fired.key, "symbol": fired.symbol, "timeframe": fired.timeframe}
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
            check["outcome"] = "candidate" if checked.candidates else "no_candidate"
            _CHECKED[(fired.key, fired.symbol.upper(), fired.timeframe)] = time.monotonic()
        except Exception as exc:  # noqa: BLE001 - one rider's checks never fail the run
            outcome["errors"] += 1
            check["outcome"] = f"error: {exc}"[:120]
        took = time.monotonic() - began
        check["seconds"] = round(took, 1)
        outcome["checks"].append(check)
        if took > RIDER_CHECK_SECONDS:  # slower than planned: no second one in this run
            outcome["skipped"] = "check_slower_than_planned"
            break
    outcome["candidates"] = len(found)
    if not found:
        return
    if time.monotonic() >= stop_at:
        outcome["skipped"] = "run_deadline"  # neither tracked nor proposed: past the run's stop
        return
    ranked = sorted(found, key=ranking_key, reverse=True)
    ranks = [c.model_copy(update={"rank": i + 1}) for i, c in enumerate(ranked)]
    extra = SimpleNamespace(candidates=ranks)  # what tracking and the pass read
    service._track_candidates(extra, origin=f"{origin}:paper_only")
    second.refresh()  # rules 4 and 5 on a fresh read of paper, not the one room() made
    auto_propose_candidates(  # its own origin: the run's funnel is not counted twice
        service, extra, origin=f"{origin}:paper_only", notify=notify, paper_only=second
    )


def _queue(fired: list[Any], taken: set[str], window: float) -> list[Any]:
    """The fired signals worth a check, best first by confidence, one a symbol."""

    from app.broker import crypto as crypto_symbols

    now = time.monotonic()
    for memo in [memo for memo, at in _CHECKED.items() if now - at >= window]:
        _CHECKED.pop(memo, None)
    queue: dict[str, Any] = {}
    for item in sorted(fired, key=lambda f: -float(getattr(f.signal, "confidence", 0) or 0)):
        symbol = str(item.symbol).upper()
        if crypto_symbols.is_crypto_symbol(symbol) or symbol.replace("/", "") in taken:
            continue  # never paper-only, or no paper-only entry may open there
        if (item.key, symbol, item.timeframe) in _CHECKED:
            continue  # checked within the scan's repeat window: it would be a suppressed repeat
        queue.setdefault(symbol, item)
    return list(queue.values())


def _open_proposal_symbols(service: Any) -> set[str]:
    """Symbols with a pending or approved proposal (the auto-propose pass skips them)."""

    from app.models.approval import ApprovalStatus

    proposals = getattr(service, "proposal_service", None)
    if proposals is None:
        return set()
    return {
        str(proposal.order.symbol).upper().replace("/", "")
        for status in (ApprovalStatus.PENDING, ApprovalStatus.APPROVED)
        for proposal in proposals.list_proposals(status=status)
    }


def _stop_at(service: Any, kwargs: dict[str, Any], kept: Any) -> tuple[float | None, str]:
    """The run's own stop for its riders, or (None, why not): the batch deadline from the scan's
    start and the cadence tick's scan stop less one paper-only proposal's allowance, whichever
    comes first; none under the legacy cadence budget, which has no scan stop."""

    from app.workflow.cadence_budget import current_budget
    from app.workflow.paper_only import PROPOSAL_SECONDS

    budget = current_budget()
    if budget is not None and not budget.enforces_scan_stop:
        return None, "legacy_cadence_budget"
    stops = []
    deadline = float(getattr(service.settings, "screener_batch_deadline_seconds", 0.0) or 0.0)
    if deadline > 0:
        stops.append(float(kept.started) + deadline)
    cadence = kwargs.get("cancel_event")
    if cadence is not None:
        stops.append(float(cadence.stop_at) - PROPOSAL_SECONDS)
    return (min(stops), "") if stops else (None, "no_run_deadline")
