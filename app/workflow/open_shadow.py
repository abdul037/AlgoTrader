"""Phase 3 shadow: best-first entries at the open on the completed bar (operator 2026-10-18).

SHADOW ONLY: going live needs another operator OK. Design: OPS_LOG 2026-10-10, step 2. The
backtests decide on the completed bar N and fill at bar N+1's open, but every live 1d entry so
far was signalled hours after the open on the unfinished bar. This records what entering the
morning's deep-scan watchlist (``premarket:watchlist:<NY date>``) best-first at the open would
have done:

1. The top ``TOP_ITEMS`` eligible setups (allowed, not blacklisted, not held or pending at the
   deep scan, strategy passing the evidence gate now) are re-checked on COMPLETED daily bars
   with the live screener's own strategy build (``CompletedBarsMarketData``: the engine with
   today's forming bar dropped, the deep scan's rule). A signal that is gone or differs (entry,
   stop, target, strict/weak, its bar, or the strategy no longer listed) is a ``mismatch``:
   skipped and logged.
2. A fresh quote comes from the order path's own call (``get_quote`` with the equities broker
   as provider, force_refresh). The entry is re-anchored to it, the signal's absolute stop and
   target are kept, and the live gates are re-checked at that price: the coordinator's quote
   checks (provider, direct, age, missing), the screener's spread ceiling and the bracket at the
   quote with the live minimum R:R (1.0). The entry drift from the signal's price (the gap to
   the open) is recorded: an order priced at the quote would see about none at the
   coordinator's check (quote vs proposed price), so it gates only the second ranking,
   ``drift_gated``, until the operator rules which reading applies. Then the eToro mirror's
   per-trade copy rules (+0.03 R edge, 8% stop cap, sizing so a stop-out costs
   0.5% of equity, eToro's $10 minimum; of the 2026-10-18 rules, the ones whose inputs exist
   here, as in the deep scan's hint: evidence passing >= 24 h unbroken, decided now, and no
   chased move on the signal's completed bars -- the planner and per-signal backtest rules
   need the live scan's build and are not checked) and its room (symbol open, 9 positions, 4
   per correlation group, free cash) as hints from stored state only, leaving out the copies
   the live bot opened that day (these entries would have taken their place).
3. The setups that pass are ranked eToro-copyable first, strict before weak among those, then
   the pooled OOS expectancy in R, then R:R at the quote. The first ``HARD_MAX_TRADES_PER_DAY``
   copyable ones with room take the eToro daily slots, the rest are paper-only, and another
   setup on a symbol already taken is a ``duplicate_symbol``.

It runs last in a ``workflow_cadence`` tick, after every live bucket, and only once the live
market-open scan has completed that day, when no bucket was deferred and at least
``MIN_START_SECONDS`` of the tick's scan budget are left (the cadence backstop's own margin, so
never after a bucket that backstop skipped and left due). It stops at the tick's scan stop or
after ``RUN_SECONDS``, whichever is first, resumes on the next tick from its cursor, and at
10:30 New York time is closed with what it has. It writes only
``runtime_state['premarket:open_shadow:<NY date>']`` and, once the day is done, one
``premarket_open_shadow_completed`` run log. It never creates proposals, orders, approvals,
scan decisions, alerts or Telegram messages, never calls the eToro LIVE account, and never
raises: a failure is logged once a day and the scheduler carries on. Its market data are the
deep scan's and the order path's own requests, but read-only on the shared market-data cache
(``write_cache=False``): a frame it fetched is never served to a live scan (review 10-19).
``premarket_open_shadow_enabled`` (default on) and the deep scan's flag switch it.
"""

from __future__ import annotations

import json
import math
import time
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from app.utils.time import utc_now
from app.workflow.cadence_budget import (
    MIN_USEFUL_SCAN_SECONDS,
    POST_SCAN_RESERVE_SECONDS,
    SCAN_SETUP_SECONDS,
)

RECORD_KEY = "premarket:open_shadow:{date}"
SCHEMA_VERSION = 1
MARKET_OPEN_KEY = "workflow:last_market_open_scan_at"  # set when the live scan completes
TEST_ORDER_KEY = "etoro_live:test_order"
TOP_ITEMS = 10  # setups re-checked at the open: each costs a history and a quote request
# The deep scan's no-chase inputs on a watchlist item (the eToro copy's rule 4, 2026-10-18).
_EXTENSION_FIELDS = ("return_20d_pct", "rsi_14", "new_high_252_recent")
DEADLINE = (10, 30)  # New York: entries "at the open" end here; an open record is closed
CLOSE_OUT_ENDS = (16, 0)  # a record still open at the deadline is closed until the bell
RUN_SECONDS = 30.0  # per tick at most; the rest on the next tick
# Of the tick's scan budget, or this tick is skipped. run_scan_task's backstop skips a bucket
# (which stays due for the next tick) with less than this left, so a tick that skipped one never
# reaches the shadow with this much: it never adds to that bucket's wait (review 10-18).
MIN_START_SECONDS = SCAN_SETUP_SECONDS + MIN_USEFUL_SCAN_SECONDS
FINAL = frozenset(
    {
        "complete",
        "incomplete_at_deadline",
        "missed_deadline",
        "no_watchlist",
        "watchlist_unreadable",
        "watchlist_wrong_date",
        "watchlist_not_final",
        "watchlist_missed_cutoff",
    }
)
RULES = (
    "shadow only: completed-bar re-check with the live build (a mismatch is skipped); entry "
    "re-anchored to a fresh quote, the signal's stop and target kept; live gates at the quote "
    "(quote checks, spread, R:R); entry drift from the signal's price (the gap to the open) "
    "recorded and gating only drift_gated, the same ranking, until the operator rules; eToro "
    "per-trade copy rules (not the planner or per-signal backtest rules: the live scan's) and "
    "room hints (stored state only); ranked eToro-copyable, strict "
    "before weak (eToro), OOS expectancy R, R:R; 2 eToro slots, the rest paper-only"
)


