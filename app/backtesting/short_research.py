"""Short-setup backtests (operator 2026-10-17, step 1 -- RESEARCH ONLY, never trades).

Six daily strategies emit bracketed ``entry_short`` SELLs (target < entry < stop) that the
long engine only ever treats as exits. This pass backtests them as shorts with
``EngineConfig(short_entries=True)``: the same walk-forward folds, sealed holdout, $10,000
account, risk per trade and eToro cost model as the long 1d pass, so R is comparable.
Nothing here trades: ``short_trading_enabled`` stays off, the eToro live mirror stays
long-only, and no proposal, risk gate or sizing reads what it writes.

Kept out of the live long evidence:

* rows are named ``"<strategy>:short"`` and end ``":walk_forward_oos_short"``. The evidence
  gate and the decay baseline match the exact suffix ``":walk_forward_oos"``, and the
  screener, live ranker and signal validation look up the exact strategy name;
* the engine is repo-less, so only ``_run_strategy``'s one relabelled row is persisted;
* verdicts go to their own key (``SHORT_VERDICTS_KEY``, native ``"<strategy>:short:1d"``
  keys) that only the live-scorecard report reads.

Compute: the scheduled backtest job calls this after its long pass and cursor save, on the
same thread, so it never delays, shrinks or replaces the long pass. It does nothing (not
even a DB read) on weekdays 08:00-16:30 New York time -- the session plus the pre-market
deep scan, and the same hours on ``schedule_timezone``'s clock -- or while crypto trades
around the clock. Its budget is what is left of the job's hard cap:
``min(backtest_short_research_deadline_seconds, scheduler_job_timeout_seconds - long
elapsed - MARGIN - POST_RESERVE)``. A symbol's fetch is bounded and starts only when its
timeout plus one unit (the slowest seen) still fit; the verdict refresh runs only when its
reserve is left. Its own cursor rotates (symbol x strategy) units across passes.

Measured at bc82efd: one 500-bar walk-forward is ~1.3 s CPU per strategy (~8 s for the six)
plus ~0.6 s per row insert: 0-1 symbol per pass after a ~185-215 s long pass, ~3 at most.
While the long pass has universe left it ends at 180 s or later, which leaves at most ~22 s;
the 45 s cap binds only after a short long pass. The ``backtest_short_research_run`` logs
(``units``, ``rows``, ``symbols``, ``stop``, ``cursor``) show the coverage actually reached.

Cost to the other jobs: the scheduler runs a tick's due jobs one after another, so on an
off-hours tick the next jobs (``workflow_cadence``, ``paper_position_refresh``, ...) can
start up to the short budget plus ``POST_RESERVE_SECONDS`` later than without this pass. The
job's worst case is unchanged: a long pass alone already reaches 185-215 s of the 240 s cap.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.backtesting.engine import BacktestEngine, EngineConfig
from app.backtesting.intraday_plan import history_bars_for
from app.backtesting.metrics import bars_per_year_for, leakage_tripwire_triggered
from app.backtesting.strategy_selection import strategy_kwargs_for, strategy_specs_for
from app.strategies import get_strategy
from app.universe import resolve_universe
from app.utils.time import utc_now

SHORT_OOS_SUFFIX = ":walk_forward_oos_short"
SHORT_NAME_SUFFIX = ":short"
# The daily strategies that emit bracketed entry_short SELLs (a test re-derives this set).
SHORT_STRATEGIES = frozenset(
    {
        "trend_following",
        "momentum_breakout",
        "mean_reversion",
        "ema_trend_stack",
        "rsi_trend_continuation",
        "rsi_vwap_ema_confluence",
    }
)
CURSOR_KEY = "backtest_short_research:cursor"
NEW_YORK = ZoneInfo("America/New_York")
QUIET_HOURS = ((8, 0), (16, 30))  # weekdays, local time
MARGIN_SECONDS = 30.0  # under the job cap: the job's own pre-run reads, a slow last write
POST_RESERVE_SECONDS = 8.0  # cursor save, verdict refresh and run log (~4 DB round trips)
MIN_BUDGET_SECONDS = 10.0
UNIT_ALLOWANCE_SECONDS = 3.0  # one walk-forward + insert, until a slower one is measured
MIN_FETCH_SECONDS = 3.0
VERDICT_RESERVE_SECONDS = 4.0


def in_quiet_hours(now_utc: datetime, settings: Any) -> bool:
    """Weekdays 08:00-16:30 in New York, and the same hours on the workflow's own clock."""

    zones = {NEW_YORK}
    with contextlib.suppress(Exception):  # an unknown zone leaves New York, which always applies
        zones.add(ZoneInfo(str(getattr(settings, "schedule_timezone", "") or "America/New_York")))
    for zone in zones:
        local = now_utc.astimezone(zone)
        if local.weekday() < 5 and QUIET_HOURS[0] <= (local.hour, local.minute) < QUIET_HOURS[1]:
            return True
    return False


