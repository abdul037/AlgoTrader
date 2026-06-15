"""Parallel paper-broker signal mirroring and comparison evidence."""

from __future__ import annotations

from typing import Any

from app.models.institutional import BrokerComparison
from app.models.trade import AssetClass, OrderSide


class ParallelBrokerComparisonService:
    """Mirror eligible Alpaca paper entries into eToro Demo when explicitly enabled."""

    def __init__(
        self,
        *,
        settings: Any,
        etoro_demo_client: Any | None,
        broker_governance: Any,
        automation: Any,
        run_logs: Any,
    ):
        self.settings = settings
        self.etoro = etoro_demo_client
        self.brokers = broker_governance
        self.automation = automation
        self.logs = run_logs

    def mirror(self, *, proposal: Any, primary_execution: Any, primary_broker: str) -> BrokerComparison | None:
        if not self.settings.etoro_parallel_comparison_enabled:
            return None
        if self.etoro is None or not self.settings.etoro_demo_v2_enabled:
            return self._fail(
                proposal,
                primary_execution,
                primary_broker,
                "etoro_demo_v2_not_configured",
                trip_circuit_breaker=True,
            )
        order = proposal.order
        asset_class = getattr(order.asset_class, "value", order.asset_class)
        blockers: list[str] = []
        if self.settings.execution_mode != "paper" or self.settings.enable_real_trading:
            blockers.append("comparison_requires_paper_mode")
        if primary_broker != "alpaca":
            blockers.append("comparison_requires_alpaca_primary")
        if order.side != OrderSide.BUY:
            blockers.append("comparison_long_only")
        if asset_class not in {AssetClass.EQUITY.value, AssetClass.UNKNOWN.value}:
            blockers.append("comparison_equities_only")
        if str(order.strategy_name or "").lower() == "manual_smoke":
            blockers.append("comparison_excludes_manual_smoke")
        if blockers:
            return self._fail(
                proposal,
                primary_execution,
                primary_broker,
                ",".join(blockers),
                trip_circuit_breaker=False,
            )
        identity = self.etoro.get_account_identity()
        if not identity.get("verified"):
            return self._fail(
                proposal,
                primary_execution,
                primary_broker,
                "etoro_demo_account_not_verified",
                trip_circuit_breaker=True,
            )
        try:
            response = self.etoro.open_market_order_by_amount(
                order,
                client_order_id=f"mirror:{proposal.id}",
            )
            comparison = BrokerComparison(
                signal_id=getattr(proposal.signal, "id", None),
                symbol=order.symbol,
                strategy_name=str(order.strategy_name or ""),
                primary_broker=primary_broker,
                comparison_broker="etoro",
                primary_order_id=str(primary_execution.broker_order_id or ""),
                comparison_order_id=response.order_id,
                status="submitted",
                details={
                    "proposal_id": proposal.id,
                    "primary_order_id": primary_execution.broker_order_id,
                    "comparison_order_id": response.order_id,
                },
            )
            self.logs.log("parallel_broker_signal_mirrored", comparison.model_dump())
            return self.brokers.record_comparison(comparison)
        except Exception as exc:
            return self._fail(
                proposal,
                primary_execution,
                primary_broker,
                str(exc),
                trip_circuit_breaker=True,
            )

    def _fail(
        self,
        proposal: Any,
        primary_execution: Any,
        primary_broker: str,
        reason: str,
        trip_circuit_breaker: bool,
    ) -> BrokerComparison:
        comparison = BrokerComparison(
            signal_id=getattr(proposal.signal, "id", None),
            symbol=proposal.order.symbol,
            strategy_name=str(proposal.order.strategy_name or ""),
            primary_broker=primary_broker,
            comparison_broker="etoro",
            primary_order_id=str(getattr(primary_execution, "broker_order_id", "") or ""),
            status="blocked",
            details={
                "proposal_id": proposal.id,
                "primary_order_id": getattr(primary_execution, "broker_order_id", None),
                "reason": reason,
            },
        )
        self.brokers.record_comparison(comparison)
        self.logs.log("parallel_broker_signal_blocked", comparison.model_dump())
        if trip_circuit_breaker:
            self.automation.trip_circuit_breaker(
                reason=f"parallel_broker_comparison_failed:{reason}",
                emergency_stop=True,
            )
        return comparison