class CompletedBarsMarketData:
    """The market data engine with today's forming daily bar dropped (the deep scan's rule:
    the UTC date of a daily bar is its session date), and never writing the shared cache: the
    live swing scan reads 1d frames from it (force_refresh=False), so a frame fetched here would
    be its input for the cache's TTL. Everything else is the engine's own."""

    def __init__(self, market_data: Any, now_ny: datetime) -> None:
        self._market_data, self._now_ny = market_data, now_ny

    def get_history(self, symbol: str, *, timeframe: str = "1d", **kwargs: Any) -> Any:
        from app.workflow.premarket_deep_scan import completed_daily_bars

        kwargs["write_cache"] = False
        frame = self._market_data.get_history(symbol, timeframe=timeframe, **kwargs)
        return completed_daily_bars(frame, self._now_ny) if timeframe == "1d" else frame

    def __getattr__(self, name: str) -> Any:
        return getattr(self._market_data, name)


class _NoAnswer(Exception):
    """A fetch the tick's budget cut short: no answer, the next tick tries it in full."""


def run_open_shadow(
    service: Any, *, started_at: float, deferred: Any = (), clock: Any = time.monotonic
) -> dict[str, Any] | None:
    """One tick of the Phase 3 shadow, last in a cadence tick. None when nothing was done.

    Never raises: a failure is logged once a day and the scheduler carries on."""

    try:
        return _tick(service, started_at, list(deferred or []), clock)
    except Exception as exc:  # noqa: BLE001 - shadow only: never into the scheduler or a scan
        try:
            day = utc_now().astimezone(_zone(service.settings)).date().isoformat()
            if getattr(service, "_open_shadow_error_day", None) != day:
                service._open_shadow_error_day = day
                service.run_logs.log("premarket_open_shadow_error", {"error": str(exc)[:300]})
        except Exception:  # noqa: BLE001
            pass
        return None


def _tick(service: Any, started_at: float, deferred: list[str], clock: Any) -> dict | None:
    from app.workflow.schedule import parse_time

    settings = service.settings
    if not (
        bool(getattr(settings, "premarket_open_shadow_enabled", True))
        and bool(getattr(settings, "premarket_deep_scan_enabled", False))
    ):
        return None
    if deferred:
        return None  # live buckets wait for the next tick: never add to their delay
    zone = _zone(settings)
    now_ny = utc_now().astimezone(zone)
    session_date = now_ny.date().isoformat()
    if now_ny.weekday() >= 5 or getattr(service, "_open_shadow_done", None) == session_date:
        return None
    opens = parse_time(str(getattr(settings, "market_open_scan_time_local", "09:35")))
    if now_ny < _at(now_ny, opens) or now_ny >= _at(now_ny, CLOSE_OUT_ENDS):
        return None
    job = float(getattr(settings, "scheduler_job_timeout_seconds", 240) or 240)
    left = started_at + job - POST_SCAN_RESERVE_SECONDS - clock()  # the cadence budget's rule
    if left < MIN_START_SECONDS:
        return None  # only with budget left, and never after the backstop skipped a bucket
    stop_at = clock() + min(left, RUN_SECONDS)
    key = RECORD_KEY.format(date=session_date)
    first = _get_many(service.runtime_state, [key, MARKET_OPEN_KEY])
    record = _json(first.get(key))
    if record.get("status") in FINAL:
        service._open_shadow_done = session_date
        return None
    past_deadline = now_ny >= _at(now_ny, DEADLINE)
    open_done = _local_date(first.get(MARKET_OPEN_KEY), zone) == session_date or not bool(
        getattr(settings, "market_open_scan_enabled", True)
    )
    if not open_done and not past_deadline:
        return None  # the live market-open scan comes first
    if not _resumable(record, session_date):
        if past_deadline:  # never started: the bot was down, or the live scan never completed
            reason = "market_open_scan_not_completed" if not open_done else "not_started"
            return _close(service, key, _new_record(session_date, "missed_deadline", reason))
        record = _start(service, session_date)
        if record["status"] in FINAL:  # the watchlist failed closed
            return _close(service, key, record)
    if not past_deadline:
        record["ticks"] = int(record.get("ticks") or 0) + 1
        _process(service, record, now_ny, stop_at, clock)
    finished = int(record["cursor"]) >= len(record["candidates"])
    if not finished and not past_deadline:
        record["updated_at"] = utc_now().isoformat()
        _save(service.runtime_state, key, record)
        return {"status": "running", "cursor": record["cursor"]}
    _finalize(record, complete=finished)
    return _close(service, key, record)


