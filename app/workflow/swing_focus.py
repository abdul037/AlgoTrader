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

Paper-only specs (operator option B, 2026-10-21): a spec the main paper gate admits on its
pooled OOS evidence alone (``strategy_evidence.paper_oos_only``) rides along in a focused run's
own scan (``paper_only_spec_keys``), on the run's own swing timeframes only: those of
``PAPER_ONLY_TIMEFRAMES`` its eToro-passing specs already fetch (in practice 1d), so the scan
fetches no extra bars or quotes. On each symbol and timeframe the screener evaluates the
paper-only specs after the eToro-passing ones, on the same bars, and cuts their candidates apart
(app/screener/scan_ranking.py): the run's top-k keeps exactly the eToro-passing specs'
candidates it would keep without them, and the paper-only specs keep their own top three
(``scan_ranking.PAPER_ONLY_TOP_K``) after those. What the eToro-passing specs pay is that
evaluation on every symbol: about 55-70 ms a symbol for four 1d specs on in-memory bars
(1.4-1.7 s a 25-symbol scan; measured 2026-10-24), and about 50 ms more for each paper-only
signal that fires (its candidate checks). The run's one auto-propose pass then considers every
other candidate before a paper-only one and makes at most one paper-only proposal
(app/workflow/paper_only.py), so its eToro-eligible entries are proposed, executed and copied
first. Paper-only candidates are tracked like the others but get no scan alert (``alerted``);
``coverage.paper_only`` counts their specs and candidates. A run takes no paper-only spec along
unless one could be proposed and executed at once (``paper_only.scan_wanted``: the main paper
account with the switch and the evidence gate on, paper auto-approval and the unattended mode).
The rotation runs scan every spec under one cut, as before. (Until 2026-10-23 the paper-only
specs had a second scan after the run's own pass, in what was left of its deadline; in
production it never had the 45 s it needed.)
"""

from __future__ import annotations

import json
from typing import Any

from app.workflow.paper_only import scan_wanted

SYMBOL_CURSOR_KEY = "workflow:symbol_offset:{task}"
FOCUS_RUNS_KEY = "workflow:swing_focus:runs"
PAPER_ONLY_TIMEFRAMES = frozenset({"1h", "1d", "1w"})  # paper-only specs: never a minute-bar key


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
    passing, oos_only = _verdict_keys(service)
    keys = lister(timeframes)
    selected = [key for key in keys if key in passing]
    if not selected:
        return None
    from app.workflow.operations import _timeframes_for_spec_keys

    run_timeframes = _timeframes_for_spec_keys(selected, fallback=timeframes)
    # Riding along: paper-only keys on the run's own swing timeframes (no extra bars), never one
    # of the run's own keys.
    riding = PAPER_ONLY_TIMEFRAMES.intersection(run_timeframes)
    paper_only = [
        key
        for key in keys
        if key in oos_only and key not in selected and key.rsplit(":", 1)[-1].lower() in riding
    ]
    return {
        "mode": "evidence_focus",
        "rotation_key": FOCUS_RUNS_KEY,
        "batch_index": 0,
        "batch_size": len(selected),
        "total_specs": len(selected),
        "strategy_spec_keys": selected,
        "timeframes": run_timeframes,
        "paper_only_spec_keys": paper_only if paper_only and scan_wanted(service) else [],
    }


def alerted(response: Any, spec_batch: dict[str, Any] | None) -> Any:
    """The run's response as its scan alerts read it: without the paper-only specs' candidates,
    which are tracked and may be proposed but get no scan alert."""

    from app.screener.scan_ranking import candidate_key

    keys = set((spec_batch or {}).get("paper_only_spec_keys") or [])
    kept = [item for item in response.candidates if candidate_key(item) not in keys]
    if len(kept) == len(response.candidates):
        return response
    return response.model_copy(update={"candidates": kept})


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


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
