"""eToro broker client abstraction and verified demo integration."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any
from uuid import uuid4

import httpx

from app.broker.instrument_resolver import InstrumentResolver
from app.config import AppSettings
from app.models.execution import (
    AccountSummary,
    BrokerOrderResponse,
    PortfolioPosition,
    PortfolioSummary,
)
from app.models.trade import TradeOrder
from app.utils.ids import generate_id

logger = logging.getLogger(__name__)


class BrokerClient(ABC):
    """Broker interface for trade execution and account queries."""

    @abstractmethod
    def get_portfolio(self) -> PortfolioSummary:
        """Return current positions and balances."""

    @abstractmethod
    def get_balance(self) -> AccountSummary:
        """Return balance and PnL summary."""

    @abstractmethod
    def open_market_order_by_amount(self, order: TradeOrder) -> BrokerOrderResponse:
        """Open a market order by notional amount."""

    @abstractmethod
    def close_position(self, symbol: str) -> BrokerOrderResponse:
        """Close an existing position."""

    @abstractmethod
    def list_supported_instruments(self) -> list[dict[str, Any]]:
        """Return supported instruments if available."""


class EToroClient(BrokerClient):
    """Safe-first eToro client with verified demo endpoints and a simulation fallback."""

    def __init__(self, settings: AppSettings):
        self.settings = settings
        self.resolver = InstrumentResolver(settings)
        self._instrument_cache_by_symbol: dict[str, dict[str, Any]] = {}
        self._instrument_cache_by_id: dict[int, dict[str, Any]] = {}

    def get_portfolio(self) -> PortfolioSummary:
        if self.settings.broker_simulation_enabled:
            return PortfolioSummary(
                mode=self.settings.etoro_account_mode,
                account=AccountSummary(
                    cash_balance=10000.0,
                    equity=10000.0,
                    daily_pnl=0.0,
                ),
                positions=[],
            )

        portfolio_payload = self._request("GET", self._trading_info_path("portfolio"))
        pnl_payload = self._request("GET", self._trading_info_path("pnl"))
        portfolio = portfolio_payload.get("clientPortfolio", {})
        pnl = pnl_payload.get("clientPortfolio", {})

        positions = [self._position_from_payload(item) for item in portfolio.get("positions", [])]
        credit = float(pnl.get("credit", portfolio.get("credit", 0.0)))
        unrealized_pnl = float(pnl.get("unrealizedPnL", 0.0))

        return PortfolioSummary(
            mode=self.settings.etoro_account_mode,
            account=AccountSummary(
                cash_balance=credit,
                equity=credit + unrealized_pnl,
                daily_pnl=unrealized_pnl,
                currency="USD",
            ),
            positions=positions,
        )

    def get_balance(self) -> AccountSummary:
        return self.get_portfolio().account

    def open_market_order_by_amount(self, order: TradeOrder) -> BrokerOrderResponse:
        instrument = self.resolver.resolve(order.symbol)
        self._ensure_order_mode_allowed()

        if self.settings.broker_simulation_enabled:
            payload = {
                "InstrumentID": instrument.broker_symbol,
                "Amount": order.amount_usd,
                "Leverage": order.leverage,
                "IsBuy": order.side.value == "buy",
            }
            logger.info("Submitting simulated eToro order payload: %s", payload)
            return BrokerOrderResponse(
                order_id=generate_id("sim_order"),
                status="simulated_submitted",
                mode=self.settings.etoro_account_mode,
                message="Simulated broker response.",
                raw_response={"payload": payload},
            )

        broker_instrument = self._search_instrument(instrument.symbol)
        payload: dict[str, Any] = {
            "InstrumentID": broker_instrument["instrument_id"],
            "Amount": order.amount_usd,
            "Leverage": order.leverage,
            "IsBuy": order.side.value == "buy",
            "IsTslEnabled": False,
            "IsNoStopLoss": order.stop_loss is None,
            "IsNoTakeProfit": order.take_profit is None,
        }
        if order.stop_loss is not None:
            payload["StopLossRate"] = order.stop_loss
        if order.take_profit is not None:
            payload["TakeProfitRate"] = order.take_profit

        response = self._request(
            "POST",
            self._trading_execution_path("market-open-orders/by-amount"),
            json_body=payload,
        )
        order_for_open = response.get("orderForOpen", {})
        return BrokerOrderResponse(
            order_id=str(order_for_open.get("orderID") or generate_id("etoro")),
            status=self._normalize_order_status(order_for_open.get("statusID")),
            mode=self.settings.etoro_account_mode,
            message="Order accepted by eToro",
            raw_response=response,
        )

    def close_position(self, symbol: str) -> BrokerOrderResponse:
        instrument = self.resolver.resolve(symbol)
        self._ensure_order_mode_allowed()

        if self.settings.broker_simulation_enabled:
            return BrokerOrderResponse(
                order_id=generate_id("sim_close"),
                status="simulated_closed",
                mode=self.settings.etoro_account_mode,
                message=f"Simulated close request for {instrument.symbol}",
                raw_response={"symbol": instrument.symbol},
            )

        broker_instrument = self._search_instrument(instrument.symbol)
        portfolio = self.get_portfolio()
        position = next(
            (
                item
                for item in portfolio.positions
                if item.instrument_id == broker_instrument["instrument_id"] and item.position_id
            ),
            None,
        )
        if position is None:
            raise ValueError(f"No open position found for {instrument.symbol}")

        payload = {
            "InstrumentID": broker_instrument["instrument_id"],
            "UnitsToDeduct": None,
        }
        response = self._request(
            "POST",
            self._trading_execution_path(
                f"market-close-orders/positions/{position.position_id}"
            ),
            json_body=payload,
        )
        order_for_close = response.get("orderForClose", {})
        return BrokerOrderResponse(
            order_id=str(order_for_close.get("orderID") or generate_id("close")),
            status=self._normalize_order_status(order_for_close.get("statusID")),
            mode=self.settings.etoro_account_mode,
            message="Close order accepted by eToro",
            raw_response=response,
        )

    def list_supported_instruments(self) -> list[dict[str, Any]]:
        if self.settings.broker_simulation_enabled:
            return [
                {
                    "symbol": instrument.symbol,
                    "broker_symbol": instrument.broker_symbol,
                    "asset_class": instrument.asset_class.value,
                }
                for instrument in self.resolver.list_supported()
            ]

        supported: list[dict[str, Any]] = []
        for instrument in self.resolver.list_supported():
            try:
                market_data = self._search_instrument(instrument.symbol)
            except RuntimeError:
                continue
            supported.append(
                {
                    "symbol": instrument.symbol,
                    "broker_symbol": str(market_data["instrument_id"]),
                    "asset_class": instrument.asset_class.value,
                    "is_tradable": market_data["is_tradable"],
                    "is_buy_enabled": market_data["is_buy_enabled"],
                    "current_rate": market_data["current_rate"],
                }
            )
        return supported

    def _ensure_order_mode_allowed(self) -> None:
        if self.settings.real_mode_requested and not self.settings.enable_real_trading:
            raise PermissionError(
                "Real trading is disabled. Set ENABLE_REAL_TRADING=true only after verification."
            )

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.settings.etoro_api_key,
            "x-user-key": self.settings.etoro_user_key,
            "x-request-id": str(uuid4()),
            "Content-Type": "application/json",
        }

    def _build_url(self, path: str) -> str:
        root = self.settings.etoro_base_url.rstrip("/")
        if path.startswith("/api/v1/"):
            return f"{root}{path}"
        return f"{root}/api/v1{path}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = self._build_url(path)
        try:
            with httpx.Client(timeout=15.0) as client:
                response = client.request(
                    method=method,
                    url=url,
                    headers=self._headers(),
                    params=params,
                    json=json_body,
                )
                response.raise_for_status()
                if not response.content:
                    return {}
                return response.json()
        except httpx.HTTPStatusError as exc:
            body = exc.response.text[:1000]
            logger.error("Broker request failed: %s %s", exc, body)
            raise RuntimeError(
                f"Broker request failed with status {exc.response.status_code}: {body}"
            ) from exc
        except httpx.HTTPError as exc:
            logger.exception("Broker request failed: %s", exc)
            raise RuntimeError(f"Broker request failed: {exc}") from exc

    def _trading_execution_path(self, suffix: str) -> str:
        if self.settings.etoro_account_mode == "demo":
            return f"/trading/execution/demo/{suffix}"
        # TODO: Real-mode endpoint mappings should be re-verified directly against a real account
        # before production use. This path is inferred from the public docs.
        return f"/trading/execution/{suffix}"

    def _trading_info_path(self, suffix: str) -> str:
        if self.settings.etoro_account_mode == "demo":
            return f"/trading/info/demo/{suffix}"
        # TODO: Real-mode endpoint mappings should be re-verified directly against a real account
        # before production use. This path is inferred from the public docs.
        return f"/trading/info/{suffix}"

    def _search_instrument(self, symbol: str) -> dict[str, Any]:
        normalized = symbol.upper().strip()
        cached = self._instrument_cache_by_symbol.get(normalized)
        if cached is not None:
            return cached

        payload = self._request(
            "GET",
            "/market-data/search",
            params={"internalSymbolFull": normalized},
        )
        items = payload.get("items", [])
        exact = next(
            (item for item in items if str(item.get("internalSymbolFull", "")).upper() == normalized),
            None,
        )
        if exact is None:
            raise RuntimeError(f"Instrument lookup failed for {normalized}")

        resolved = {
            "symbol": normalized,
            "instrument_id": int(exact["internalInstrumentId"]),
            "current_rate": float(exact.get("currentRate", 0.0)),
            "is_tradable": bool(exact.get("isCurrentlyTradable", False)),
            "is_buy_enabled": bool(exact.get("isBuyEnabled", False)),
        }
        self._instrument_cache_by_symbol[normalized] = resolved
        self._instrument_cache_by_id[resolved["instrument_id"]] = resolved
        return resolved

    def _position_from_payload(self, payload: dict[str, Any]) -> PortfolioPosition:
        instrument_id = int(payload.get("instrumentID", 0))
        resolved = self._instrument_cache_by_id.get(instrument_id) or self._resolve_cached_symbol(
            instrument_id
        )
        symbol = resolved.get("symbol", str(instrument_id))
        amount = float(payload.get("amount", 0.0))
        return PortfolioPosition(
            symbol=symbol,
            position_id=int(payload.get("positionID", 0)) or None,
            instrument_id=instrument_id or None,
            is_buy=bool(payload.get("isBuy", True)),
            leverage=int(payload.get("leverage", 1) or 1),
            quantity=float(payload.get("units", 0.0)),
            average_price=float(payload.get("openRate", 0.0)),
            market_value=amount,
            unrealized_pnl=0.0,
        )

    def _resolve_cached_symbol(self, instrument_id: int) -> dict[str, Any]:
        for instrument in self.resolver.list_supported():
            cached = self._instrument_cache_by_symbol.get(instrument.symbol)
            if cached is None:
                try:
                    cached = self._search_instrument(instrument.symbol)
                except RuntimeError:
                    continue
            if cached["instrument_id"] == instrument_id:
                return cached
        return {"symbol": str(instrument_id)}

    @staticmethod
    def _normalize_order_status(status_id: Any) -> str:
        mapping = {
            1: "submitted",
            2: "rejected",
            3: "filled",
            4: "cancelled",
        }
        try:
            return mapping.get(int(status_id), "submitted")
        except (TypeError, ValueError):
            return "submitted"