def _start(service: Any, session_date: str) -> dict[str, Any]:
    """A new record from today's watchlist, or a final one when the watchlist fails closed."""

    from app.broker.etoro_live_mirror import HALTED_KEY, STATE_KEY
    from app.broker.etoro_live_scorecard import SCORECARD_KEY
    from app.performance.strategy_evidence import VERDICTS_KEY
    from app.workflow import premarket_deep_scan as deep

    watchlist_key = deep.WATCHLIST_KEY.format(date=session_date)
    keys = [watchlist_key, VERDICTS_KEY, STATE_KEY, HALTED_KEY, SCORECARD_KEY, TEST_ORDER_KEY]
    values = _get_many(service.runtime_state, keys)
    watchlist = _json(values.get(watchlist_key))
    if not values.get(watchlist_key):
        status = "no_watchlist"
    elif watchlist.get("schema_version") != deep.SCHEMA_VERSION or not isinstance(
        watchlist.get("signals"), list
    ):
        status = "watchlist_unreadable"
    elif watchlist.get("session_date") != session_date:
        status = "watchlist_wrong_date"
    elif watchlist.get("status") == "missed_cutoff":
        status = "watchlist_missed_cutoff"
    elif watchlist.get("status") not in deep.FINAL_STATUSES:
        status = "watchlist_not_final"
    else:
        status = "running"
    record = _new_record(session_date, status)
    if status != "running":
        return record
    record["watchlist_status"] = watchlist.get("status")
    record["watchlist_computed_at"] = watchlist.get("computed_at")
    record["etoro"] = _etoro_snapshot(service.settings, values, session_date)
    record["candidates"] = _candidates(service.settings, watchlist["signals"], values)
    return record


def _new_record(session_date: str, status: str, reason: str | None = None) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "session_date": session_date,
        "status": status,
        "run_id": uuid4().hex,
        "started_at": utc_now().isoformat(),
        "cursor": 0,
        "ticks": 0,
        "candidates": [],
        "results": [],
        "shadow_only": True,
        "rules": RULES,
    }
    if reason:
        record["reason"] = reason
    return record


def _resumable(record: dict[str, Any], session_date: str) -> bool:
    """A record this code wrote today and can resume (anything else starts over)."""

    return (
        record.get("schema_version") == SCHEMA_VERSION
        and record.get("session_date") == session_date
        and record.get("status") == "running"
        and isinstance(record.get("candidates"), list)
        and isinstance(record.get("results"), list)
        and type(record.get("cursor")) is int
        and 0 <= record["cursor"] <= len(record["candidates"])
        and len(record["results"]) == record["cursor"]
        and isinstance(record.get("etoro"), dict)
    )


def _candidates(settings: Any, signals: list, values: dict[str, Any]) -> list[dict[str, Any]]:
    """The top setups an entry at the open could take, with their evidence decided now."""

    from app.broker.etoro_live_mirror import (
        HARD_MAX_STOP_DISTANCE_PCT,
        MIRROR_MIN_EXPECTANCY_R,
        _StrictEvidence,
        evidence_streak_blocker,
        extension_blockers,
        stop_distance_pct,
    )
    from app.performance.strategy_evidence import VERDICTS_KEY, evidence_blocker, passing_hours
    from app.workflow.premarket_deep_scan import _OneKey

    one_key = _OneKey(VERDICTS_KEY, values.get(VERDICTS_KEY))
    cached = _json(values.get(VERDICTS_KEY))
    verdicts = cached.get("verdicts")
    verdicts = verdicts if isinstance(verdicts, dict) else {}
    strict = _StrictEvidence(settings)  # the deep scan's rule: evidence is always required
    now = utc_now()
    picked: list[dict[str, Any]] = []
    for item in signals:
        if not isinstance(item, dict) or not item.get("allowed") or item.get("blacklisted"):
            continue
        if item.get("held_paper") or item.get("pending_proposal"):
            continue  # as the deep scan saw them, before the live bot's entries at the open
        entry, stop, target = (_finite(item.get(k)) for k in ("entry", "stop", "target"))
        if None in (entry, stop, target) or not 0 < stop < entry < target:
            continue
        strategy = str(item.get("strategy") or "")
        if evidence_blocker(strict, one_key, strategy=strategy, timeframe="1d") is not None:
            continue
        edge = evidence_blocker(
            strict,
            one_key,
            strategy=strategy,
            timeframe="1d",
            min_expectancy_r=MIRROR_MIN_EXPECTANCY_R,
        )
        # The 2026-10-18 copy rules whose inputs exist here, read as the mirror reads them: the
        # evidence streak now, and the deep scan's extension of the signal's completed bars
        # (the re-check keeps only a signal on that same bar). Missing inputs refuse the copy.
        streak = evidence_streak_blocker(
            passing_hours(cached, strategy=strategy, timeframe="1d", now=now)
        )
        extension = {"timeframe": "1d", **{k: item.get(k) for k in _EXTENSION_FIELDS}}
        rules = ([streak] if streak else []) + extension_blockers(extension, "1d")
        stop_pct = stop_distance_pct(entry, stop)
        verdict = verdicts.get(f"{strategy}:1d")
        picked.append(
            {
                **{k: item.get(k) for k in ("symbol", "strategy", "spec_key", "kind")},
                "signal_bar_ts": item.get("signal_bar_ts"),
                "entry": entry,
                "stop": stop,
                "target": target,
                "rr": _finite(item.get("rr")),
                "oos_expectancy_r": _finite((verdict or {}).get("oos_expectancy_r")),
                "copy_edge_reason": edge,
                "copy_rule_reasons": rules,
                # The copy rules on the signal's price: only to pick the top before the quote.
                "copy_rules_at_signal": edge is None
                and not rules
                and stop_pct is not None
                and stop_pct <= HARD_MAX_STOP_DISTANCE_PCT,
            }
        )
    picked.sort(
        key=lambda c: rank_key(
            {**c, "etoro_copy_rules_ok": c["copy_rules_at_signal"], "rr_at_quote": c["rr"]}
        )
    )
    return picked[:TOP_ITEMS]


