"""Alpaca paper broker client adapter.

This module is intentionally additive. It does not change the active execution
router; it only normalizes Alpaca paper responses into the project's existing
broker and execution models.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import pandas as pd
from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import (
    StockBarsRequest,
    StockLatestQuoteRequest,
    StockLatestTradeRequest,
)
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, OrderType, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import (
    GetOrdersRequest,
    LimitOrderRequest,
    MarketOrderRequest,
    StopOrderRequest,
)

from app.broker.etoro_client import BrokerClient
from app.live_signal_schema import MarketQuote
from app.models.execution import (
    AccountSummary,
    BrokerOrderResponse,
    ExecutionRecord,
    PortfolioPosition,
    PortfolioSummary,
)
from app.models.trade import TradeOrder

logger = logging.getLogger(__name__)


class AlpacaClient(BrokerClient):
    """Paper-first Alpaca broker adapter behind the existing broker interface."""

    def __init__(
        self,
        *,
        api_key: str,
        secret_key: str,
        base_url: str,
        paper: bool = True,
        data_feed: str = "iex",
        data_url: str = "https://data.alpaca.markets",
    ):
        self.api_key = api_key
        self.secret_key = secret_key
        self.base_url = base_url
        self.paper = paper
        self.data_feed = data_feed
        self.data_url = data_url
        trading_base_url = _sdk_base_url(base_url)
        data_base_url = _sdk_base_url(data_url)
        self.trading_client = TradingClient(
            api_key=api_key,
            secret_key=secret_key,
            paper=paper,
            url_override=trading_base_url,
        )
        self.data_client = StockHistoricalDataClient(
            api_key=api_key,
            secret_key=secret_key,
            url_override=data_base_url,
        )

    def get_portfolio(self) -> PortfolioSummary:
        """Call TradingClient.get_account/get_all_positions and return PortfolioSummary."""

        account = self.trading_client.get_account()
        positions = self.trading_client.get_all_positions()
        account_summary = AccountSummary(
            cash_balance=_to_float(getattr(account, "cash", 0.0)),
            equity=_to_float(getattr(account, "equity", 0.0)),
            daily_pnl=0.0,
            currency=str(getattr(account, "currency", "USD") or "USD"),
        ).model_copy(
            update={
                "buying_power": _to_float(getattr(account, "buying_power", 0.0)),
                "day_trade_count": int(_to_float(getattr(account, "day_trade_count", 0))),
            }
        )
        return PortfolioSummary(
            mode="alpaca_paper" if self.paper else "alpaca_live",
            account=account_summary,
            positions=[
                PortfolioPosition(
                    symbol=str(getattr(item, "symbol", "")).upper(),
                    position_id=None,
                    instrument_id=None,
                    is_buy=_to_float(getattr(item, "qty", 0.0)) >= 0,
                    leverage=1,
                    quantity=_to_float(getattr(item, "qty", 0.0)),
                    average_price=_to_float(getattr(item, "avg_entry_price", 0.0)),
                    market_value=_to_float(getattr(item, "market_value", 0.0)),
                    unrealized_pnl=_to_float(getattr(item, "unrealized_pl", 0.0)),
                )
                for item in positions
            ],
        )

    def get_balance(self) -> AccountSummary:
        """Call TradingClient.get_account through get_portfolio and return AccountSummary."""

        return self.get_portfolio().account

    def get_quote(
        self,
        symbol: str,
        *,
        force_refresh: bool = False,
        timeframe: str = "1d",
    ) -> MarketQuote:
        """Call StockHistoricalDataClient.get_stock_latest_quote/trade."""

        del force_refresh, timeframe
        normalized = symbol.upper().strip()
        feed = self._data_feed()
        quote_response = self.data_client.get_stock_latest_quote(
            StockLatestQuoteRequest(symbol_or_symbols=normalized, feed=feed)
        )
        trade_response = self.data_client.get_stock_latest_trade(
            StockLatestTradeRequest(symbol_or_symbols=normalized, feed=feed)
        )
        quote = _extract_symbol_payload(quote_response, normalized)
        trade = _extract_symbol_payload(trade_response, normalized)
        timestamp = (
            getattr(trade, "timestamp", None)
            or getattr(quote, "timestamp", None)
            or getattr(quote, "t", None)
        )
        return MarketQuote(
            symbol=normalized,
            bid=_to_float(getattr(quote, "bid_price", getattr(quote, "bp", None))),
            ask=_to_float(getattr(quote, "ask_price", getattr(quote, "ap", None))),
            last_execution=_to_float(getattr(trade, "price", getattr(trade, "p", None))),
            timestamp=_iso_timestamp(timestamp),
            source="alpaca",
            is_primary=True,
            used_fallback=False,
            from_cache=False,
            quote_derived_from_history=False,
            data_age_seconds=0.0,
        )

    def get_bars(
        self,
        symbol: str,
        *,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> pd.DataFrame:
        """Call StockHistoricalDataClient.get_stock_bars and return normalized OHLCV."""

        normalized = symbol.upper().strip()
        request = StockBarsRequest(
            symbol_or_symbols=normalized,
            timeframe=_to_timeframe(timeframe),
            start=start,
            end=end,
            feed=self._data_feed(),
        )
        response = self.data_client.get_stock_bars(request)
        return _bars_to_frame(response, normalized)

    def submit_order(
        self,
        *,
        symbol: str,
        side: str,
        qty: float,
        order_type: str,
        limit_price: float | None = None,
        stop_price: float | None = None,
        time_in_force: str = "day",
        client_order_id: str | None = None,
    ) -> ExecutionRecord:
        """Call TradingClient.submit_order and preserve client_order_id idempotency."""

        request = self._order_request(
            symbol=symbol,
            side=side,
            qty=qty,
            order_type=order_type,
            limit_price=limit_price,
            stop_price=stop_price,
            time_in_force=time_in_force,
            client_order_id=client_order_id,
        )
        try:
            order = self.trading_client.submit_order(request)
            logger.info(
                "alpaca_order_submitted",
                extra=_order_log_fields(
                    symbol=symbol,
                    side=side,
                    qty=qty,
                    client_order_id=client_order_id,
                    broker_order_id=getattr(order, "id", None),
                    status=getattr(order, "status", None),
                ),
            )
            return _execution_record(order, paper=self.paper, request=request)
        except APIError as exc:
            return self._handle_order_error(
                exc,
                symbol=symbol,
                side=side,
                qty=qty,
                client_order_id=client_order_id,
                request=request,
            )
        except Exception as exc:
            return self._handle_order_error(
                exc,
                symbol=symbol,
                side=side,
                qty=qty,
                client_order_id=client_order_id,
                request=request,
            )

    def cancel_order(self, broker_order_id: str) -> bool:
        """Call TradingClient.cancel_order_by_id for one broker order."""

        try:
            self.trading_client.cancel_order_by_id(broker_order_id)
            logger.info(
                "alpaca_order_cancelled",
                extra=_order_log_fields(broker_order_id=broker_order_id, status="cancelled"),
            )
            return True
        except Exception as exc:
            message = str(exc).lower()
            if any(token in message for token in ("already", "filled", "canceled", "cancelled", "not found")):
                logger.info(
                    "alpaca_order_cancel_noop",
                    extra=_order_log_fields(broker_order_id=broker_order_id, status="already_done"),
                )
                return False
            logger.exception(
                "alpaca_order_cancel_error",
                extra=_order_log_fields(broker_order_id=broker_order_id, status="error"),
            )
            raise

    def cancel_all_orders(self) -> int:
        """Call TradingClient.cancel_orders and return the number cancelled."""

        try:
            responses = self.trading_client.cancel_orders()
            count = len(responses or [])
            logger.info("alpaca_cancel_all_orders", extra={"status": "submitted", "count": count})
            return count
        except Exception:
            logger.exception("alpaca_cancel_all_orders_error", extra={"status": "error"})
            raise

    def close_all_positions(self) -> int:
        """Call TradingClient.close_all_positions(cancel_orders=True)."""

        try:
            responses = self.trading_client.close_all_positions(cancel_orders=True)
            count = len(responses or [])
            logger.info("alpaca_close_all_positions", extra={"status": "submitted", "count": count})
            return count
        except Exception:
            logger.exception("alpaca_close_all_positions_error", extra={"status": "error"})
            raise

    def get_executions(self, since: datetime | None = None) -> list[ExecutionRecord]:
        """Call TradingClient.get_orders with QueryOrderStatus.ALL and optional after."""

        request = GetOrdersRequest(status=QueryOrderStatus.ALL, after=since)
        orders = self.trading_client.get_orders(filter=request)
        return [_execution_record(order, paper=self.paper, request=request) for order in orders]

    def open_market_order_by_amount(
        self,
        order: TradeOrder,
        *,
        client_order_id: str | None = None,
    ) -> BrokerOrderResponse:
        """Call submit_order after converting TradeOrder notional into share quantity."""

        qty = float(order.amount_usd) / float(order.proposed_price)
        execution = self.submit_order(
            symbol=order.symbol,
            side=order.side.value,
            qty=qty,
            order_type="market",
            client_order_id=client_order_id,
        )
        return BrokerOrderResponse(
            order_id=execution.broker_order_id or "",
            status=execution.status,
            mode=execution.mode,
            message="Order submitted to Alpaca",
            raw_response=execution.response_payload,
        )

    def close_position(self, symbol: str) -> BrokerOrderResponse:
        """Call TradingClient.close_position for one symbol."""

        response = self.trading_client.close_position(symbol.upper().strip())
        return BrokerOrderResponse(
            order_id=str(getattr(response, "id", "")),
            status=str(getattr(response, "status", "submitted")),
            mode="alpaca_paper" if self.paper else "alpaca_live",
            message=f"Close request submitted for {symbol.upper().strip()}",
            raw_response=_model_payload(response),
        )

    def list_supported_instruments(self) -> list[dict[str, Any]]:
        """Return an empty list; Alpaca asset discovery is not routed in Phase A."""

        return []

    def _data_feed(self) -> DataFeed:
        return DataFeed(str(self.data_feed or "iex").lower())

    def _order_request(
        self,
        *,
        symbol: str,
        side: str,
        qty: float,
        order_type: str,
        limit_price: float | None,
        stop_price: float | None,
        time_in_force: str,
        client_order_id: str | None,
    ) -> MarketOrderRequest | LimitOrderRequest | StopOrderRequest:
        normalized_type = order_type.strip().lower()
        common = {
            "symbol": symbol.upper().strip(),
            "qty": float(qty),
            "side": _to_order_side(side),
            "time_in_force": _to_time_in_force(time_in_force),
            "client_order_id": client_order_id,
        }
        if normalized_type == "market":
            return MarketOrderRequest(type=OrderType.MARKET, **common)
        if normalized_type == "limit":
            if limit_price is None:
                raise ValueError("limit_price is required for limit orders")
            return LimitOrderRequest(type=OrderType.LIMIT, limit_price=float(limit_price), **common)
        if normalized_type == "stop":
            if stop_price is None:
                raise ValueError("stop_price is required for stop orders")
            return StopOrderRequest(type=OrderType.STOP, stop_price=float(stop_price), **common)
        raise ValueError(f"Unsupported Alpaca order_type: {order_type}")

    def _handle_order_error(
        self,
        exc: Exception,
        *,
        symbol: str,
        side: str,
        qty: float,
        client_order_id: str | None,
        request: MarketOrderRequest | LimitOrderRequest | StopOrderRequest,
    ) -> ExecutionRecord:
        message = str(exc)
        duplicate = client_order_id and "client_order_id" in message.lower() and any(
            token in message.lower() for token in ("exist", "duplicate", "already", "unique")
        )
        logger.error(
            "alpaca_order_error",
            extra=_order_log_fields(
                symbol=symbol,
                side=side,
                qty=qty,
                client_order_id=client_order_id,
                status="duplicate" if duplicate else "error",
            ),
        )
        if not duplicate:
            raise exc
        existing = self.trading_client.get_order_by_client_id(client_order_id)
        logger.info(
            "alpaca_order_duplicate_reused",
            extra=_order_log_fields(
                symbol=symbol,
                side=side,
                qty=qty,
                client_order_id=client_order_id,
                broker_order_id=getattr(existing, "id", None),
                status=getattr(existing, "status", None),
            ),
        )
        return _execution_record(existing, paper=self.paper, request=request)


def _to_order_side(side: str) -> OrderSide:
    normalized = side.strip().lower()
    if normalized == "buy":
        return OrderSide.BUY
    if normalized == "sell":
        return OrderSide.SELL
    raise ValueError(f"Unsupported Alpaca order side: {side}")


def _to_time_in_force(value: str) -> TimeInForce:
    mapping = {
        "day": TimeInForce.DAY,
        "gtc": TimeInForce.GTC,
        "ioc": TimeInForce.IOC,
        "fok": TimeInForce.FOK,
    }
    try:
        return mapping[value.strip().lower()]
    except KeyError as exc:
        raise ValueError(f"Unsupported Alpaca time_in_force: {value}") from exc


def _to_timeframe(value: str) -> TimeFrame:
    normalized = value.strip().lower()
    if normalized == "1d":
        return TimeFrame.Day
    if normalized == "1h":
        return TimeFrame.Hour
    if normalized.endswith("m"):
        return TimeFrame(int(normalized[:-1]), TimeFrameUnit.Minute)
    if normalized.endswith("h"):
        return TimeFrame(int(normalized[:-1]), TimeFrameUnit.Hour)
    raise ValueError(f"Unsupported Alpaca timeframe: {value}")


def _extract_symbol_payload(response: Any, symbol: str) -> Any:
    if isinstance(response, dict):
        return response.get(symbol) or response.get(symbol.upper()) or next(iter(response.values()))
    data = getattr(response, "data", None)
    if isinstance(data, dict):
        return data.get(symbol) or data.get(symbol.upper()) or next(iter(data.values()))
    return response


def _bars_to_frame(response: Any, symbol: str) -> pd.DataFrame:
    raw_df = getattr(response, "df", None)
    if raw_df is not None:
        frame = raw_df.copy()
        if isinstance(frame.index, pd.MultiIndex):
            frame = frame.xs(symbol, level=0, drop_level=True)
        frame = frame.reset_index()
    else:
        bars = _extract_symbol_payload(response, symbol)
        if isinstance(bars, list):
            rows = [_model_payload(item) for item in bars]
        else:
            rows = [_model_payload(item) for item in getattr(bars, "data", [])]
        frame = pd.DataFrame(rows)
    if frame.empty:
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume", "vwap"])
    rename = {
        "t": "timestamp",
        "o": "open",
        "h": "high",
        "l": "low",
        "c": "close",
        "v": "volume",
        "vw": "vwap",
    }
    frame = frame.rename(columns=rename)
    if "timestamp" not in frame.columns and "index" in frame.columns:
        frame = frame.rename(columns={"index": "timestamp"})
    keep = ["timestamp", "open", "high", "low", "close", "volume", "vwap"]
    for column in keep:
        if column not in frame.columns:
            frame[column] = pd.NA
    frame = frame[keep]
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    for column in ["open", "high", "low", "close", "volume", "vwap"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    return frame.sort_values("timestamp").reset_index(drop=True)


def _execution_record(
    order: Any,
    *,
    paper: bool,
    request: Any,
) -> ExecutionRecord:
    order_id = str(getattr(order, "id", "") or "")
    client_order_id = getattr(order, "client_order_id", getattr(request, "client_order_id", None))
    payload = {
        "broker_order_id": order_id,
        "client_order_id": client_order_id,
        "symbol": str(getattr(order, "symbol", getattr(request, "symbol", ""))).upper(),
        "side": _enum_value(getattr(order, "side", getattr(request, "side", ""))),
        "qty": _to_float(getattr(order, "qty", getattr(request, "qty", 0.0))),
        "status": _enum_value(getattr(order, "status", "submitted")),
        "submitted_at": _iso_timestamp(getattr(order, "submitted_at", None)),
        "filled_qty": _to_float(getattr(order, "filled_qty", 0.0)),
        "filled_avg_price": _optional_float(getattr(order, "filled_avg_price", None)),
    }
    return ExecutionRecord(
        proposal_id=f"alpaca:{client_order_id or order_id}",
        status=payload["status"],
        mode="alpaca_paper" if paper else "alpaca_live",
        broker_order_id=order_id,
        request_payload=_model_payload(request),
        response_payload=payload,
    )


def _model_payload(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    if hasattr(value, "dict"):
        return value.dict()
    if isinstance(value, dict):
        return dict(value)
    return {
        key: item
        for key, item in vars(value).items()
        if not key.startswith("_")
    }


def _order_log_fields(
    *,
    symbol: str | None = None,
    side: str | None = None,
    qty: float | None = None,
    client_order_id: str | None = None,
    broker_order_id: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "side": side,
        "qty": qty,
        "client_order_id": client_order_id,
        "broker_order_id": broker_order_id,
        "status": status,
    }


def _to_float(value: Any) -> float:
    optional = _optional_float(value)
    return optional if optional is not None else 0.0


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _iso_timestamp(value: Any) -> str | None:
    if value in (None, ""):
        return None
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize(timezone.utc)
    else:
        ts = ts.tz_convert(timezone.utc)
    return ts.isoformat()


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def _sdk_base_url(value: str) -> str:
    """Return the base URL expected by alpaca-py before it appends /v2."""

    normalized = str(value or "").rstrip("/")
    if normalized.endswith("/v2"):
        return normalized[:-3]
    return normalized
