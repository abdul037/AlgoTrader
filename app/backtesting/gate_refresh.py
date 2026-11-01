"""The scheduled walk-forward backtests behind the evidence gate (the ``backtest_gate_refresh`` job).

A full-universe walk-forward pass overruns the scheduler's hard job cap, so each run is bounded
by a soft deadline and a persisted cursor rotates successive runs over the universe; a run that
stops inside a symbol resumes at that symbol's next strategy.

Daily first (operator 2026-11-01, item A): one pass used to run every timeframe symbol by
symbol, and the minute-bar walk-forwards took most of each pass, so from 10-27 only 10-16 of the
25 symbols had their daily evidence refreshed a day. The daily strategies are the only ones
that can trade, so with both kinds configured a pass now runs the daily timeframes alone, on
their own cursor, until every symbol has been covered since the last full daily cycle, and runs
the minute-bar timeframes on the original cursor for the rest of the ``DAILY_CYCLE_HOURS``.
Minute-bar verdicts read 7 days of rows, so their slower rotation still reaches every symbol.
With one kind configured, every pass runs it on the original cursor, as before.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.backtesting.intraday_plan import is_minute_bar
from app.utils.time import utc_now

CURSOR_KEY = "backtest_gate_refresh:cursor"
UNIT_KEY = "backtest_gate_refresh:unit"
DAILY_CURSOR_KEY = "backtest_gate_refresh:daily_cursor"
DAILY_UNIT_KEY = "backtest_gate_refresh:daily_unit"
DAILY_COVERED_KEY = "backtest_gate_refresh:daily_covered"  # symbols since the last full cycle
DAILY_FULL_AT_KEY = "backtest_gate_refresh:daily_full_at"  # when the last full cycle ended
DAILY_CYCLE_HOURS = 12.0


def _int(runtime_state: Any, key: str) -> int:
    try:
        return max(int(runtime_state.get(key) or "0"), 0)
    except (TypeError, ValueError):
        return 0


def _daily_due(runtime_state: Any, now: datetime) -> bool:
    try:
        full_at = datetime.fromisoformat(str(runtime_state.get(DAILY_FULL_AT_KEY) or ""))
    except ValueError:
        return True  # never completed, or unreadable: refresh the daily evidence first
    return now - full_at >= timedelta(hours=DAILY_CYCLE_HOURS)


def refresh_backtest_gate(batch: Any, settings: Any, runtime_state: Any) -> dict[str, Any]:
    """Run one bounded pass, advance its cursor, then the short research. Returns what ran."""

    from app.universe import resolve_universe

    limit = settings.backtest_scheduler_symbol_limit or None
    universe_size = len(resolve_universe(settings, limit=limit))
    timeframes = [str(tf).lower() for tf in (settings.backtest_scheduler_timeframes or ["1d"])]
    daily = [tf for tf in timeframes if not is_minute_bar(tf)]
    minute = [tf for tf in timeframes if is_minute_bar(tf)]
    now = utc_now()
    run_daily = bool(daily and minute) and _daily_due(runtime_state, now)
    if run_daily:
        kind, run_timeframes, cursor_key, unit_key = "daily", daily, DAILY_CURSOR_KEY, DAILY_UNIT_KEY
    else:
        kind = "minute" if daily and minute else "all"
        run_timeframes = minute if daily and minute else timeframes
        cursor_key, unit_key = CURSOR_KEY, UNIT_KEY
    cursor, unit = _int(runtime_state, cursor_key), _int(runtime_state, unit_key)
    summary = batch.run(
        timeframes=run_timeframes,
        limit=limit,
        walk_forward=True,
        deadline_seconds=max(float(settings.backtest_scheduler_deadline_seconds), 1.0),
        start_offset=cursor,
        start_unit=unit,
    )
    covered = int(getattr(summary, "symbols_evaluated", 0) or 0)
    mid_symbol = bool(getattr(summary, "stopped_mid_symbol", False))
    resume = int(getattr(summary, "resume_unit", 0) or 0) if mid_symbol else 0
    if universe_size > 0:
        # Stopped mid-symbol: stay on it and resume at that strategy.
        advance = covered - 1 if mid_symbol else max(covered, 1)
        runtime_state.set(cursor_key, str((cursor + advance) % universe_size))
        runtime_state.set(unit_key, str(resume))
        if run_daily:
            done = _int(runtime_state, DAILY_COVERED_KEY) + advance
            if done >= universe_size:
                runtime_state.set(DAILY_FULL_AT_KEY, now.isoformat())
                done = 0
            runtime_state.set(DAILY_COVERED_KEY, str(done))
    batch.run_short_research(runtime_state)
    return {"pass": kind, "timeframes": run_timeframes, "start_offset": cursor, "covered": covered}