def _etoro_snapshot(settings: Any, values: dict[str, Any], session_date: str) -> dict[str, Any]:
    """The eToro LIVE account as stored (no eToro call), without the copies opened that day."""

    from app.broker.etoro_live_mirror import (
        HALTED_KEY,
        HARD_DAILY_LOSS_STOP_PCT,
        STATE_KEY,
        EtoroLiveMirrorService,
    )
    from app.broker.etoro_live_room import effective_open_symbols
    from app.broker.etoro_live_scorecard import SCORECARD_KEY
    from app.workflow.premarket_deep_scan import _OneKey

    try:  # the mirror's own read (its UTC-day reset included)
        view = SimpleNamespace(state=_OneKey(STATE_KEY, values.get(STATE_KEY)))
        state = EtoroLiveMirrorService._state(view)
    except Exception:  # noqa: BLE001 - an odd stored state reads as unknown
        state = {"open_symbols": []}
    held = set(effective_open_symbols(_Values(values), state))
    zone = _zone(settings)
    cards = _json(values.get(SCORECARD_KEY))
    today = [
        card
        for card in cards.values()
        if isinstance(card, dict) and _local_date(card.get("opened_at"), zone) == session_date
    ]
    live = sorted({str(card.get("symbol") or "").upper() for card in today} - {""})
    cash = _finite(state.get("last_cash"))
    if cash is not None:  # what those copies took from the free cash comes back
        in_flight = [c for c in today if c.get("status") in {"pending_fill", "open"}]
        cash += sum(_finite(card.get("amount_usd")) or 0.0 for card in in_flight)
    equity, start = _finite(state.get("last_equity")), _finite(state.get("day_start_equity"))
    hints = []
    if not bool(getattr(settings, "etoro_live_mirror_enabled", False)):
        hints.append("etoro_live_mirror_disabled")
    if values.get(HALTED_KEY):
        hints.append("etoro_live_mirror_halted")
    if equity is None or equity <= 0:
        hints.append("etoro_live_equity_unknown")
    elif start is not None and start - equity >= start * HARD_DAILY_LOSS_STOP_PCT / 100.0:
        hints.append("etoro_live_daily_loss_stop")
    return {
        "equity": equity,
        "cash": None if cash is None else round(cash, 2),
        "open_symbols": sorted(held - set(live)),
        "live_copies_today": live,
        "trades_today": int(_finite(state.get("trades_today")) or 0),
        "account_hints": hints,
    }


def _process(
    service: Any, record: dict[str, Any], now_ny: datetime, stop_at: float, clock: Any
) -> None:
    """Check candidates from the cursor until the list ends or the tick's budget does."""

    from app.broker.etoro_rate_limit import EToroRateLimitError

    candidates, cache, specs = record["candidates"], {}, None
    record.pop("paused_reason", None)
    while int(record["cursor"]) < len(candidates):
        if stop_at - clock() < 1.0:
            return
        if specs is None:  # the live screener's current daily specs, read once per tick
            listed = service.market_screener._strategy_specs_for_timeframe("1d")
            specs = {f"{spec.name}:1d": spec for spec in listed}
        candidate = candidates[int(record["cursor"])]
        try:
            result = _check(service, record, candidate, specs, cache, now_ny, (stop_at, clock))
        except EToroRateLimitError:
            record["paused_reason"] = "etoro_rate_limit"  # a cooldown: retry on the next tick
            return
        except _NoAnswer:
            return
        record["results"].append(result)
        record["cursor"] = int(record["cursor"]) + 1


