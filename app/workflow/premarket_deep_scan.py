"""Shadow pre-market deep scan (operator-approved 2026-10-10, phase 1 -- SHADOW ONLY).

The 08:30 ET ``premarket_scan`` bucket only ever reached ~7 of the same first 10 symbols
with a rotating batch of 6 specs (mostly 1h specs that cannot pass the evidence gate), and
found 0 candidates from 2026-09-25 to 10-09. When ``premarket_deep_scan_enabled`` is set,
this scan runs in that bucket INSTEAD of the old one: every allowed equity
(``resolve_universe``) against every daily strategy (except pairs_stat_arb, whose hedge leg
fetches its own data), each built exactly as the live screener builds it, on the last
COMPLETED daily bar -- the bar the backtests decide on (signal on bar N's close, fill at bar
N+1's open).

It writes one ranked watchlist to ``runtime_state['premarket:watchlist:<NY date>']``, the
bucket's coverage record and one ``premarket_deep_scan_completed`` run log. It never creates
proposals, orders, scan decisions, signal states, alerts, tracked signals or learning rows,
and never touches the eToro mirror. Because the old 08:30 scan does not run while the flag is
on, what it would have produced is not produced: alerts, tracked signals, the scan decisions
later scans use for repeat suppression and the weak-valid daily cap, and auto-proposals. It
produced none of these from 09-25 to 10-09. ``tradeable`` is a pre-filter, not a promise:
at the open the live screener's scores and every risk gate still decide.

It is resumable: a scan stopped by its deadline saves a cursor and the bucket stays due; at
the cutoff (09:20 ET by default, never later than the 09:30 open) it finishes with what it
has.
"""

from __future__ import annotations

import json
import math
import time
from datetime import datetime
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.models.workflow import WorkflowTaskResponse
from app.utils.time import utc_now

WATCHLIST_KEY = "premarket:watchlist:{date}"
COVERAGE_KEY = "workflow:premarket_scan:last_scan_coverage"
SCHEMA_VERSION = 1
EXCLUDED_SPECS = frozenset({"pairs_stat_arb"})
TASK = "premarket_scan"
FINAL_STATUSES = frozenset({"complete", "incomplete_at_cutoff", "missed_cutoff"})
DEFAULT_CUTOFF = (9, 20)
LATEST_CUTOFF = (9, 30)  # the session opens: a shadow scan never runs into it


def completed_daily_bars(frame: Any, now_ny: datetime) -> Any:
    """Drop daily bars of today's session or later (a forming bar).

    Daily bars are stamped at the start of their session date: Alpaca and yfinance at New
    York midnight (04:00Z in EDT, 05:00Z in EST), other feeds at 00:00Z or at the 13:30Z
    open. All of these fall on the session date in UTC, so the UTC date is the session date.
    (The New York date would put a 00:00Z bar on the day before and keep today's bar.)"""

    if frame is None or len(frame) == 0 or "timestamp" not in frame:
        return frame
    import pandas as pd

    session = pd.to_datetime(frame["timestamp"], utc=True).dt.date
    keep = session < now_ny.date()
    return frame.loc[keep.values].reset_index(drop=True)


def session_date_now(settings: Any) -> str:
    return utc_now().astimezone(_zone(settings)).date().isoformat()


def load_watchlist(runtime_state: Any, session_date: str) -> dict[str, Any] | None:
    return _load(runtime_state, WATCHLIST_KEY.format(date=session_date))


def rank_key(item: dict[str, Any]) -> tuple[Any, ...]:
    """Tradeable first, strict before weak, then backtest expectancy, then R:R, then name."""

    expectancy = item.get("oos_expectancy_r")
    return (
        0 if item.get("tradeable") else 1,
        0 if item.get("kind") == "strict" else 1,
        -float(expectancy) if expectancy is not None else 9.0,  # no verdict ranks last
        -float(item.get("rr") or 0.0),
        str(item.get("symbol")),
        str(item.get("spec_key")),
    )


