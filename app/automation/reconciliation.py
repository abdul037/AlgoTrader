"""Alpaca paper account reconciliation and safety checks."""

from __future__ import annotations

import time
from typing import Any

from app.models.execution import ExecutionStatus
from app.models.institutional import (
    BrokerAccountIdentity,
    BrokerCapability,
    BrokerReconciliationResult,
)
from app.utils.time import utc_now


class AlpacaReconciliationService:
    """Synchronize Alpaca order state and trip the circuit breaker on unsafe drift."""

    FAILURE_COUNT_KEY = "reconciliation:consecutive_failures"
    LAST_RUN_KEY = "reconciliation:last_run_at"
    LAST_STATUS_KEY = "reconciliation:last_status"
    LAST_ISSUES_KEY = "reconciliation:last_issues"

    def __init__(
        self,
        *,
        settings: Any,
        alpaca_client: Any | None,
        executions: Any,
        broker_orders: Any,
        broker_positions: Any,
        safety_state: Any,
        runtime_state: Any,
        run_logs: Any,
        automation: Any,
        broker_governance: Any | None = None,
        learning_service: Any | None = None,
    ):
        self.settings = settings
        self.alpaca = alpaca_client
        self.executions = executions
        self.broker_orders = broker_orders
        self.broker_positions = broker_positions
        self.safety = safety_state
        self.state = runtime_state
        self.logs = run_logs
        self.automation = automation
        self.broker_governance = broker_governance
        self.learning = learning_service

    def reconcile(self) -> dict[str, Any]:
        if self.alpaca is None or not bool(getattr(self.settings, "alpaca_reconciliation_enabled", True)):
            return {"status": "disabled", "issues": []}
        result: dict[str, Any] | None = None
        last_error: Exception | None = None
        attempts = max(int(getattr(self.settings, "alpaca_reconciliation_max_attempts", 3) or 3), 1)
        backoff = max(float(getattr(self.settings, "alpaca_reconciliation_retry_backoff_seconds", 1.0) or 0.0), 0.0)
        for attempt in range(1, attempts + 1):
            try:
                result = self._reconcile()
                last_error = None
                break
            except Exception as exc:  # noqa: BLE001 - broker SDK raises transport-specific exceptions
                last_error = exc
                self.logs.log(
                    "alpaca_reconciliation_attempt_failed",
                    {
                        "attempt": attempt,
                        "max_attempts": attempts,
                        "error": str(exc),
                    },
                )
                if attempt < attempts and backoff > 0:
                    time.sleep(backoff)
        if result is None:
            return self._record_failure([f"reconciliation_error:{last_error}"])

        issues = list(result["issues"])
        if issues:
            return self._record_failure(issues, result=result)
        self.state.set(self.FAILURE_COUNT_KEY, "0")
        self.state.set(self.LAST_RUN_KEY, utc_now().isoformat())
        self.state.set(self.LAST_STATUS_KEY, "ok")
        self.state.set(self.LAST_ISSUES_KEY, "")
        self.safety.record_reconciliation(
            status="ok",
            account_number=result["account"]["account_number"],
            orders_seen=result["orders_seen"],
            positions_seen=result["positions_seen"],
            issues=[],
            account=result["account"],
        )
        self._record_governance_reconciliation(status="ok", issues=[], result=result)
        self.logs.log("alpaca_reconciliation_ok", result)
        return {"status": "ok", **result}

    def account_verified(self) -> bool:
        expected = str(
            getattr(
                self.settings,
                "alpaca_effective_expected_account_number",
                getattr(self.settings, "alpaca_expected_account_number", ""),
            )
            or ""
        ).strip()
        if not expected:
            return True
        return bool(self.automation.status().account_verified)

    def _reconcile(self) -> dict[str, Any]:
        account = self.alpaca.get_account_identity()
        expected = str(
            getattr(
                self.settings,
                "alpaca_effective_expected_account_number",
                getattr(self.settings, "alpaca_expected_account_number", ""),
            )
            or ""
        ).strip()
        actual = str(account.get("account_number") or "")
        account_verified = not expected or actual == expected
        self.automation.set_account_verified(account_verified)
        if self.broker_governance is not None:
            self.broker_governance.upsert_identity(
                BrokerAccountIdentity(
                    broker="alpaca",
                    account_mode="paper" if getattr(self.alpaca, "paper", True) else "live",
                    account_id=str(account.get("id") or ""),
                    account_number=actual,
                    expected_account_number=expected,
                    verified=account_verified,
                    status=str(account.get("status") or "unknown"),
                    details={
                        "trading_blocked": bool(account.get("trading_blocked")),
                        "equity": float(account.get("equity") or 0.0),
                    },
                )
            )
            capabilities = (
                self.alpaca.get_capabilities()
                if hasattr(self.alpaca, "get_capabilities")
                else {}
            )
            self.broker_governance.upsert_capability(
                BrokerCapability(
                    broker="alpaca",
                    account_mode="paper" if getattr(self.alpaca, "paper", True) else "live",
                    supports_equities=bool(capabilities.get("supports_equities", True)),
                    supports_native_protection=bool(
                        capabilities.get("supports_native_protection", True)
                    ),
                    supports_client_idempotency=bool(
                        capabilities.get("supports_client_idempotency", True)
                    ),
                    supports_shorting=bool(capabilities.get("supports_shorting", False)),
                    supports_borrow_checks=bool(
                        capabilities.get("supports_borrow_checks", False)
                    ),
                    supports_financing_costs=bool(
                        capabilities.get("supports_financing_costs", False)
                    ),
                    verified=account_verified,
                    details=capabilities,
                )
            )
        issues: list[str] = []
        if not account_verified:
            issues.append(f"account_mismatch:expected={expected}:actual={actual}")
        if account.get("trading_blocked"):
            issues.append("alpaca_trading_blocked")

        orders = self.alpaca.get_all_orders()
        positions = self.alpaca.get_portfolio().positions
        self.broker_positions.replace_active(account_number=actual, positions=positions)
        executions_by_order = {
            item.broker_order_id: item
            for item in self.executions.list(limit=2000)
            if item.broker_order_id
        }
        owned_symbols: set[str] = set()
        protected_symbols: set[str] = set()

        for order in orders:
            payload = dict(order.response_payload or {})
            broker_order_id = str(order.broker_order_id or "")
            execution = executions_by_order.get(broker_order_id)
            execution_id = execution.id if execution is not None else None
            symbol = str(payload.get("symbol") or "").upper()
            if execution is not None:
                owned_symbols.add(symbol)
                self._update_execution(execution, payload)
            legs = list(payload.get("legs") or [])
            if execution is not None and payload.get("order_class") == "bracket" and len(legs) >= 2:
                protected_symbols.add(symbol)
            if broker_order_id:
                self._upsert_order(payload, execution_id=execution_id, parent_order_id=None)
            for leg in legs:
                self._upsert_order(leg, execution_id=execution_id, parent_order_id=broker_order_id)

        for position in positions:
            symbol = str(position.symbol or "").upper()
            if symbol not in owned_symbols:
                issues.append(f"unknown_position:{symbol}")
            if symbol not in protected_symbols:
                issues.append(f"missing_bracket_protection:{symbol}")

        return {
            "account": account,
            "orders_seen": len(orders),
            "positions_seen": len(positions),
            "issues": sorted(set(issues)),
        }

    def _update_execution(self, execution: Any, payload: dict[str, Any]) -> None:
        status = str(payload.get("status") or "").lower()
        execution.response_payload = {
            **dict(execution.response_payload or {}),
            "broker": "alpaca",
            "broker_execution": payload,
        }
        if status == "filled":
            execution.status = ExecutionStatus.FILLED
        elif status in {"canceled", "cancelled", "expired", "rejected"}:
            execution.status = ExecutionStatus.CANCELED if status.startswith("cancel") else ExecutionStatus.FAILED
        execution.realized_pnl_usd = self._realized_pnl(payload)
        execution.updated_at = utc_now().isoformat()
        self.executions.update(execution)
        if self.learning is not None:
            self.learning.record_execution_event(
                execution,
                event_type=self._learning_event_type(payload),
            )
        if self.broker_governance is not None:
            fill_price = float(payload.get("filled_avg_price") or 0.0) or None
            proposed_price = float((execution.request_payload or {}).get("proposed_price") or 0.0)
            slippage_bps = (
                abs((fill_price - proposed_price) / proposed_price) * 10_000.0
                if fill_price is not None and proposed_price > 0
                else None
            )
            self.broker_governance.update_comparison_fill(
                broker="alpaca",
                broker_order_id=str(payload.get("broker_order_id") or execution.broker_order_id or ""),
                fill_price=fill_price,
                slippage_bps=slippage_bps,
            )

    @staticmethod
    def _learning_event_type(payload: dict[str, Any]) -> str:
        legs = list(payload.get("legs") or [])
        if any(
            str(leg.get("status") or "").lower() == "filled"
            and str(leg.get("side") or "").lower() == "sell"
            for leg in legs
        ):
            return "closure"
        if str(payload.get("status") or "").lower() == "filled":
            return "fill"
        if legs:
            return "protective_order_change"
        return "reconciled"

    @staticmethod
    def _realized_pnl(payload: dict[str, Any]) -> float:
        entry_price = float(payload.get("filled_avg_price") or 0.0)
        entry_qty = float(payload.get("filled_qty") or 0.0)
        if entry_price <= 0 or entry_qty <= 0:
            return 0.0
        for leg in list(payload.get("legs") or []):
            if str(leg.get("status") or "").lower() != "filled":
                continue
            exit_price = float(leg.get("filled_avg_price") or 0.0)
            exit_qty = float(leg.get("filled_qty") or 0.0)
            if exit_price > 0 and exit_qty > 0:
                return round((exit_price - entry_price) * min(entry_qty, exit_qty), 2)
        return 0.0

    def _upsert_order(
        self,
        payload: dict[str, Any],
        *,
        execution_id: str | None,
        parent_order_id: str | None,
    ) -> None:
        broker_order_id = str(payload.get("broker_order_id") or "")
        if not broker_order_id:
            return
        self.broker_orders.upsert(
            broker_order_id=broker_order_id,
            execution_id=execution_id,
            client_order_id=str(payload.get("client_order_id") or "") or None,
            symbol=str(payload.get("symbol") or ""),
            side=str(payload.get("side") or ""),
            order_class=str(payload.get("order_class") or ("bracket_leg" if parent_order_id else "")),
            status=str(payload.get("status") or ""),
            filled_qty=float(payload.get("filled_qty") or 0.0),
            filled_avg_price=(
                float(payload["filled_avg_price"])
                if payload.get("filled_avg_price") not in (None, "")
                else None
            ),
            parent_order_id=parent_order_id,
            payload=payload,
        )

    def _record_failure(
        self,
        issues: list[str],
        *,
        result: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        previous = int(self.state.get(self.FAILURE_COUNT_KEY) or 0)
        count = previous + 1
        now = utc_now().isoformat()
        self.state.set(self.FAILURE_COUNT_KEY, str(count))
        self.state.set(self.LAST_RUN_KEY, now)
        self.state.set(self.LAST_STATUS_KEY, "error")
        self.state.set(self.LAST_ISSUES_KEY, ",".join(issues))
        account = dict((result or {}).get("account") or {})
        self.safety.record_reconciliation(
            status="error",
            account_number=str(account.get("account_number") or ""),
            orders_seen=int((result or {}).get("orders_seen") or 0),
            positions_seen=int((result or {}).get("positions_seen") or 0),
            issues=issues,
            account=account,
        )
        self._record_governance_reconciliation(status="error", issues=issues, result=result or {})
        self.logs.log("alpaca_reconciliation_failed", {"issues": issues, "consecutive_failures": count})
        threshold = max(int(getattr(self.settings, "reconciliation_failures_before_kill_switch", 3)), 1)
        immediate = any(
            item.startswith(("account_mismatch:", "unknown_position:", "missing_bracket_protection:"))
            for item in issues
        )
        if immediate or count >= threshold:
            account_mismatch = any(item.startswith("account_mismatch:") for item in issues)
            self.automation.trip_circuit_breaker(
                reason=";".join(issues),
                emergency_stop=not account_mismatch,
            )
        return {"status": "error", "issues": issues, "consecutive_failures": count, **(result or {})}

    def _record_governance_reconciliation(
        self,
        *,
        status: str,
        issues: list[str],
        result: dict[str, Any],
    ) -> None:
        if self.broker_governance is None:
            return
        account = dict(result.get("account") or {})
        self.broker_governance.record_reconciliation(
            BrokerReconciliationResult(
                broker="alpaca",
                account_id=str(account.get("id") or account.get("account_number") or ""),
                status=status,
                orders_seen=int(result.get("orders_seen") or 0),
                positions_seen=int(result.get("positions_seen") or 0),
                unknown_positions=sum(item.startswith("unknown_position:") for item in issues),
                unprotected_positions=sum(
                    item.startswith("missing_bracket_protection:") for item in issues
                ),
                issues=issues,
                details={"account_number": str(account.get("account_number") or "")},
            )
        )