def _check(service, record, candidate, specs, cache, now_ny, budget) -> dict[str, Any]:
    """One candidate: the completed-bar re-check, then the live gates at a fresh quote."""

    from app.broker.etoro_live_mirror import (
        HARD_MAX_STOP_DISTANCE_PCT,
        MIN_TRADE_USD,
        EtoroLiveMirrorService,
        stop_distance_pct,
    )
    from app.broker.etoro_rate_limit import EToroRateLimitError
    from app.execution.coordinator import ExecutionCoordinator

    settings = service.settings
    symbol = str(candidate.get("symbol") or "").upper()
    result: dict[str, Any] = {**candidate, "checked_at": utc_now().isoformat()}
    completed = CompletedBarsMarketData(service.market_data, now_ny)
    bars = service.market_screener._bars_for_timeframe("1d")
    try:
        history = _fetch(
            cache,
            ("history", symbol),
            budget,
            settings,
            lambda: completed.get_history(symbol, timeframe="1d", bars=bars, force_refresh=False),
        )
    except (_NoAnswer, EToroRateLimitError):
        raise
    except Exception as exc:  # noqa: BLE001 - this candidate only
        return {**result, "status": "unavailable", "reason": f"history: {exc}"[:200]}
    mismatch = _recheck(service, candidate, specs.get(str(candidate.get("spec_key"))), history)
    if mismatch:
        return {**result, "status": "mismatch", "mismatch": mismatch}
    broker = str(getattr(settings, "broker_for_equities", "alpaca") or "")
    provider = broker if broker in {"alpaca", "etoro"} else None  # as the coordinator asks
    try:
        quote = _fetch(
            cache,
            ("quote", symbol),
            budget,
            settings,
            lambda: service.market_data.get_quote(  # its history fallback: not cached either
                symbol, timeframe="1d", provider=provider, force_refresh=True, write_cache=False
            ),
        )
    except (_NoAnswer, EToroRateLimitError):
        raise
    except Exception as exc:  # noqa: BLE001
        return {**result, "status": "unavailable", "reason": f"quote: {exc}"[:200]}
    entry, stop, target = (float(candidate[k]) for k in ("entry", "stop", "target"))
    price = _finite(
        getattr(quote, "last_execution", None)
        or getattr(quote, "ask", None)
        or getattr(quote, "bid", None)
    )
    view = SimpleNamespace(settings=settings)  # the live checks read only the settings
    gates = list(
        ExecutionCoordinator._quote_validation_reasons(view, quote, expected_broker=broker)
    )
    spread = _spread_bps(quote)
    if spread is not None and spread > float(getattr(settings, "screener_max_spread_bps", 50.0)):
        gates.append("spread_too_wide")
    result["quote"] = {
        "price": price,
        "bid": _finite(getattr(quote, "bid", None)),
        "ask": _finite(getattr(quote, "ask", None)),
        "source": getattr(quote, "source", None),
        "age_seconds": _finite(getattr(quote, "data_age_seconds", None)),
        "timestamp": getattr(quote, "timestamp", None),
    }
    result["spread_bps"] = None if spread is None else round(spread, 2)
    if price is None or price <= 0:
        reasons = gates if "quote_missing" in gates else [*gates, "quote_missing"]
        return {**result, "status": "gate_failed", "gate_reasons": reasons}
    # From the signal's price (its bar's close): the gap to the open. Not a gate here: the order
    # would be priced at this quote, so the coordinator's drift check would see about none.
    drift = abs(price - entry) / entry * 10_000.0
    drift_ok = drift <= float(getattr(settings, "execution_max_entry_drift_bps", 35.0))
    order = SimpleNamespace(side="buy", stop_loss=stop, take_profit=target)
    min_rr = float(getattr(settings, "execution_min_reward_to_risk_at_quote", 1.0) or 0.0)
    gates += ExecutionCoordinator._alpaca_bracket_reasons(
        SimpleNamespace(order=order), price, min_rr
    )
    # The eToro mirror's per-trade copy rules, at the price it would copy at.
    stop_pct = stop_distance_pct(price, stop)
    copy = [candidate["copy_edge_reason"]] if candidate.get("copy_edge_reason") else []
    copy += list(candidate.get("copy_rule_reasons") or [])
    if stop_pct is None:
        copy.append("etoro_live_stop_distance_unknown")
    elif stop_pct > HARD_MAX_STOP_DISTANCE_PCT:
        copy.append("etoro_live_stop_too_wide")
    equity = {"last_equity": record["etoro"].get("equity")}
    amount = EtoroLiveMirrorService._trade_amount(view, equity, stop_pct)  # None: equity unknown
    if amount is not None and amount < MIN_TRADE_USD:
        copy.append("etoro_live_trade_below_minimum")
    result.update(
        entry_at_quote=round(price, 4),
        entry_drift_bps=round(drift, 2),
        drift_from_signal_ok=drift_ok,  # False: left out of drift_gated
        rr_at_quote=round((target - price) / (price - stop), 3) if price > stop else None,
        stop_pct_at_quote=None if stop_pct is None else round(stop_pct, 3),
        gate_reasons=gates,
        etoro_copy_reasons=copy,
        etoro_copy_rules_ok=not copy,
        etoro_amount_usd=amount,
        etoro_risk_usd=round(amount * stop_pct / 100.0, 2) if amount and stop_pct else None,
        status="gate_failed" if gates else "entered",
    )
    return result


