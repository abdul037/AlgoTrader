"""The learning account's own Alpaca client: a second, independent paper trading client.

Fixed to ``PAPER_URL`` with ``paper=True`` (there is no base-URL setting) and a
``TIMEOUT_SECONDS`` timeout on every HTTP request. It is never in the broker router, never the
eToro mirror, never the main reconciliation, kill switch or emergency flatten: only the
learning thread holds it. It trades only; quotes and bars come read-only from the main
account's data client (app/learning_account/market.py).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

PAPER_URL = "https://paper-api.alpaca.markets"
TIMEOUT_SECONDS = 10.0
_ENDED = frozenset({"filled", "canceled", "cancelled", "expired", "rejected", "replaced", "done_for_day"})


def build_client(settings: Any) -> LearningClient:
    """The learning account's trading client, from its own keys (never the main ones)."""

    from alpaca.trading.client import TradingClient

    class _TimedTradingClient(TradingClient):
        def _one_request(self, method: str, url: str, opts: dict, retry: int) -> Any:  # noqa: D401
            return super()._one_request(method, url, {**opts, "timeout": TIMEOUT_SECONDS}, retry)

    trading = _TimedTradingClient(
        api_key=str(settings.alpaca_learning_api_key).strip(),
        secret_key=str(settings.alpaca_learning_secret_key).strip(),
        paper=True,
        url_override=PAPER_URL,
    )
    return LearningClient(trading)


def _value(item: Any) -> str:
    return str(getattr(item, "value", item) or "")


def _float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _stamp(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def order_dict(order: Any) -> dict[str, Any]:
    """An Alpaca order (and its nested legs) as the book keeps it."""

    def one(item: Any) -> dict[str, Any]:
        return {
            "id": str(getattr(item, "id", "") or ""),
            "client_order_id": str(getattr(item, "client_order_id", "") or ""),
            "symbol": str(getattr(item, "symbol", "") or "").upper(),
            "side": _value(getattr(item, "side", "")).lower(),
            "type": _value(getattr(item, "type", getattr(item, "order_type", ""))).lower(),
            "status": _value(getattr(item, "status", "")).lower(),
            "qty": _float(getattr(item, "qty", None)),
            "filled_qty": _float(getattr(item, "filled_qty", None)) or 0.0,
            "filled_avg_price": _float(getattr(item, "filled_avg_price", None)),
            "stop_price": _float(getattr(item, "stop_price", None)),
            "limit_price": _float(getattr(item, "limit_price", None)),
            "filled_at": _stamp(getattr(item, "filled_at", None)),
        }

    payload = one(order)
    payload["legs"] = [one(leg) for leg in list(getattr(order, "legs", None) or [])]
    return payload


def ended(status: Any) -> bool:
    return str(status or "").lower() in _ENDED


def not_found(exc: Exception) -> bool:
    """An order lookup the broker answered with 'no such order' (not a failed read)."""

    code = getattr(exc, "status_code", None)
    return code == 404 or "not found" in str(exc).lower()


def refused(exc: Exception) -> bool:
    """An order the broker certainly refused (a 4xx answer that is not a duplicate id, a
    timeout or a rate limit); anything else leaves the outcome unknown."""

    code = getattr(exc, "status_code", None)
    message = str(exc).lower()
    duplicate = "client_order_id" in message and any(t in message for t in ("exist", "duplicate", "already", "unique"))
    return isinstance(code, int) and 400 <= code < 500 and code not in (408, 409, 429) and not duplicate


class LearningClient:
    """Identity, clock, positions and orders of the learning account, normalized to dicts."""

    def __init__(self, trading: Any):
        self.trading = trading

    def identity(self) -> dict[str, Any]:
        account = self.trading.get_account()
        return {
            "account_number": str(getattr(account, "account_number", "") or ""),
            "status": _value(getattr(account, "status", "")).upper(),
            "trading_blocked": bool(getattr(account, "trading_blocked", False)),
            "equity": _float(getattr(account, "equity", None)) or 0.0,
        }

    def clock(self) -> dict[str, Any]:
        clock = self.trading.get_clock()
        return {
            "is_open": bool(getattr(clock, "is_open", False)),
            "next_open": getattr(clock, "next_open", None),
            "next_close": getattr(clock, "next_close", None),
        }

    def positions(self) -> dict[str, float]:
        held: dict[str, float] = {}
        for item in self.trading.get_all_positions():
            symbol = str(getattr(item, "symbol", "") or "").upper()
            qty = _float(getattr(item, "qty", None)) or 0.0
            if symbol and qty:
                held[symbol] = held.get(symbol, 0.0) + qty
        return held

    def submit_bracket(
        self, *, symbol: str, qty: int, take_profit: float, stop_loss: float, client_order_id: str
    ) -> dict[str, Any]:
        """A market entry with GTC take-profit and stop-loss legs (as the main account sends)."""

        from alpaca.trading.enums import OrderClass, OrderSide, OrderType, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest, StopLossRequest, TakeProfitRequest

        request = MarketOrderRequest(
            symbol=symbol.upper(),
            qty=int(qty),
            side=OrderSide.BUY,
            type=OrderType.MARKET,
            time_in_force=TimeInForce.GTC,
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=round(float(take_profit), 2)),
            stop_loss=StopLossRequest(stop_price=round(float(stop_loss), 2)),
            client_order_id=client_order_id,
        )
        return order_dict(self.trading.submit_order(request))

    def submit_close(self, *, symbol: str, qty: float, client_order_id: str) -> dict[str, Any]:
        """A market sell of ``qty`` (the legs are cancelled first, so the shares are free)."""

        from alpaca.trading.enums import OrderSide, OrderType, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        request = MarketOrderRequest(
            symbol=symbol.upper(),
            qty=float(qty),
            side=OrderSide.SELL,
            type=OrderType.MARKET,
            time_in_force=TimeInForce.DAY,
            client_order_id=client_order_id,
        )
        return order_dict(self.trading.submit_order(request))

    def order(self, broker_order_id: str) -> dict[str, Any]:
        """One order by its broker id, with its nested legs."""

        from alpaca.trading.requests import GetOrderByIdRequest

        return order_dict(self.trading.get_order_by_id(broker_order_id, filter=GetOrderByIdRequest(nested=True)))

    def order_by_client_id(self, client_order_id: str) -> dict[str, Any] | None:
        """The order sent with ``client_order_id``, re-read by its broker id with nested legs;
        None when the broker has no such order."""

        try:
            found = self.trading.get_order_by_client_id(client_order_id)
        except Exception as exc:
            if not_found(exc):
                return None
            raise
        broker_id = str(getattr(found, "id", "") or "")
        return self.order(broker_id) if broker_id else order_dict(found)

    def cancel(self, broker_order_id: str) -> None:
        try:
            self.trading.cancel_order_by_id(broker_order_id)
        except Exception as exc:
            message = str(exc).lower()
            if not any(token in message for token in ("already", "filled", "canceled", "cancelled", "not found", "422")):
                raise