def run_premarket_deep_scan(
    service: Any, *, state_key: str, force_refresh: bool, stop: Any | None = None
) -> WorkflowTaskResponse:
    settings = service.settings
    zone = _zone(settings)
    now_ny = utc_now().astimezone(zone)
    session_date = now_ny.date().isoformat()
    key = WATCHLIST_KEY.format(date=session_date)
    cutoff = _cutoff(now_ny, settings)
    watchlist = _load(service.runtime_state, key) or {}
    if watchlist.get("status") in FINAL_STATUSES:
        service.runtime_state.set(state_key, utc_now().isoformat())
        return _response("skipped", f"Pre-market watchlist for {session_date} already done.")
    if now_ny >= cutoff and not watchlist:
        # e.g. the bot was down at 08:30: record the missed day, never run into the session
        missed = _new_watchlist(session_date, [], [])
        missed.update(status="missed_cutoff", computed_at=utc_now().isoformat())
        missed.update(items=[], shadow_items=[])
        _save(service, key, missed)
        service.runtime_state.set(state_key, utc_now().isoformat())
        service.run_logs.log(
            "premarket_deep_scan_skipped", {"session_date": session_date, "reason": "past_cutoff"}
        )
        return _response("ok", "Pre-market deep scan skipped: past the cutoff.")

    from app.broker import crypto as crypto_symbols
    from app.universe import resolve_universe
    from app.workflow.cadence_budget import ScanDeadline

    if stop is None:  # a manual run or the legacy budget: bounded like the old scan's batch
        limit = float(getattr(settings, "screener_batch_deadline_seconds", 120.0) or 120.0)
        stop = ScanDeadline(time.monotonic() + max(limit, 30.0))
    specs_by_key = {
        f"{spec.name}:1d": spec
        for spec in service.market_screener._strategy_specs_for_timeframe("1d")
        if str(spec.name).lower() not in EXCLUDED_SPECS
    }
    if not watchlist:
        symbols = [s for s in resolve_universe(settings) if not crypto_symbols.is_crypto_symbol(s)]
        watchlist = _new_watchlist(session_date, symbols, list(specs_by_key))
    watchlist["run_id"] = uuid4().hex  # each save checks no other run got further
    watchlist.pop("paused_reason", None)
    for spec_key in watchlist["specs"]:  # demoted or regime-routed out since the first run
        note = f"spec_unavailable_on_resume: {spec_key}"
        if spec_key not in specs_by_key and note not in watchlist["errors"]:
            watchlist["errors"].append(note)
    context = _prefetch(service)
    started = utc_now()
    universe = list(watchlist.get("universe") or [])
    while int(watchlist["cursor"]) < len(universe):
        if now_ny >= cutoff or stop.is_set():
            break
        symbol = universe[int(watchlist["cursor"])]
        if _scan_symbol(service, watchlist, symbol, specs_by_key, context, now_ny, force_refresh):
            watchlist["paused_reason"] = "etoro_rate_limit"  # retry this symbol next tick
            break
        watchlist["cursor"] = int(watchlist["cursor"]) + 1
        now_ny = utc_now().astimezone(zone)

    finished = int(watchlist["cursor"]) >= len(universe)
    if not finished and utc_now().astimezone(zone) < cutoff:
        watchlist["status"] = "partial"  # the bucket stays due; the next tick resumes here
        if _ahead_of(service, key, watchlist) is None:
            _save(service, key, watchlist)
            _record_coverage(service, watchlist)
        return _response(
            "ok", f"Pre-market deep scan paused at {watchlist['cursor']}/{len(universe)}."
        )

    ahead = _ahead_of(service, key, watchlist)
    if ahead is not None and ahead.get("status") in FINAL_STATUSES:
        service.runtime_state.set(state_key, utc_now().isoformat())
        return _response("skipped", f"Pre-market watchlist for {session_date} already done.")
    if ahead is not None:  # another run got further: finish its list instead of ours
        watchlist, universe = ahead, list(ahead.get("universe") or [])
        finished = int(watchlist.get("cursor") or 0) >= len(universe)
    _finalize(watchlist, settings, complete=finished)
    _save(service, key, watchlist)
    _record_coverage(service, watchlist)
    service.runtime_state.set(state_key, utc_now().isoformat())
    top = [
        {k: item.get(k) for k in ("symbol", "strategy", "kind", "rr", "oos_expectancy_r")}
        for item in watchlist["items"][:5]
    ]
    service.run_logs.log(
        "premarket_deep_scan_completed",
        {
            "session_date": session_date,
            "status": watchlist["status"],
            "symbols": len(universe),
            "specs": len(watchlist.get("specs") or []),
            "evaluated_runs": watchlist["evaluated_runs"],
            "signals": len(watchlist.get("signals") or []),
            "tradeable_items": len(watchlist["items"]),
            "seconds_this_run": round((utc_now() - started).total_seconds(), 1),
            "errors": len(watchlist.get("errors") or []),
            "top": top,
            "shadow_only": True,
        },
    )
    _maybe_notify(service, watchlist, top)
    return _response(
        "ok",
        f"Pre-market deep scan {watchlist['status']}: {len(watchlist['items'])} tradeable setups "
        f"from {watchlist['evaluated_runs']} checks (shadow only, nothing traded).",
        candidates=len(watchlist["items"]),
    )


