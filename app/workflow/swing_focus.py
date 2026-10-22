"""Evidence-focused swing scan (operator request 2026-10-06: "build the scan fix").

The hourly swing scan rotated 6 of 38 strategy specs per run, 1h specs first, and its
180 s deadline reached only the first 6-10 of 25 symbols -- always the same ones. With the
Phase 2 evidence gate on, only specs with a passing walk-forward verdict (today six 1d
strategies) can trade, so each of them was checked about once a day on a third of the
universe, and most of every run went on specs that cannot trade.

While the gate is enforced this module makes the swing scan:

* evaluate only the passing specs (falling back to the normal rotation when no verdicts
  are cached), with every ``swing_focus_shadow_every``-th run left to the rotation so
  failing strategies keep producing shadow signals and can earn their way back;
* start each run where the last one stopped in the symbol list, so the deadline-bounded
  runs sweep all symbols in turn;
* run every ``swing_focus_interval_minutes`` (default 10) instead of hourly.

No gate is loosened: the proposal path still checks evidence, risk and caps as before.

Paper-only specs (operator option B, 2026-10-21): a 1d/1h/1w spec the main paper gate admits on
its pooled OOS evidence alone (``strategy_evidence.paper_oos_only``) is never scanned in the same
scan as the run's eToro-passing specs: a scan works symbol by symbol, so extra specs would slow
every symbol (fewer symbols before the deadline for the eToro-passing specs) and their
candidates could take the run's top-k. A focused run first does everything it did before option
B: its scan, alerts, tracking and auto-propose pass, whose entries are proposed, executed and
copied to eToro as before. Only then (``paper_only_scan``), and only when that scan reached every
symbol, the paper-only specs get a second scan bounded by what is left of the same run: the batch
deadline counted from the first scan's start and the cadence tick's scan stop less
``PAPER_ONLY_PASS_SECONDS`` (time for the one paper-only proposal a pass may make), so the
tick's post-scan reserve is never spent on it. Its top ``PAPER_ONLY_TOP_K`` candidates are
tracked (no scan alert of their own) and given a second auto-propose pass. ``coverage.
paper_only_scan`` says when and why it was skipped; those specs still come up in the rotation
runs. The paper-only scan sweeps the symbols from its own cursor and asks for cached bars
(``force_refresh`` off); whether the first scan's bars are reused is the data engine's choice
(with ``require_verified_market_data_for_alerts`` it may fetch them again).
"""

from __future__ import annotations

import inspect
import json
import time
from typing import Any

SYMBOL_CURSOR_KEY = "workflow:symbol_offset:{task}"
PAPER_ONLY_CURSOR_KEY = "workflow:symbol_offset:swing_scan:paper_only"
FOCUS_RUNS_KEY = "workflow:swing_focus:runs"


def focus_active(settings: Any) -> bool:
    return bool(getattr(settings, "require_strategy_oos_evidence", False)) and bool(
        getattr(settings, "swing_scan_evidence_focus", True)
    )


def swing_interval_minutes(settings: Any) -> int:
    """How often the swing scan is due: the focused cadence while the gate is on."""

    if focus_active(settings):
        return max(int(getattr(settings, "swing_focus_interval_minutes", 10) or 10), 1)
    return int(getattr(settings, "swing_scan_interval_minutes", 60) or 60)


def focused_spec_batch(service: Any, *, task: str, timeframes: list[str]) -> dict[str, Any] | None:
    """The passing specs for this swing run, or None to use the normal rotation."""

    if task != "swing_scan" or not focus_active(service.settings):
        return None
    lister = getattr(service.market_screener, "strategy_spec_keys_for_timeframes", None)
    if lister is None:
        return None
    runs = _int(service.runtime_state.get(FOCUS_RUNS_KEY)) + 1
    service.runtime_state.set(FOCUS_RUNS_KEY, str(runs))
    shadow_every = int(getattr(service.settings, "swing_focus_shadow_every", 6) or 0)
    if shadow_every > 0 and runs % shadow_every == 0:
        return None  # this run rotates through all specs for shadow signals
    passing, paper_only = _verdict_keys(service)
    keys = lister(timeframes)
    selected = [key for key in keys if key in passing]
    if not selected:
        return None
    from app.workflow.operations import _timeframes_for_spec_keys

    later = [key for key in keys if key in paper_only]  # scanned later: paper_only_scan
    return {
        "mode": "evidence_focus",
        "rotation_key": FOCUS_RUNS_KEY,
        "batch_index": 0,
        "batch_size": len(selected),
        "total_specs": len(selected),
        "strategy_spec_keys": selected,
        "timeframes": _timeframes_for_spec_keys(selected, fallback=timeframes),
        "paper_only_spec_keys": later,
        "paper_only_timeframes": _timeframes_for_spec_keys(later, fallback=timeframes),
    }


PAPER_ONLY_TOP_K = 3  # candidates the paper-only scan keeps (not screener_top_k)
PAPER_ONLY_PASS_SECONDS = 30.0  # the scan's stop overshoot (<= 10 s) + one paper-only proposal


