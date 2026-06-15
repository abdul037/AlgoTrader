"""Institutional promotion, portfolio-risk, and rollout-readiness service."""

from __future__ import annotations

from typing import Any

from app.models.institutional import (
    BrokerCapability,
    PortfolioRiskSnapshot,
    PromotionDecision,
    RolloutGateEvidence,
)
from app.risk.portfolio import PortfolioRiskEvaluator

ROLLOUT_GATES: dict[str, tuple[str, ...]] = {
    "stage_1_validation": (
        "observation_48h",
        "supervised_alpaca_session_1",
        "supervised_alpaca_session_2",
        "clean_reconciliation",
        "zero_duplicate_orders",
        "zero_unprotected_positions",
        "credentials_rotated",
        "offsite_backup_verified",
        "managed_monitoring_live",
    ),
    "stage_2_strategy_qualification": (
        "point_in_time_data_validated",
        "audit_zero_unexplained_errors",
        "benchmark_comparison_passed",
        "monte_carlo_parameter_stability_passed",
        "production_strategy_approved",
        "shadow_20_sessions",
    ),
    "stage_3_dual_broker_paper": (
        "dual_broker_60_sessions",
        "overall_100_closed_trades",
        "matched_30_broker_lifecycles",
        "positive_net_expectancy",
        "paper_profit_factor_above_1_25",
        "paper_drawdown_below_7_5_pct",
        "runtime_availability_99_5",
        "broker_reconciliation_clean",
        "etoro_demo_account_verified",
        "zero_duplicate_orders",
        "zero_unprotected_positions",
    ),
    "stage_4_micro_live": (
        "micro_live_60_sessions",
        "micro_live_30_closed_trades",
        "micro_live_drawdown_below_5_pct",
        "live_credentials_verified",
        "manual_approval_enabled",
        "signed_live_gate_report",
    ),
}