def _new_watchlist(session_date: str, symbols: list[str], spec_keys: list[str]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "session_date": session_date,
        "started_at": utc_now().isoformat(),
        "status": "running",
        "cursor": 0,
        "universe": symbols,
        "specs": spec_keys,
        "evaluated_runs": 0,
        "signals": [],
        "errors": [],
    }


def _scan_symbol(service, watchlist, symbol, specs_by_key, context, now_ny, force_refresh) -> bool:
    """Scan one symbol into the watchlist; True when eToro's rate limit stopped the fetch."""

    from app.broker.etoro_rate_limit import EToroRateLimitError
    from app.indicators import precomputed_indicators
    from app.risk.sectors import correlation_bucket_for_symbol
    from app.screener.scan_support import _bounded_call

    screener = service.market_screener
    timeout = float(getattr(service.settings, "screener_market_data_timeout_seconds", 20.0) or 0.0)
    try:
        history = _bounded_call(
            f"{symbol}_1d_history",
            timeout,
            service.market_data.get_history,
            symbol,
            timeframe="1d",
            bars=screener._bars_for_timeframe("1d"),
            force_refresh=force_refresh,
        )
        history = completed_daily_bars(history, now_ny)
    except EToroRateLimitError:
        return True  # in a cooldown every fetch fails at once: never burn the universe on it
    except Exception as exc:  # noqa: BLE001 - one symbol never stops the scan
        watchlist["errors"].append(f"{symbol}: history: {exc}"[:200])
        return False
    if history is None or len(history) < 20:
        watchlist["errors"].append(f"{symbol}: too_few_completed_bars")
        return False
    last = history.iloc[-1]
    prior_volume = _finite(history["volume"].iloc[-21:-1].mean())
    last_volume = _finite(last["volume"])
    relative_volume = (
        round(last_volume / prior_volume, 3) if prior_volume and last_volume is not None else None
    )
    bucket = correlation_bucket_for_symbol(symbol)
    with precomputed_indicators(history):
        for spec_key in list(watchlist.get("specs") or []):
            spec = specs_by_key.get(spec_key)
            if spec is None:
                continue
            watchlist["evaluated_runs"] = int(watchlist["evaluated_runs"]) + 1
            try:
                signal = screener._build_strategy(spec).generate_signal(history.copy(), symbol)
            except Exception as exc:  # noqa: BLE001 - one strategy never stops the scan
                watchlist["errors"].append(f"{symbol} {spec_key}: {exc}"[:200])
                continue
            item = _item(signal, symbol, spec, spec_key, last, relative_volume, bucket, context)
            if item is not None:
                watchlist["signals"].append(item)
    return False


def _item(signal, symbol, spec, spec_key, last, relative_volume, bucket, context) -> dict | None:
    if signal is None or str(getattr(signal.action, "value", signal.action)).lower() != "buy":
        return None
    price, stop, target = (_finite(v) for v in (signal.price, signal.stop_loss, signal.take_profit))
    if price is None or stop is None or target is None or not (0 < stop < price < target):
        return None
    metadata = dict(signal.metadata or {})
    weak = (
        metadata.get("source") == "supervised_weak_valid"
        or metadata.get("signal_classification") == "supervised_weak_valid"
        or bool(metadata.get("weak_signal_kind"))
    )
    verdict = context["verdicts"].get(spec_key) or {}
    evidence_passing = context["evidence_ok"](str(spec.name))
    held = symbol in context["held_paper"]
    pending = symbol in context["pending"]
    allowed = symbol in context["allowed"]
    blacklisted = symbol in context["blacklisted"]
    return {
        "symbol": symbol,
        "strategy": str(spec.name),
        "spec_key": spec_key,
        "kind": "weak" if weak else "strict",
        "weak_signal_kind": metadata.get("weak_signal_kind"),
        "signal_bar_ts": str(last["timestamp"]),
        "close": _finite(last["close"], 4),
        "entry": round(price, 4),
        "stop": round(stop, 4),
        "target": round(target, 4),
        "rr": round((target - price) / (price - stop), 3),
        "relative_volume": relative_volume,
        "evidence_passing": evidence_passing,
        "oos_expectancy_r": _finite(verdict.get("oos_expectancy_r")),
        "oos_trades": verdict.get("oos_trades"),
        "holdout_trades": verdict.get("holdout_trades"),
        "correlation_bucket": bucket,
        "held_paper": held,
        "pending_proposal": pending,
        "etoro_symbol_open": symbol in context["etoro_open"],
        "etoro_bucket_full": bool(context["etoro_bucket_full"](symbol)),
        "allowed": allowed,
        "blacklisted": blacklisted,
        # Weak signals never auto-trade (they need the operator's approval).
        "tradeable": bool(
            allowed
            and not blacklisted
            and evidence_passing
            and not weak
            and not held
            and not pending
        ),
    }


