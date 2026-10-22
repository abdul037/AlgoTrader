"""Close intraday-strategy positions before the bell (paper only).

Review Team 2026-10-02: positions opened by 5m/15m strategies were carried
overnight with intraday-sized stops (INTC for two days, COST overnight), taking
gap risk those strategies never priced. In the last minutes of the session the
scheduler cancels the bracket of every open position whose entry came from an
intraday timeframe and closes it at market. Daily-or-longer (swing) positions
keep their GTC brackets. Reconciliation books the exit through the standalone
sell path, so the loss gates see it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.utils.time import utc_now

INTRADAY_TIMEFRAMES = frozenset({"1m", "2m", "3m", "5m", "10m", "15m", "30m", "1h"})
_QTY_HELD = ("insufficient qty", "held_for_orders")


def _minutes_to_close(alpaca: Any, now: datetime) -> float | None:
    clock_fn = getattr(alpaca, "market_clock", None)
    if clock_fn is None:
        return None
    is_open, next_close = clock_fn()
    if not is_open or next_close is None:
        return None
    next_close = next_close if next_close.tzinfo else next_close.replace(tzinfo=UTC)
    return (next_close - now).total_seconds() / 60.0


def _open_intraday_executions(executions: Any) -> dict[str, Any]:
    """Symbol -> newest filled long entry from an intraday timeframe with no filled exit."""

    found: dict[str, Any] = {}
    for execution in executions.list(limit=200):
        request = dict(execution.request_payload or {})
        timeframe = str(dict(request.get("metadata") or {}).get("timeframe") or "").lower()
        symbol = str(request.get("symbol") or "").upper()
        payload = dict(dict(execution.response_payload or {}).get("broker_execution") or {})
        if timeframe not in INTRADAY_TIMEFRAMES or not symbol or float(payload.get("filled_qty") or 0.0) <= 0:
            continue
        if float(getattr(execution, "realized_pnl_usd", 0.0) or 0.0) != 0.0:
            continue
        if any(str(leg.get("status") or "").lower() == "filled" for leg in payload.get("legs") or []):
            continue
        if symbol not in found or str(execution.created_at) > str(found[symbol].created_at):
            found[symbol] = execution
    return found


def flatten_intraday_before_close(host: Any, *, now: datetime | None = None) -> list[str]:
    """Return the symbols whose close was submitted (or is already in flight)."""

    settings = host.settings
    reconciliation = getattr(host, "reconciliation", None)
    minutes = float(getattr(settings, "intraday_flatten_minutes_before_close", 0) or 0)
    if minutes <= 0 or reconciliation is None:
        return []
    if str(getattr(settings, "execution_mode", "paper")) != "paper" or bool(getattr(settings, "enable_real_trading", False)):
        return []
    alpaca = getattr(reconciliation, "alpaca", None)
    if alpaca is None or not hasattr(alpaca, "close_position"):
        return []
    now = now or utc_now()
    try:
        remaining = _minutes_to_close(alpaca, now)
    except Exception:  # noqa: BLE001 - no clock, no action; reconciliation still protects
        return []
    if remaining is None or remaining > minutes:
        return []

    held = {str(p.symbol or "").upper() for p in alpaca.get_portfolio().positions}
    closed: list[str] = []
    for symbol, execution in _open_intraday_executions(reconciliation.executions).items():
        if symbol not in held:
            continue
        legs = dict(dict(execution.response_payload or {}).get("broker_execution") or {}).get("legs") or []
        for leg in legs:
            if str(leg.get("status") or "").lower() in {"new", "held", "accepted", "pending_new", "partially_filled"}:
                alpaca.cancel_order(str(leg.get("broker_order_id") or ""))
        try:
            response = alpaca.close_position(symbol)
        except Exception as exc:  # noqa: BLE001 - legs still cancelling: retry next tick
            message = str(exc).lower()
            event = "intraday_flatten_deferred" if any(m in message for m in _QTY_HELD) else "intraday_flatten_failed"
            host.run_logs.log(event, {"symbol": symbol, "error": str(exc)})
            if event == "intraday_flatten_deferred":
                closed.append(symbol)
            continue
        host.run_logs.log(
            "intraday_position_flattened_before_close",
            {
                "symbol": symbol,
                "execution_id": execution.id,
                "timeframe": dict(dict(execution.request_payload or {}).get("metadata") or {}).get("timeframe"),
                "minutes_to_close": round(remaining, 1),
                "broker_order_id": str(getattr(response, "order_id", "") or ""),
            },
        )
        closed.append(symbol)
    return closed


__all__ = ["INTRADAY_TIMEFRAMES", "flatten_intraday_before_close"]
