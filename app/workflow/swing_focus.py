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
"""

from __future__ import annotations

import json
from typing import Any

SYMBOL_CURSOR_KEY = "workflow:symbol_offset:{task}"
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
    passing = _passing_keys(service.runtime_state)
    selected = [key for key in lister(timeframes) if key in passing]
    if not selected:
        return None
    from app.workflow.operations import _timeframes_for_spec_keys

    return {
        "mode": "evidence_focus",
        "rotation_key": FOCUS_RUNS_KEY,
        "batch_index": 0,
        "batch_size": len(selected),
        "total_specs": len(selected),
        "strategy_spec_keys": selected,
        "timeframes": _timeframes_for_spec_keys(selected, fallback=timeframes),
    }


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


def _passing_keys(runtime_state: Any) -> set[str]:
    from app.performance.strategy_evidence import VERDICTS_KEY

    try:
        cached = json.loads(runtime_state.get(VERDICTS_KEY) or "{}")
    except (TypeError, ValueError):
        return set()
    verdicts = cached.get("verdicts") if isinstance(cached, dict) else None
    if not isinstance(verdicts, dict):
        return set()
    return {key for key, v in verdicts.items() if isinstance(v, dict) and v.get("passed")}


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