def _recheck(service: Any, candidate: dict[str, Any], spec: Any, history: Any) -> list | None:
    """None when the live build gives the same signal on the completed bars, else why not."""

    import pandas as pd

    from app.indicators import precomputed_indicators
    from app.workflow.premarket_deep_scan import _item

    if spec is None:
        return ["spec_unavailable"]  # demoted or routed out since the deep scan
    if history is None or len(history) < 20:
        return ["too_few_completed_bars"]
    last = history.iloc[-1]
    try:
        same_bar = bool(pd.Timestamp(last["timestamp"]) == pd.Timestamp(candidate["signal_bar_ts"]))
    except (KeyError, TypeError, ValueError):
        same_bar = False
    if not same_bar:
        return ["signal_bar_changed"]
    symbol = str(candidate.get("symbol") or "").upper()
    try:
        with precomputed_indicators(history):
            strategy = service.market_screener._build_strategy(spec)
            signal = strategy.generate_signal(history.copy(), symbol)
        item = _item(signal, symbol, spec, str(candidate["spec_key"]), last, None, None, _NEUTRAL)
    except Exception as exc:  # noqa: BLE001 - one strategy: that candidate's mismatch
        return [f"strategy_error: {exc}"[:200]]
    if item is None:
        return ["no_buy_signal"]
    return [k for k in ("entry", "stop", "target", "kind") if item[k] != candidate.get(k)] or None


# The deep scan's item builder needs a context; only its signal fields are compared here.
_NEUTRAL: dict[str, Any] = {
    "verdicts": {},
    "evidence_ok": lambda strategy: False,
    "copy_edge_ok": lambda strategy: False,
    "max_stop_pct": 0.0,
    "held_paper": frozenset(),
    "pending": frozenset(),
    "allowed": frozenset(),
    "blacklisted": frozenset(),
    "etoro_open": frozenset(),
    "etoro_bucket_full": lambda symbol: [],
}


def _fetch(cache: dict, key: tuple, budget: tuple, settings: Any, call: Any) -> Any:
    """One bounded request per symbol and kind per tick. A timeout set by the tick's budget
    (shorter than the market-data timeout) is no answer; a full one is this symbol's error."""

    from app.screener.scan_support import ScanTimeoutError, _bounded_call

    if key in cache:
        return cache[key]
    stop_at, clock = budget
    limit = float(getattr(settings, "screener_market_data_timeout_seconds", 20.0) or 0.0)
    full = limit if limit > 0 else RUN_SECONDS
    timeout = min(full, stop_at - clock())
    if timeout < 1.0:
        raise _NoAnswer
    try:
        cache[key] = _bounded_call(f"open_shadow_{key[0]}_{key[1]}", timeout, call)
    except ScanTimeoutError:
        if timeout < full:
            raise _NoAnswer from None
        raise
    return cache[key]


def rank_key(item: dict[str, Any]) -> tuple[Any, ...]:
    """eToro-copyable first, strict before weak among those, OOS expectancy R, R:R at quote."""

    copyable = bool(item.get("etoro_copy_rules_ok"))
    expectancy = _finite(item.get("oos_expectancy_r"))
    return (
        0 if copyable else 1,
        (0 if item.get("kind") == "strict" else 1) if copyable else 0,
        -expectancy if expectancy is not None else 9.0,  # no verdict ranks last
        -(_finite(item.get("rr_at_quote")) or 0.0),
        str(item.get("symbol")),
        str(item.get("spec_key")),
    )


def _finalize(record: dict[str, Any], *, complete: bool) -> None:
    """Rank the setups that passed and hand out the eToro slots, then the same ranking with the
    entry drift from the signal's price as a gate (``drift_gated``: the other reading)."""

    etoro = record.get("etoro") or {}
    entered = sorted((r for r in record["results"] if r.get("status") == "entered"), key=rank_key)
    for row, mark in _slots(entered, etoro):
        row.update(mark)
    record.update(_entries(entered))
    gated = [r for r in entered if r.get("drift_from_signal_ok") is not False]
    record["drift_gated"] = _entries([{**row, **mark} for row, mark in _slots(gated, etoro)])
    record["status"] = "complete" if complete else "incomplete_at_deadline"


