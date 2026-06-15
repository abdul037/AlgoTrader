"""eToro Demo order and position reconciliation with circuit-breaker enforcement."""

from __future__ import annotations

from typing import Any

from app.models.institutional import (
    BrokerAccountIdentity,
    BrokerCapability,
    BrokerReconciliationResult,
)
from app.utils.time import utc_now


class EToroDemoReconciliationService:
    """Reconcile eToro Demo state against the durable local request ledger."""

    LAST_RUN_KEY = "etoro_demo_reconciliation:last_run_at"
    LAST_STATUS_KEY = "etoro_demo_reconciliation:last_status"

    def __init__(
        self,
        *,
        settings: Any,
        client: Any | None,
        idempotency: Any,
        broker_governance: Any,
        runtime_state: Any,
        run_logs: Any,
        automation: Any,
    ):
        self.settings = settings
        self.client = client
        self.idempotency = idempotency
        self.brokers = broker_governance
        self.state = runtime_state
        self.logs = run_logs
        self.automation = automation

    def reconcile(self) -> dict[str, Any]:
        if self.client is None or not self.settings.etoro_demo_v2_enabled:
            return {"status": "disabled", "issues": []}
        issues: list[str] = []
        try:
            portfolio = self.client.get_demo_portfolio().get("clientPortfolio") or {}
            positions = portfolio.get("positions") or []
            requests = self.idempotency.list(limit=2000)
            owned_positions: set[int] = set()
            account_ids: set[str] = {
                str(position.get("CID"))
                for position in positions
                if position.get("CID") not in (None, "")
            }
            orders_seen = 0
            for request in requests:
                try:
                    order = self.client.get_order(reference_id=request["request_id"])
                except Exception as exc:
                    issues.append(
                        f"unresolved_order_state:{request['client_order_id']}:{exc}"
                    )
                    continue
                orders_seen += 1
                account_id = order.get("accountId")
                if account_id not in (None, ""):
                    account_ids.add(str(account_id))
                for execution in order.get("positionExecutions") or []:
                    position_id = int(execution.get("positionId") or 0)
                    if position_id:
                        owned_positions.add(position_id)
                opening_data = next(
                    (
                        execution.get("openingData") or {}
                        for execution in order.get("positionExecutions") or []
                        if execution.get("openingData")
                    ),
                    {},
                )
                self.brokers.update_comparison_fill(
                    broker="etoro",
                    broker_order_id=str(order.get("orderId") or ""),
                    fill_price=float(opening_data.get("avgPrice") or 0.0) or None,
                    cost_usd=float(order.get("totalCosts") or opening_data.get("fees") or 0.0),
                    slippage_bps=None,
                )
                self.idempotency.complete(
                    client_order_id=request["client_order_id"],
                    broker_order_id=str(order.get("orderId") or request.get("broker_order_id") or ""),
                    status=str((order.get("status") or {}).get("name") or "reconciled"),
                    response=order,
                )

            actual_account_id = next(iter(account_ids), "")
            expected = str(self.settings.etoro_demo_expected_account_id or "").strip()
            verified = bool(expected and actual_account_id and expected == actual_account_id)
            if not expected:
                issues.append("etoro_demo_expected_account_not_configured")
            elif not verified:
                issues.append(
                    f"etoro_demo_account_mismatch:expected={expected}:actual={actual_account_id}"
                )
            self.brokers.upsert_identity(
                BrokerAccountIdentity(
                    broker="etoro",
                    account_mode="demo",
                    account_id=actual_account_id,
                    expected_account_number=expected,
                    verified=verified,
                    status="active" if verified else "mismatch",
                )
            )

            unknown_positions = 0
            unprotected_positions = 0
            for position in positions:
                position_id = int(position.get("positionID") or 0)
                if position_id not in owned_positions:
                    unknown_positions += 1
                    issues.append(f"unknown_etoro_demo_position:{position_id}")
                if _missing_protection(position):
                    unprotected_positions += 1
                    issues.append(f"missing_etoro_demo_protection:{position_id}")
            result = BrokerReconciliationResult(
                broker="etoro",
                account_id=actual_account_id,
                status="error" if issues else "ok",
                orders_seen=orders_seen,
                positions_seen=len(positions),
                unknown_positions=unknown_positions,
                unprotected_positions=unprotected_positions,
                issues=sorted(set(issues)),
            )
            capabilities = (
                self.client.get_capabilities()
                if hasattr(self.client, "get_capabilities")
                else {}
            )
            self.brokers.upsert_capability(
                BrokerCapability(
                    broker="etoro",
                    account_mode="demo",
                    supports_equities=bool(capabilities.get("supports_equities")),
                    supports_native_protection=bool(
                        capabilities.get("supports_native_protection")
                    ),
                    supports_client_idempotency=bool(
                        capabilities.get("supports_client_idempotency")
                    ),
                    supports_shorting=bool(capabilities.get("supports_shorting")),
                    supports_borrow_checks=bool(capabilities.get("supports_borrow_checks")),
                    supports_financing_costs=bool(
                        capabilities.get("supports_financing_costs")
                    ),
                    verified=not issues,
                    details=capabilities,
                )
            )
            self.brokers.record_reconciliation(result)
            self.state.set(self.LAST_RUN_KEY, utc_now().isoformat())
            self.state.set(self.LAST_STATUS_KEY, result.status)
            self.logs.log("etoro_demo_reconciliation", result.model_dump())
            if issues:
                self.automation.trip_circuit_breaker(
                    reason=";".join(sorted(set(issues))),
                    emergency_stop=True,
                )
            return result.model_dump()
        except Exception as exc:
            issue = f"etoro_demo_reconciliation_error:{exc}"
            self.state.set(self.LAST_RUN_KEY, utc_now().isoformat())
            self.state.set(self.LAST_STATUS_KEY, "error")
            self.logs.log("etoro_demo_reconciliation_failed", {"issues": [issue]})
            self.automation.trip_circuit_breaker(reason=issue, emergency_stop=True)
            return {"status": "error", "issues": [issue]}


def _missing_protection(position: dict[str, Any]) -> bool:
    if bool(position.get("isNoStopLoss")) or bool(position.get("isNoTakeProfit")):
        return True
    return float(position.get("stopLossRate") or 0.0) <= 0 or float(
        position.get("takeProfitRate") or 0.0
    ) <= 0