def short_budget_seconds(settings: Any, started: float | None, now: float) -> float:
    """Seconds the pass may use after a long pass that started at ``started`` (0 = skip)."""

    if started is None:
        return 0.0
    cap = float(getattr(settings, "backtest_short_research_deadline_seconds", 0.0) or 0.0)
    job_cap = float(getattr(settings, "scheduler_job_timeout_seconds", 0) or 0.0)
    budget = min(cap, job_cap - (now - started) - MARGIN_SECONDS - POST_RESERVE_SECONDS)
    return budget if budget >= MIN_BUDGET_SECONDS else 0.0


def run_short_pass(
    service: Any,
    runtime_state: Any,
    *,
    pass_started: float | None,
    clock: Callable[[], float] = time.monotonic,
    wall: Callable[[], datetime] = utc_now,
) -> dict[str, Any] | None:
    """One bounded pass over the next (symbol x short strategy) units; None when skipped."""

    settings = service.settings
    if pass_started is None or not bool(getattr(settings, "backtest_short_research_enabled", False)):
        return None
    if bool(getattr(settings, "crypto_trading_enabled", False)) or in_quiet_hours(wall(), settings):
        return None  # a trading tick: no fetch, no DB read, no log
    now = clock()
    budget = short_budget_seconds(settings, pass_started, now)
    long_elapsed = round(now - pass_started, 1)
    if budget <= 0:
        service.logs.log("backtest_short_research_skipped", {"reason": "no_budget", "long_elapsed_seconds": long_elapsed})
        return None

    from app.broker.crypto import is_crypto_symbol

    specs = sorted(
        (spec for spec in strategy_specs_for(settings, timeframe="1d") if spec.name in SHORT_STRATEGIES),
        key=lambda spec: spec.name,
    )
    limit = int(getattr(settings, "backtest_scheduler_symbol_limit", 0) or 0) or None
    universe = [symbol for symbol in resolve_universe(settings, limit=limit) if not is_crypto_symbol(symbol)]
    units = [(symbol, spec) for symbol in universe for spec in specs]
    if not units:
        return None
    try:
        cursor = int(runtime_state.get(CURSOR_KEY) or "0") % len(units)
    except (TypeError, ValueError):
        cursor = 0
    bars = history_bars_for("1d", settings)  # outside the per-symbol try: a bug here must surface
    # Repo-less, like the folds and holdout: only _run_strategy's relabelled row persists.
    engine = BacktestEngine(
        config=EngineConfig(
            initial_cash=10_000.0,
            risk_per_trade_pct=float(getattr(settings, "max_risk_per_trade_pct", 1.0)),
            bars_per_year=bars_per_year_for("1d"),
            short_entries=True,
        )
    )
    loop = _sweep(service, engine, units[cursor:] + units[:cursor], bars, now + budget, clock, wall)
    next_cursor = (cursor + loop["units"]) % len(units)
    runtime_state.set(CURSOR_KEY, str(next_cursor))
    verdicts = "none"
    if loop["rows"]:
        db = getattr(service.backtests, "db", None)
        hard_end = pass_started + float(getattr(settings, "scheduler_job_timeout_seconds", 0) or 0) - MARGIN_SECONDS
        if db is not None and clock() <= hard_end - VERDICT_RESERVE_SECONDS:
            from app.performance.strategy_evidence import refresh_short_verdicts

            refresh_short_verdicts(db, settings, runtime_state)
            verdicts = "refreshed"
        else:
            verdicts = "deferred"  # the next pass that writes rows refreshes them
    payload = {
        "long_elapsed_seconds": long_elapsed,
        "budget_seconds": round(budget, 1),
        "seconds": round(clock() - now, 1),
        "units": loop["units"],
        "rows": loop["rows"],
        "symbols": loop["symbols"],
        "stop": loop["stop"],
        "cursor": next_cursor,
        "universe_units": len(units),
        "errors": len(loop["errors"]),
        "first_error": loop["errors"][0] if loop["errors"] else "",
        "tripwires": loop["tripwires"],
        "verdicts": verdicts,
    }
    service.logs.log("backtest_short_research_run", payload)
    return payload