class InstitutionalGovernanceService:
    """Make promotion and rollout decisions from persisted evidence."""

    def __init__(
        self,
        *,
        settings: Any,
        strategies: Any,
        brokers: Any,
        portfolio_risk: Any,
        rollout_gates: Any,
        automation: Any | None = None,
    ):
        self.settings = settings
        self.strategies = strategies
        self.brokers = brokers
        self.portfolio_risk = portfolio_risk
        self.rollout_gates = rollout_gates
        self.automation = automation
        self.risk_evaluator = PortfolioRiskEvaluator(settings)

    def initialize_known_capabilities(self) -> None:
        """Persist conservative capability baselines without claiming verification."""

        if self.settings.alpaca_enabled:
            self.brokers.upsert_capability(
                BrokerCapability(
                    broker="alpaca",
                    account_mode="paper" if self.settings.execution_mode == "paper" else "live",
                    supports_equities=True,
                    supports_native_protection=True,
                    supports_client_idempotency=True,
                    supports_shorting=False,
                    supports_borrow_checks=False,
                    supports_financing_costs=False,
                    verified=False,
                    details={"source": "configured_baseline", "shorting_deferred": True},
                )
            )
        if self.settings.etoro_demo_v2_enabled:
            self.brokers.upsert_capability(
                BrokerCapability(
                    broker="etoro",
                    account_mode="demo",
                    supports_equities=True,
                    supports_native_protection=False,
                    supports_client_idempotency=True,
                    supports_shorting=False,
                    supports_borrow_checks=False,
                    supports_financing_costs=True,
                    verified=False,
                    details={
                        "source": "configured_baseline",
                        "requires_demo_lifecycle_verification": True,
                        "durable_internal_request_ledger_required": True,
                    },
                )
            )

    def assess_strategy(
        self,
        version_id: str,
        *,
        decided_by: str = "system",
    ) -> PromotionDecision:
        """Assess the latest audit against every production-candidate threshold."""

        self.strategies.get_version(version_id)
        audit = self.strategies.latest_audit(version_id)
        blockers: list[str] = []
        if audit is None:
            blockers.append("missing_strategy_audit")
            evidence: dict[str, Any] = {}
        else:
            evidence = audit.model_dump()
            checks = (
                (
                    audit.out_of_sample_trades < self.settings.production_min_oos_trades,
                    "insufficient_out_of_sample_trades",
                ),
                (
                    audit.deflated_sharpe < self.settings.production_min_deflated_sharpe,
                    "deflated_sharpe_below_threshold",
                ),
                (
                    audit.rolling_sharpe < self.settings.production_min_rolling_sharpe,
                    "rolling_sharpe_below_threshold",
                ),
                (
                    audit.profit_factor < self.settings.production_min_profit_factor,
                    "profit_factor_below_threshold",
                ),
                (audit.expectancy_after_costs <= 0, "non_positive_expectancy_after_costs"),
                (
                    audit.max_drawdown_pct > self.settings.production_max_portfolio_drawdown_pct,
                    "portfolio_drawdown_above_threshold",
                ),
                (
                    audit.strategy_drawdown_pct > self.settings.production_max_strategy_drawdown_pct,
                    "strategy_drawdown_above_threshold",
                ),
                (audit.unexplained_errors > 0, "unexplained_audit_errors"),
                (audit.protected_exit_coverage_pct < 100.0, "incomplete_protected_exit_coverage"),
            )
            blockers.extend(reason for failed, reason in checks if failed)
        decision = PromotionDecision(
            strategy_version_id=version_id,
            strategy_audit_id=audit.id if audit else None,
            target_stage="production_candidate",
            approved=not blockers,
            blockers=blockers,
            evidence=evidence,
            decided_by=decided_by,
        )
        persisted = self.strategies.record_decision(decision)
        self.strategies.update_version_status(
            version_id,
            "production_candidate" if decision.approved else "qualification_failed",
        )
        return persisted

    def evaluate_portfolio(self, snapshot: PortfolioRiskSnapshot) -> PortfolioRiskSnapshot:
        """Evaluate and persist a portfolio risk snapshot."""

        evaluated = self.risk_evaluator.evaluate(snapshot)
        persisted = self.portfolio_risk.create(evaluated)
        if self.automation is not None and evaluated.status == "kill_switch":
            self.automation.trip_circuit_breaker(
                reason="portfolio_hard_drawdown_limit",
                emergency_stop=True,
            )
        elif self.automation is not None and evaluated.status == "pause":
            self.automation.pause(reason="portfolio_soft_drawdown_pause")
        return persisted

    def readiness(self) -> dict[str, Any]:
        """Return computed rollout readiness and its unresolved blockers."""

        stage = self.settings.rollout_stage
        required = ROLLOUT_GATES.get(stage, ())
        gates = self.rollout_gates.list(stage=stage)
        gate_status = {gate.gate_name: gate.status for gate in gates}
        blockers = [
            f"rollout_gate_not_passed:{name}"
            for name in required
            if gate_status.get(name) != "passed"
        ]
        approved_versions = self.strategies.approved_production_versions()
        if not approved_versions:
            blockers.append("no_production_approved_strategy")
        latest_risk = self.portfolio_risk.latest()
        if latest_risk is None:
            blockers.append("missing_portfolio_risk_snapshot")
        elif latest_risk.status != "ok":
            blockers.extend(f"portfolio_risk:{item}" for item in latest_risk.blockers)
        identities = self.brokers.list_identities()
        configured_identities = [item for item in identities if item.get("expected_account_number")]
        if configured_identities and any(not item.get("verified") for item in configured_identities):
            blockers.append("broker_account_identity_not_verified")
        return {
            "stage": stage,
            "ready": not blockers,
            "blockers": blockers,
            "required_gates": list(required),
            "gate_status": gate_status,
            "approved_strategy_versions": approved_versions,
            "latest_portfolio_risk": latest_risk.model_dump() if latest_risk else None,
        }

    def record_gate(self, gate: RolloutGateEvidence) -> RolloutGateEvidence:
        """Persist signed gate evidence."""

        if gate.status not in {"pending", "passed", "failed"}:
            raise ValueError("Rollout gate status must be pending, passed, or failed")
        if gate.status == "passed" and (not gate.signed_by.strip() or not gate.evidence):
            raise ValueError("Passed rollout gates require signed_by and non-empty evidence")
        return self.rollout_gates.upsert(gate)