def paper_only_scan(
    service: Any,
    kwargs: dict[str, Any],
    response: Any,
    spec_batch: dict[str, Any] | None,
    started: float,
    *,
    origin: str,
    notify: bool,
) -> int:
    """After the run's own alerts, tracking and auto-propose pass: in a focused run whose
    eToro-passing specs reached every symbol, the paper-only specs with the run's time left,
    then a second auto-propose pass on their candidates. Returns the proposals it created;
    never raises (the run's own candidates were handled already)."""

    keys = list((spec_batch or {}).get("paper_only_spec_keys") or [])
    if not keys or not kwargs.get("symbols"):
        return 0
    created = 0
    try:
        timeframes = (spec_batch or {})["paper_only_timeframes"]
        outcome, extra = _paper_only_scan(service, kwargs, response, keys, timeframes, started)
        if extra is not None:
            from app.workflow.operations import auto_propose_candidates

            service._track_candidates(extra, origin=origin)
            created = auto_propose_candidates(service, extra, origin=origin, notify=notify)
            outcome["proposals_created"] = created
    except Exception as exc:  # noqa: BLE001 - the run's own candidates are already handled
        outcome = {"error": str(exc)[:300]}
    coverage = getattr(response, "coverage", None)
    if isinstance(coverage, dict):  # counts only: the spec keys are in the verdicts
        coverage["paper_only_scan"] = {"specs": len(keys), **outcome}
    return created


def _paper_only_scan(
    service: Any,
    kwargs: dict[str, Any],
    response: Any,
    keys: list[str],
    timeframes: list[str],
    started: float,
) -> tuple[dict[str, Any], Any]:
    from app.workflow.cadence_budget import MIN_USEFUL_SCAN_SECONDS, ScanDeadline

    symbols = sorted(kwargs["symbols"])
    if _count(response.evaluated_symbols) < len(symbols) or any(
        error in {"scan_deadline_exceeded", "scan_cancelled"} for error in response.errors
    ):
        return {"skipped": "etoro_passing_specs_unfinished"}, None
    stops = []
    deadline = float(getattr(service.settings, "screener_batch_deadline_seconds", 0.0) or 0.0)
    if deadline > 0:
        stops.append(started + deadline)
    cadence = kwargs.get("cancel_event")
    if cadence is not None:  # the cadence tick's own scan stop, less one paper-only proposal
        stops.append(float(cadence.stop_at) - PAPER_ONLY_PASS_SECONDS)
    if not stops:
        return {"skipped": "no_run_deadline"}, None  # never an unbounded extra scan
    stop_at = min(stops)
    if stop_at - time.monotonic() < MIN_USEFUL_SCAN_SECONDS:
        return {"skipped": "run_deadline_too_close"}, None
    if "cancel_event" not in inspect.signature(service.market_screener.scan_universe).parameters:
        return {"skipped": "scan_cannot_stop"}, None
    offset = _int(service.runtime_state.get(PAPER_ONLY_CURSOR_KEY)) % len(symbols)
    extra = service.market_screener.scan_universe(
        **{
            **kwargs,
            "symbols": symbols[offset:] + symbols[:offset],
            "timeframes": timeframes,
            "strategy_spec_keys": keys,
            "limit": PAPER_ONLY_TOP_K,
            "force_refresh": False,  # cached bars where the data engine has them
            "cancel_event": ScanDeadline(stop_at),
        }
    )
    evaluated = _count(extra.evaluated_symbols)
    service.runtime_state.set(
        PAPER_ONLY_CURSOR_KEY, str((offset + max(evaluated, 1)) % len(symbols))
    )
    outcome = {
        "symbols_evaluated": evaluated,
        "candidates": len(extra.candidates),
        "errors": list(extra.errors)[:5],
    }
    return outcome, extra


def rotate_symbols(service: Any, *, task: str, symbols: list[str]) -> list[str]:
    """Start this run where the last one stopped (swing scan only)."""

    if task != "swing_scan" or not symbols:
        return symbols
    offset = _int(service.runtime_state.get(SYMBOL_CURSOR_KEY.format(task=task))) % len(symbols)
    return symbols[offset:] + symbols[:offset]


def advance_symbols(service: Any, *, task: str, total: int, evaluated: int) -> None:
    if task != "swing_scan" or total <= 0:
        return
    key = SYMBOL_CURSOR_KEY.format(task=task)
    offset = _int(service.runtime_state.get(key))
    service.runtime_state.set(key, str((offset + max(evaluated, 1)) % total))


def _verdict_keys(service: Any) -> tuple[set[str], set[str]]:
    """(the keys passing the full check, the paper-only keys: paper's OOS bar alone)."""

    from app.performance.strategy_evidence import VERDICTS_KEY, paper_oos_only

    try:
        cached = json.loads(service.runtime_state.get(VERDICTS_KEY) or "{}")
    except (TypeError, ValueError):
        return set(), set()
    verdicts = cached.get("verdicts") if isinstance(cached, dict) else None
    if not isinstance(verdicts, dict):
        return set(), set()
    passing = {key for key, v in verdicts.items() if isinstance(v, dict) and v.get("passed")}
    paper_only = {
        key
        for key, v in verdicts.items()
        if paper_oos_only(service.settings, v, key.rsplit(":", 1)[-1])
    }
    return passing, paper_only


def _count(value: Any) -> int:
    """A scan's ``evaluated_symbols``: a count (or a list)."""

    return len(value) if isinstance(value, (list, tuple)) else _int(value)


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
