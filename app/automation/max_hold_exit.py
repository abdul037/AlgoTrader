"""Close a daily paper position once its signal's hold has run out (operator 2026-11-07).

The backtest engine honours a signal's ``metadata['max_hold_bars']``: once that many bars have
passed since the entry bar it closes the trade at that bar's open, before any stop or target.
Paper did not, so overnight_drift (``max_hold_bars=1``: out at the next session's open) would
have held until its bracket's stop or target, a different trade from its evidence. Operator
2026-11-07: "build the overnight_drift one-day hold for paper".

Rule, for a 1d entry whose signal carries ``max_hold_bars`` = N: while the regular session is
open (Alpaca's clock), once N or more sessions have started since the New York date the entry
filled, the bracket legs are cancelled and the position is closed at market. So an entry filled
on day T with N=1 closes at the first scheduler tick after day T+1's open, as the backtest
closes at the open of the bar after its entry bar. Sessions are counted as weekdays: the clock
is closed on a market holiday, so nothing closes then, and N=1 is exact; a holiday inside a
longer hold counts as a session.

Paper only (``execution_mode=paper``, real trading off), like the intraday close. It is a
strategy exit, not a safety flatten, so it writes no ``paper_safety_flatten`` marker: the eToro
exit copy follows it once paper reads flat. Reconciliation books the exit through the standalone
sell path. ``paper_max_hold_exit_enabled`` turns it off.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from app.utils.time import utc_now

NEW_YORK = ZoneInfo("America/New_York")
MAX_HOLD_TIMEFRAMES = frozenset({"1d"})
_QTY_HELD = ("insufficient qty", "held_for_orders")
_OPEN_LEG_STATUSES = {"new", "held", "accepted", "pending_new", "partially_filled"}


def max_hold_bars(request: dict[str, Any]) -> int | None:
    """The signal's ``max_hold_bars`` from an execution's request metadata (None: no time stop)."""

    raw = dict(request.get("metadata") or {}).get("max_hold_bars")
    if raw in (None, "") or isinstance(raw, bool):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def sessions_since(entry_day: date, today: date) -> int:
    """Weekday sessions after ``entry_day`` up to and including ``today``."""

    count = 0
    day = entry_day + timedelta(days=1)
    while day <= today:
        if day.weekday() < 5:
            count += 1
        day += timedelta(days=1)
    return count


def _parse(stamp: Any) -> datetime | None:
    if not stamp:
        return None
    try:
        value = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _open_entries(executions: Any) -> dict[str, Any]:
    """Symbol -> newest filled entry with no filled exit (any timeframe), as the intraday close."""

    found: dict[str, Any] = {}
    for execution in executions.list(limit=200):
        request = dict(execution.request_payload or {})
        symbol = str(request.get("symbol") or "").upper()
        payload = dict(dict(execution.response_payload or {}).get("broker_execution") or {})
        if not symbol or float(payload.get("filled_qty") or 0.0) <= 0:
            continue
        if float(getattr(execution, "realized_pnl_usd", 0.0) or 0.0) != 0.0:
            continue
        if any(str(leg.get("status") or "").lower() == "filled" for leg in payload.get("legs") or []):
            continue
        if symbol not in found or str(execution.created_at) > str(found[symbol].created_at):
            found[symbol] = execution
    return found


def close_held_past_max_hold(host: Any, *, now: datetime | None = None) -> list[str]:
    """Return the symbols whose close was submitted (or is already in flight). Never raises: it
    runs on every scheduler tick, and a failure leaves the position on its bracket."""

    try:
        return _close_due(host, now=now)
    except Exception as exc:  # noqa: BLE001 - a broker or store hiccup must not stop the tick
        with contextlib.suppress(Exception):
            host.run_logs.log("max_hold_close_error", {"error": str(exc)})
        return []


def _close_due(host: Any, *, now: datetime | None) -> list[str]:
    settings = host.settings
    reconciliation = getattr(host, "reconciliation", None)
    if not bool(getattr(settings, "paper_max_hold_exit_enabled", True)) or reconciliation is None:
        return []
    if str(getattr(settings, "execution_mode", "paper")) != "paper" or bool(getattr(settings, "enable_real_trading", False)):
        return []
    alpaca = getattr(reconciliation, "alpaca", None)
    if alpaca is None or not hasattr(alpaca, "close_position") or not hasattr(alpaca, "market_clock"):
        return []
    now = now or utc_now()
    try:
        is_open, _ = alpaca.market_clock()
    except Exception:  # noqa: BLE001 - no clock, no action; the bracket still protects
        return []
    if not is_open:
        return []

    today = now.astimezone(NEW_YORK).date()
    due: list[tuple[str, Any, int, int]] = []
    for symbol, execution in _open_entries(reconciliation.executions).items():
        request = dict(execution.request_payload or {})
        timeframe = str(dict(request.get("metadata") or {}).get("timeframe") or "").lower()
        hold = max_hold_bars(request)
        if timeframe not in MAX_HOLD_TIMEFRAMES or hold is None:
            continue
        payload = dict(dict(execution.response_payload or {}).get("broker_execution") or {})
        filled = _parse(payload.get("filled_at")) or _parse(getattr(execution, "created_at", None))
        if filled is None:
            continue
        held = sessions_since(filled.astimezone(NEW_YORK).date(), today)
        if held >= hold:
            due.append((symbol, execution, hold, held))
    if not due:
        return []

    held_symbols = {str(p.symbol or "").upper() for p in alpaca.get_portfolio().positions}
    closed: list[str] = []
    for symbol, execution, hold, held in due:
        if symbol not in held_symbols:
            continue
        legs = dict(dict(execution.response_payload or {}).get("broker_execution") or {}).get("legs") or []
        for leg in legs:
            if str(leg.get("status") or "").lower() in _OPEN_LEG_STATUSES:
                alpaca.cancel_order(str(leg.get("broker_order_id") or ""))
        try:
            response = alpaca.close_position(symbol)
        except Exception as exc:  # noqa: BLE001 - legs still cancelling: retry next tick
            message = str(exc).lower()
            event = "max_hold_close_deferred" if any(m in message for m in _QTY_HELD) else "max_hold_close_failed"
            host.run_logs.log(event, {"symbol": symbol, "execution_id": execution.id, "error": str(exc)})
            if event == "max_hold_close_deferred":
                closed.append(symbol)
            continue
        host.run_logs.log(
            "paper_position_closed_max_hold",
            {
                "symbol": symbol,
                "execution_id": execution.id,
                "strategy_name": getattr(execution, "strategy_name", None),
                "max_hold_bars": hold,
                "sessions_held": held,
                "broker_order_id": str(getattr(response, "order_id", "") or ""),
            },
        )
        closed.append(symbol)
    return closed


__all__ = ["MAX_HOLD_TIMEFRAMES", "close_held_past_max_hold", "max_hold_bars", "sessions_since"]