def _prefetch(service: Any) -> dict[str, Any]:
    """Everything the items need, read once (no per-run database calls)."""

    from app.broker.etoro_live_mirror import STATE_KEY, _StrictEvidence
    from app.broker.etoro_live_room import bucket_blockers, effective_open_symbols
    from app.models.approval import ApprovalStatus
    from app.performance.strategy_evidence import VERDICTS_KEY, evidence_blocker

    raw_verdicts = service.runtime_state.get(VERDICTS_KEY)
    try:
        verdicts = (json.loads(raw_verdicts or "{}").get("verdicts")) or {}
    except (TypeError, ValueError, AttributeError):
        verdicts = {}
    one_key = _OneKey(VERDICTS_KEY, raw_verdicts)
    settings = service.settings
    strict = _StrictEvidence(settings)  # the eToro mirror's rule: evidence is always required

    def evidence_ok(strategy: str) -> bool:  # the live gate itself, on the one cached read
        return evidence_blocker(strict, one_key, strategy=strategy, timeframe="1d") is None

    reconciliation = getattr(service, "reconciliation", None)
    held = _symbols(
        lambda: getattr(reconciliation, "broker_positions", None).list_active(),
        lambda row: row.get("symbol"),
    )
    blacklisted = _symbols(
        lambda: getattr(reconciliation, "safety", None).list_blacklist(),
        lambda row: row.get("symbol"),
    )
    pending: set[str] = set()
    try:  # the repository, not list_proposals(): that one writes expiries on read
        proposals = getattr(getattr(service, "proposal_service", None), "proposals", None)
        for status in (ApprovalStatus.PENDING, ApprovalStatus.APPROVED) if proposals else ():
            pending |= {
                p.order.symbol.upper()
                for p in proposals.list(status=status)
                if datetime.fromisoformat(p.expires_at) > utc_now()
            }
    except Exception:  # noqa: BLE001 - flags only
        pending = set()
    try:
        etoro_state = json.loads(service.runtime_state.get(STATE_KEY) or "{}")
        etoro_open = effective_open_symbols(service.runtime_state, etoro_state)
    except Exception:  # noqa: BLE001 - hints only
        etoro_open = []
    blocked = {str(s).upper() for s in getattr(settings, "blocked_instruments", []) or []}
    allowed = {str(s).upper() for s in getattr(settings, "allowed_instruments", []) or []} - blocked
    return {
        "allowed": allowed,  # the instrument resolver refuses anything else
        "blacklisted": blacklisted,
        "verdicts": verdicts,
        "evidence_ok": evidence_ok,
        "held_paper": held,
        "pending": pending,
        "etoro_open": set(etoro_open),
        "etoro_bucket_full": lambda symbol: bucket_blockers(symbol, etoro_open),
    }


def _symbols(rows: Any, symbol_of: Any) -> set[str]:
    try:
        return {str(symbol_of(row) or "").upper() for row in rows() or []} - {""}
    except Exception:  # noqa: BLE001 - flags only; a missing repository reads as none
        return set()


def _finalize(watchlist: dict[str, Any], settings: Any, *, complete: bool) -> None:
    signals = sorted(watchlist.get("signals") or [], key=rank_key)
    max_items = max(int(getattr(settings, "premarket_deep_scan_max_items", 25) or 25), 1)
    watchlist["items"] = [s for s in signals if s.get("tradeable")][:max_items]
    watchlist["shadow_items"] = [s for s in signals if not s.get("tradeable")][:25]
    watchlist["status"] = "complete" if complete else "incomplete_at_cutoff"
    watchlist["computed_at"] = utc_now().isoformat()
    watchlist["errors"] = list(watchlist.get("errors") or [])[:20]