def _slots(rows: list[dict[str, Any]], etoro: dict[str, Any]) -> list[tuple[dict, dict]]:
    """Each row with its rank, slot and room hints. Room is checked against the stored account
    plus the slots already handed out."""

    from app.broker.etoro_live_mirror import (
        CASH_RESERVE_USD,
        HARD_MAX_OPEN_POSITIONS,
        HARD_MAX_TRADES_PER_DAY,
    )
    from app.broker.etoro_live_room import bucket_blockers

    held, cash = set(etoro.get("open_symbols") or []), _finite(etoro.get("cash"))
    slots, taken, out = HARD_MAX_TRADES_PER_DAY, set(), []
    for rank, row in enumerate(sorted(rows, key=rank_key), start=1):
        mark: dict[str, Any] = {"rank": rank}
        out.append((row, mark))
        if row["symbol"] in taken:
            mark["slot"] = "duplicate_symbol"  # one position per symbol
            continue
        taken.add(row["symbol"])
        amount = _finite(row.get("etoro_amount_usd"))
        room = ["etoro_live_symbol_already_open"] if row["symbol"] in held else []
        if len(held) >= HARD_MAX_OPEN_POSITIONS:
            room.append("etoro_live_open_position_cap")
        room += bucket_blockers(row["symbol"], sorted(held))
        if amount and cash is not None and cash - CASH_RESERVE_USD < amount:
            room.append("etoro_live_insufficient_cash")
        mark["etoro_room_hints"] = room
        if row.get("etoro_copy_rules_ok") and not room and slots > 0:
            mark["slot"], slots = "etoro", slots - 1
            held.add(row["symbol"])
            cash = None if cash is None or amount is None else cash - amount
        else:
            mark["slot"] = "paper_only"
            if row.get("etoro_copy_rules_ok") and not room:
                mark["etoro_room_hints"] = ["etoro_daily_slots_taken"]
    return out


def _entries(rows: list[dict[str, Any]]) -> dict[str, list]:
    fields = ("rank", "symbol", "strategy", "kind", "slot", "entry_at_quote", "entry_drift_bps")
    fields += ("stop", "target", "rr_at_quote", "etoro_amount_usd", "oos_expectancy_r")
    entries = [{k: r.get(k) for k in fields} for r in rows if r["slot"] != "duplicate_symbol"]
    return {
        "entries": entries,
        "etoro_slots": [e["symbol"] for e in entries if e["slot"] == "etoro"],
        "paper_only": [e["symbol"] for e in entries if e["slot"] == "paper_only"],
    }


def _close(service: Any, key: str, record: dict[str, Any]) -> dict[str, Any] | None:
    """Save a final record and its one run log, unless another run already closed the day."""

    if _json(service.runtime_state.get(key)).get("status") in FINAL:
        service._open_shadow_done = record["session_date"]
        return None
    record["computed_at"] = record["updated_at"] = utc_now().isoformat()
    _save(service.runtime_state, key, record)
    service._open_shadow_done = record["session_date"]
    results = record.get("results") or []
    gate_failed: dict[str, int] = {}
    for row in (r for r in results if r.get("status") == "gate_failed"):
        for reason in row.get("gate_reasons") or []:
            gate_failed[reason] = gate_failed.get(reason, 0) + 1
    summary = {
        "session_date": record["session_date"],
        "status": record["status"],
        "reason": record.get("reason"),
        "watchlist_status": record.get("watchlist_status"),
        "candidates": len(record.get("candidates") or []),
        "checked": len(results),
        "mismatches": [
            {k: row.get(k) for k in ("symbol", "spec_key", "mismatch")}
            for row in results
            if row.get("status") == "mismatch"
        ],
        "unavailable": sum(1 for row in results if row.get("status") == "unavailable"),
        "gate_failed": gate_failed,
        "entries": [
            {k: e.get(k) for k in ("symbol", "strategy", "kind", "slot", "rr_at_quote")}
            for e in record.get("entries") or []
        ],
        "etoro_slots": record.get("etoro_slots") or [],
        "drift_gated_etoro_slots": (record.get("drift_gated") or {}).get("etoro_slots") or [],
        "etoro_account_hints": (record.get("etoro") or {}).get("account_hints") or [],
        "paused_reason": record.get("paused_reason"),
        "ticks": record.get("ticks"),
        "shadow_only": True,
    }
    service.run_logs.log("premarket_open_shadow_completed", summary)
    return summary


def open_shadow_report(
    runtime_state: Any, db: Any, cards: Any, *, settings: Any = None, days: int = 10
) -> dict[str, Any]:
    """The last ``days`` Phase 3 records next to what the live bot entered those days (paper
    entry orders and eToro copies). Reporting only: a failure is reported, never raised."""

    try:
        return _report(runtime_state, db, cards if isinstance(cards, dict) else {}, settings, days)
    except Exception as exc:  # noqa: BLE001 - the scorecard page never fails for this
        return {"error": f"open_shadow_unreadable: {exc}"[:200], "rules": RULES}


