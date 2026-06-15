"""Official eToro Demo adapter with durable request idempotency.

eToro's current demo contract is hybrid: order create/lookup and cost checks
use v2 endpoints, while demo portfolio and position close remain official v1
demo endpoints.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

import httpx

from app.broker.etoro_client import BrokerClient
from app.broker.etoro_rate_limit import (
    EToroRateLimitError,
    compact_http_body,
    mark_etoro_rate_limited,
    wait_for_etoro_slot,
)
from app.models.execution import (
    AccountSummary,
    BrokerOrderResponse,
    PortfolioPosition,
    PortfolioSummary,
)
from app.models.trade import TradeOrder

logger = logging.getLogger(__name__)


class EToroDemoV2Client(BrokerClient):
    """Paper-only eToro Demo execution and reconciliation adapter."""

    CREATE_ORDER_PATH = "/api/v2/trading/execution/demo/orders"
    LOOKUP_ORDER_PATH = "/api/v2/trading/info/demo/orders:lookup"
    COSTS_PATH = "/api/v2/trading/info/demo/costs"
    ELIGIBILITY_PATH = "/api/v2/trading/info/demo/eligibility"
    PORTFOLIO_PATH = "/api/v1/trading/info/demo/portfolio"
    CLOSE_POSITION_PATH = "/api/v1/trading/execution/demo/market-close-orders/positions/{position_id}"

    def __init__(self, settings: Any, *, idempotency_repository: Any | None = None):
        self.settings = settings
        self.idempotency = idempotency_repository

    def get_portfolio(self) -> PortfolioSummary:
        payload = self.get_demo_portfolio()
        portfolio = payload.get("clientPortfolio") or {}
        positions = [
            PortfolioPosition(
                symbol=str(item.get("symbol") or item.get("instrumentID") or ""),
                position_id=int(item.get("positionID") or 0) or None,
                instrument_id=int(item.get("instrumentID") or 0) or None,
                is_buy=bool(item.get("isBuy", True)),
                leverage=int(item.get("leverage") or 1),
                quantity=float(item.get("units") or 0.0),
                average_price=float(item.get("openRate") or 0.0),
                market_value=float(item.get("amount") or 0.0),
                unrealized_pnl=float(item.get("unrealizedPnL") or 0.0),
            )
            for item in portfolio.get("positions") or []
        ]
        credit = float(portfolio.get("credit") or 0.0)
        unrealized = sum(position.unrealized_pnl for position in positions)
        return PortfolioSummary(
            mode="etoro_demo",
            account=AccountSummary(
                cash_balance=credit,
                equity=credit + unrealized,
                daily_pnl=unrealized,
            ),
            positions=positions,
        )

    def get_balance(self) -> AccountSummary:
        return self.get_portfolio().account

    def get_demo_portfolio(self) -> dict[str, Any]:
        return self._request("GET", self.PORTFOLIO_PATH)

    def get_account_identity(self) -> dict[str, Any]:
        portfolio = self.get_demo_portfolio().get("clientPortfolio") or {}
        positions = portfolio.get("positions") or []
        account_id = str((positions[0].get("CID") if positions else "") or "")
        expected = str(self.settings.etoro_demo_expected_account_id or "")
        return {
            "account_id": account_id,
            "expected_account_id": expected,
            "verified": bool(expected and account_id and expected == account_id),
            "mode": "demo",
        }

    def open_market_order_by_amount(
        self,
        order: TradeOrder,
        *,
        client_order_id: str | None = None,
    ) -> BrokerOrderResponse:
        self._ensure_demo_mutation_allowed()
        if not client_order_id:
            raise ValueError("client_order_id is required for eToro Demo idempotency")
        if order.stop_loss is None or order.take_profit is None:
            raise ValueError("eToro Demo unattended entries require stop-loss and take-profit")
        payload = {
            "action": "open",
            "transaction": order.side.value,
            "symbol": order.symbol.upper(),
            "settlementType": str(order.metadata.get("settlement_type") or "stock"),
            "orderType": "mkt",
            "leverage": order.leverage,
            "amount": float(order.amount_usd),
            "orderCurrency": "usd",
            "stopLossRate": float(order.stop_loss),
            "takeProfitRate": float(order.take_profit),
            "stopLossType": "fixed",
        }
        costs = self.what_if(payload)
        response = self.create_order(payload, client_order_id=client_order_id)
        order_id = str(response.get("orderId") or response.get("referenceId") or "")
        return BrokerOrderResponse(
            order_id=order_id,
            status="submitted",
            mode="etoro_demo",
            message="Order accepted by eToro Demo",
            raw_response={"etoro": response, "costs": costs, "client_order_id": client_order_id},
        )

    def create_order(self, payload: dict[str, Any], *, client_order_id: str) -> dict[str, Any]:
        return self._submit_idempotent(
            method="POST",
            path=self.CREATE_ORDER_PATH,
            payload=payload,
            client_order_id=client_order_id,
            order_id_keys=("orderId", "referenceId"),
        )

    def close_position(self, symbol: str) -> BrokerOrderResponse:
        self._ensure_demo_mutation_allowed()
        normalized = symbol.upper().strip()
        raw_positions = (self.get_demo_portfolio().get("clientPortfolio") or {}).get("positions") or []
        matching = [
            item
            for item in raw_positions
            if str(item.get("symbol") or item.get("instrumentID") or "").upper() == normalized
        ]
        if len(matching) != 1:
            raise ValueError(f"Expected exactly one eToro Demo position for {normalized}")
        position = matching[0]
        client_order_id = f"close:{position['positionID']}"
        response = self.close_demo_position(
            position_id=int(position["positionID"]),
            instrument_id=int(position["instrumentID"]),
            client_order_id=client_order_id,
        )
        close_order = response.get("orderForClose") or {}
        return BrokerOrderResponse(
            order_id=str(close_order.get("orderID") or ""),
            status="submitted",
            mode="etoro_demo",
            message=f"Close order accepted for {normalized}",
            raw_response=response,
        )

    def close_demo_position(
        self,
        *,
        position_id: int,
        instrument_id: int,
        client_order_id: str,
        units_to_deduct: float | None = None,
    ) -> dict[str, Any]:
        self._ensure_demo_mutation_allowed()
        payload = {"InstrumentID": instrument_id, "UnitsToDeduct": units_to_deduct}
        return self._submit_idempotent(
            method="POST",
            path=self.CLOSE_POSITION_PATH.format(position_id=position_id),
            payload=payload,
            client_order_id=client_order_id,
            order_id_keys=("orderID",),
        )

    def get_order(
        self,
        *,
        order_id: str | None = None,
        reference_id: str | None = None,
    ) -> dict[str, Any]:
        if bool(order_id) == bool(reference_id):
            raise ValueError("Provide exactly one of order_id or reference_id")
        params = {"orderId": order_id} if order_id else {"referenceId": reference_id}
        return self._request("GET", self.LOOKUP_ORDER_PATH, params=params)

    def get_position(self, position_id: int) -> dict[str, Any]:
        positions = (self.get_demo_portfolio().get("clientPortfolio") or {}).get("positions") or []
        matching = [item for item in positions if int(item.get("positionID") or 0) == position_id]
        if not matching:
            raise KeyError(f"eToro Demo position {position_id} not found")
        return matching[0]

    def what_if(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", self.COSTS_PATH, json_body=payload)

    def eligibility(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", self.ELIGIBILITY_PATH, json_body=payload)

    def list_supported_instruments(self) -> list[dict[str, Any]]:
        return []

    def cancel_all_orders(self) -> int:
        self._ensure_demo_mutation_allowed()
        portfolio = self.get_demo_portfolio().get("clientPortfolio") or {}
        orders = portfolio.get("orders") or []
        cancelled = 0
        for order in orders:
            order_id = order.get("orderID")
            if order_id is None:
                continue
            self._request("DELETE", f"{self.CREATE_ORDER_PATH}/{order_id}")
            cancelled += 1
        return cancelled

    def close_all_positions(self) -> int:
        self._ensure_demo_mutation_allowed()
        portfolio = self.get_demo_portfolio().get("clientPortfolio") or {}
        positions = portfolio.get("positions") or []
        closed = 0
        for position in positions:
            position_id = int(position.get("positionID") or 0)
            instrument_id = int(position.get("instrumentID") or 0)
            if not position_id or not instrument_id:
                continue
            self.close_demo_position(
                position_id=position_id,
                instrument_id=instrument_id,
                client_order_id=f"emergency-close:{position_id}",
            )
            closed += 1
        return closed

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "supports_equities": True,
            "supports_native_protection": True,
            "supports_client_idempotency": True,
            "supports_shorting": False,
            "supports_borrow_checks": False,
            "supports_financing_costs": True,
            "verified": False,
            "hybrid_official_contract": True,
        }

    def _submit_idempotent(
        self,
        *,
        method: str,
        path: str,
        payload: dict[str, Any],
        client_order_id: str,
        order_id_keys: tuple[str, ...],
    ) -> dict[str, Any]:
        if self.idempotency is None:
            raise RuntimeError("eToro Demo mutation requires a durable idempotency repository")
        request_id = str(uuid5(NAMESPACE_URL, f"algobot:etoro-demo:{client_order_id}"))
        request_hash = hashlib.sha256(
            json.dumps({"method": method, "path": path, "payload": payload}, sort_keys=True).encode()
        ).hexdigest()
        reservation = self.idempotency.reserve(
            client_order_id=client_order_id,
            request_id=request_id,
            request_hash=request_hash,
            request_payload={"method": method, "path": path, "payload": payload},
        )
        if reservation["request_hash"] != request_hash:
            raise RuntimeError("client_order_id was already used for a different eToro Demo request")
        if reservation.get("response") is not None:
            return dict(reservation["response"])
        if not reservation["is_new"]:
            try:
                recovered = self.get_order(reference_id=request_id)
            except Exception as exc:
                raise RuntimeError(
                    "Existing eToro Demo request is unresolved; submission blocked to prevent duplication"
                ) from exc
            self.idempotency.complete(
                client_order_id=client_order_id,
                broker_order_id=str(recovered.get("orderId") or ""),
                status="recovered",
                response=recovered,
            )
            return recovered
        response = self._request(method, path, request_id=request_id, json_body=payload)
        broker_order_id = _find_order_id(response, order_id_keys)
        self.idempotency.complete(
            client_order_id=client_order_id,
            broker_order_id=broker_order_id,
            status="submitted",
            response=response,
        )
        return response

    def _ensure_demo_mutation_allowed(self) -> None:
        if not self.settings.etoro_demo_v2_enabled:
            raise PermissionError("ETORO_DEMO_V2_ENABLED must be true for eToro Demo mutations")
        if self.settings.etoro_account_mode != "demo" or self.settings.enable_real_trading:
            raise PermissionError("eToro Demo adapter refuses non-demo or real-enabled configuration")

    def _request(
        self,
        method: str,
        path: str,
        *,
        request_id: str | None = None,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {
            "x-api-key": self.settings.etoro_api_key,
            "x-user-key": self.settings.etoro_user_key,
            "x-request-id": request_id or str(uuid4()),
            "Content-Type": "application/json",
        }
        url = f"{self.settings.etoro_base_url.rstrip('/')}{path}"
        try:
            wait_for_etoro_slot(self.settings)
            with httpx.Client(timeout=15.0) as client:
                response = client.request(method, url, headers=headers, params=params, json=json_body)
                response.raise_for_status()
                return response.json() if response.content else {}
        except httpx.HTTPStatusError as exc:
            raw_body = exc.response.text
            body = compact_http_body(raw_body)
            if mark_etoro_rate_limited(
                self.settings,
                status_code=exc.response.status_code,
                body=raw_body,
            ):
                raise EToroRateLimitError(f"eToro API rate-limited: {body}") from exc
            raise RuntimeError(
                f"eToro Demo request failed with status {exc.response.status_code}: {body}"
            ) from exc
        except EToroRateLimitError:
            raise
        except httpx.HTTPError as exc:
            logger.exception("eToro Demo request failed: %s", exc)
            raise RuntimeError(f"eToro Demo request failed: {exc}") from exc


def _find_order_id(payload: dict[str, Any], keys: tuple[str, ...]) -> str:
    candidates = [payload]
    candidates.extend(value for value in payload.values() if isinstance(value, dict))
    for candidate in candidates:
        for key in keys:
            if candidate.get(key) is not None:
                return str(candidate[key])
    return ""