def _maybe_notify(service: Any, watchlist: dict[str, Any], top: list[dict[str, Any]]) -> None:
    if not bool(getattr(service.settings, "premarket_deep_scan_notify", False)):
        return
    lines = [f"SHADOW pre-market setups {watchlist['session_date']} (nothing traded):"]
    lines += [f"- {t['symbol']} {t['strategy']} ({t['kind']}) R:R {t['rr']}" for t in top]
    try:
        service.notifier.send_text("\n".join(lines))
    except Exception:  # noqa: BLE001 - optional
        return


def _ahead_of(service: Any, key: str, watchlist: dict[str, Any]) -> dict[str, Any] | None:
    """The stored watchlist when another run finished it or got further (else None).

    Runs normally never overlap (the workflow lock), but a manual run can slip past the
    lock's read-then-write; a save must never replace more progress with less."""

    stored = _load(service.runtime_state, key)
    if not stored or stored.get("run_id") == watchlist.get("run_id"):
        return None
    if stored.get("status") in FINAL_STATUSES:
        return stored
    if int(stored.get("cursor") or 0) > int(watchlist.get("cursor") or 0):
        return stored
    return None


def _record_coverage(service: Any, watchlist: dict[str, Any]) -> None:
    """The bucket's coverage record (``/workflow/health``), in the old scan's field names."""

    specs, universe = watchlist.get("specs") or [], watchlist.get("universe") or []
    try:
        service.runtime_state.set(
            COVERAGE_KEY,
            json.dumps(
                {
                    "recorded_at": utc_now().isoformat(),
                    "mode": "premarket_deep_scan_shadow",
                    "status": watchlist.get("status"),
                    "timeframes": ["1d"],
                    "specs_requested": len(specs),
                    "symbols_requested": len(universe),
                    "symbols_evaluated": int(watchlist.get("cursor") or 0),
                    "expected_strategy_runs": len(specs) * len(universe),
                    "evaluated_strategy_runs": int(watchlist.get("evaluated_runs") or 0),
                    "deadline_exceeded": watchlist.get("status") != "complete",
                    "candidates_found": len(watchlist.get("signals") or []),
                    "tradeable_found": len(watchlist.get("items") or []),
                    "proposals_created": 0,
                }
            ),
        )
    except Exception:  # noqa: BLE001 - coverage state must not break the scan
        return


def _save(service: Any, key: str, watchlist: dict[str, Any]) -> None:
    watchlist["updated_at"] = utc_now().isoformat()
    service.runtime_state.set(key, json.dumps(_json_safe(watchlist), allow_nan=False, default=str))


def _load(runtime_state: Any, key: str) -> dict[str, Any] | None:
    try:
        value = json.loads(runtime_state.get(key) or "null")
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None  # e.g. a NaN expectancy in a verdict
    return value


def _finite(value: Any, digits: int | None = None) -> float | None:
    """A plain float, or None for missing / NaN / infinite (the watchlist must stay valid JSON:
    the scorecard casts it to jsonb, which rejects NaN and Infinity)."""

    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, digits) if digits is not None else number


def _cutoff(now_ny: datetime, settings: Any) -> datetime:
    """Today's cutoff: ``premarket_deep_scan_cutoff_local`` ("HH:MM"), never after 09:30.

    A mistyped value falls back to 09:20 instead of failing the bucket every tick."""

    hour, minute = DEFAULT_CUTOFF
    raw = str(getattr(settings, "premarket_deep_scan_cutoff_local", "") or "").strip()
    try:
        parts = [int(part) for part in raw.split(":")]
    except ValueError:
        parts = []
    if len(parts) in (2, 3) and 0 <= parts[0] <= 23 and 0 <= parts[1] <= 59:
        hour, minute = parts[0], parts[1]
    hour, minute = min((hour, minute), LATEST_CUTOFF)
    return now_ny.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _zone(settings: Any) -> ZoneInfo:
    return ZoneInfo(str(getattr(settings, "schedule_timezone", "America/New_York")))


def _response(status: str, detail: str, *, candidates: int = 0) -> WorkflowTaskResponse:
    return WorkflowTaskResponse(
        task=TASK, status=status, detail=detail, skipped=status == "skipped", candidates=candidates
    )


class _OneKey:
    """A read-only runtime_state holding one key, so evidence_blocker runs without a DB call."""

    def __init__(self, key: str, value: Any) -> None:
        self._key, self._value = key, value

    def get(self, key: str, default: Any = None) -> Any:
        return self._value if key == self._key else default