def _sweep(service, engine, order, bars, loop_end, clock, wall) -> dict[str, Any]:
    """Run units in order until the budget, quiet hours or a rate limit stops the pass.

    ``units`` counts the units consumed (run, or skipped because their symbol's fetch
    failed): the cursor advances past them. A unit stopped before it starts is not consumed.
    """

    from app.broker.etoro_rate_limit import EToroRateLimitError, etoro_cooldown_remaining
    from app.screener.scan_support import _bounded_call

    settings = service.settings
    fetch_cap = float(getattr(settings, "screener_market_data_timeout_seconds", 20.0) or 20.0)
    allowance = UNIT_ALLOWANCE_SECONDS
    out: dict[str, Any] = {"units": 0, "rows": 0, "symbols": 0, "stop": "complete", "errors": [], "tripwires": []}
    current, history = None, None
    for symbol, spec in order:
        if in_quiet_hours(wall(), settings):
            out["stop"] = "quiet_hours"
            break
        remaining = loop_end - clock()
        if symbol != current:
            timeout = min(fetch_cap, remaining - allowance)
            if timeout < MIN_FETCH_SECONDS:
                out["stop"] = "budget"
                break
            if etoro_cooldown_remaining(settings) > 0:
                out["stop"] = "rate_limited"  # in a cooldown every fetch fails at once
                break
            try:
                history = _bounded_call(
                    f"{symbol}_1d_history", timeout, service.market_data.get_history,
                    symbol, timeframe="1d", bars=bars, provider=None, force_refresh=False,
                )
            except EToroRateLimitError:
                out["stop"] = "rate_limited"
                break
            except Exception as exc:  # noqa: BLE001 - one symbol never stops the pass
                out["errors"].append(f"{symbol}: history: {exc}"[:200])
                history = None
            current = symbol
            out["symbols"] += 1
        elif remaining < allowance:
            out["stop"] = "budget"
            break
        out["units"] += 1
        if history is None:
            continue
        started = clock()
        try:
            strategy = get_strategy(spec.name, **strategy_kwargs_for(settings, spec))
            summary = service._run_strategy(
                engine=engine, symbol=symbol, strategy=strategy, history=history.copy(),
                timeframe="1d", provider=None, walk_forward=True,
                oos_suffix=SHORT_OOS_SUFFIX, row_name=f"{spec.name}{SHORT_NAME_SUFFIX}",
            )
        except Exception as exc:  # noqa: BLE001 - one unit never stops the pass
            out["errors"].append(f"{symbol} {spec.name}: {exc}"[:200])
        else:
            out["rows"] += 1
            triggered, reason = leakage_tripwire_triggered(summary)
            if triggered:
                out["tripwires"].append(f"{symbol} {spec.name}: {reason}"[:200])
        allowance = max(allowance, clock() - started)
    return out