def _report(runtime_state: Any, db: Any, cards: dict, settings: Any, days: int) -> dict[str, Any]:
    zone = _zone(settings)
    today = utc_now().astimezone(zone).date()
    dates = [(today - timedelta(days=i)).isoformat() for i in range(max(int(days), 1))]
    raw = _get_many(runtime_state, [RECORD_KEY.format(date=day) for day in dates])
    records = {day: _json(raw.get(RECORD_KEY.format(date=day))) for day in dates}
    records = {day: record for day, record in records.items() if record}
    if not records:
        return {"days": [], "rules": RULES}
    start = datetime.combine(date.fromisoformat(min(records)), datetime.min.time(), zone)
    end = datetime.combine(date.fromisoformat(max(records)), datetime.min.time(), zone)
    paper = _paper_entries(db, start, end + timedelta(days=1), zone)
    etoro: dict[str, list[dict[str, Any]]] = {}
    for card in cards.values():
        day = _local_date(card.get("opened_at"), zone) if isinstance(card, dict) else None
        if day in records:
            fields = ("symbol", "strategy_name", "opened_at", "amount_usd", "status")
            etoro.setdefault(day, []).append({k: card.get(k) for k in fields})
    out = []
    for day in sorted(records, reverse=True):
        record = records[day]
        entries = [e for e in record.get("entries") or [] if isinstance(e, dict)]
        phase3 = {str(e.get("symbol")) for e in entries}
        live = {str(e["symbol"]) for e in paper.get(day, [])}
        live |= {str(card.get("symbol") or "").upper() for card in etoro.get(day, [])}
        statuses = Counter(
            r.get("status") for r in record.get("results") or [] if isinstance(r, dict)
        )
        gated = record.get("drift_gated") if isinstance(record.get("drift_gated"), dict) else {}
        out.append(
            {
                "session_date": day,
                "status": record.get("status"),
                "reason": record.get("reason"),
                "computed_at": record.get("computed_at"),
                "phase3_entries": entries,
                "phase3_etoro": record.get("etoro_slots") or [],
                "phase3_paper_only": record.get("paper_only") or [],
                "phase3_drift_gated": {
                    k: gated.get(k) or [] for k in ("etoro_slots", "paper_only")
                },
                "live_paper_entries": paper.get(day, []),
                "live_etoro_entries": etoro.get(day, []),
                "same_symbols": sorted(phase3 & live),
                "phase3_only": sorted(phase3 - live),
                "live_only": sorted(live - phase3),
                "mismatches": statuses["mismatch"],
                "unavailable": statuses["unavailable"],
                "gate_failed": statuses["gate_failed"],
                "etoro_account_hints": (record.get("etoro") or {}).get("account_hints") or [],
            }
        )
    return {"days": out, "rules": RULES}


def _paper_entries(db: Any, start: datetime, end: datetime, zone: Any) -> dict[str, list[dict]]:
    """The live bot's entry orders by New York date (failed, blocked and cancelled left out)."""

    with db.connect() as connection:
        rows = connection.execute(
            "SELECT strategy_name, status, request_json, created_at FROM executions "
            "WHERE created_at >= ? AND created_at < ? ORDER BY created_at",
            (start.astimezone(UTC).isoformat(), end.astimezone(UTC).isoformat()),
        ).fetchall()
    out: dict[str, list[dict]] = {}
    for row in rows:
        if str(row["status"] or "").lower() in {"failed", "blocked", "canceled", "cancelled"}:
            continue
        request = _json(row["request_json"])
        if str(request.get("side") or "buy").lower() != "buy":
            continue
        metadata = request.get("metadata") if isinstance(request.get("metadata"), dict) else {}
        out.setdefault(str(_local_date(row["created_at"], zone)), []).append(
            {
                "symbol": str(request.get("symbol") or "").upper(),
                "strategy": row["strategy_name"] or request.get("strategy_name"),
                "timeframe": metadata.get("timeframe"),
                "at": row["created_at"],
                "status": row["status"],
            }
        )
    return out


class _Values:
    """A read-only runtime_state over values already read (no database call)."""

    def __init__(self, values: dict[str, Any]) -> None:
        self._values = values

    def get(self, key: str, default: Any = None) -> Any:
        return self._values.get(key, default)


def _get_many(runtime_state: Any, keys: list[str]) -> dict[str, Any]:
    """One database round trip where the repository can (the fakes read key by key)."""

    getter = getattr(runtime_state, "get_many", None)
    if callable(getter):
        return dict(getter(keys))
    return {key: runtime_state.get(key) for key in keys}


def _spread_bps(quote: Any) -> float | None:
    """The screener's spread: (ask - bid) / mid, in bps (None without both sides)."""

    bid, ask = _finite(getattr(quote, "bid", None)), _finite(getattr(quote, "ask", None))
    if bid is None or ask is None:
        return None
    mid = (bid + ask) / 2.0
    return (ask - bid) / mid * 10_000.0 if mid > 0 else None


def _local_date(value: Any, zone: Any) -> str | None:
    """An aware ISO timestamp's date in ``zone``; None when missing or unreadable."""

    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        return None
    return moment.astimezone(zone).date().isoformat()


def _at(now: datetime, hour_minute: tuple[int, int]) -> datetime:
    return now.replace(hour=hour_minute[0], minute=hour_minute[1], second=0, microsecond=0)


def _zone(settings: Any) -> Any:
    from app.workflow.premarket_deep_scan import _zone as deep_zone

    return deep_zone(settings)


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _json(raw: Any) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _save(runtime_state: Any, key: str, record: dict[str, Any]) -> None:
    from app.workflow.premarket_deep_scan import _json_safe

    runtime_state.set(key, json.dumps(_json_safe(record), allow_nan=False, default=str))
