"""Paper-only unattended approval and execution policy."""

from __future__ import annotations

from typing import Any

from app.models.approval import ApprovalDecisionRequest
from app.models.execution import ExecutionStatus
from app.universe import resolve_universe


class PaperAutoTradingService:
    """Auto-approve and execute only fully validated Alpaca paper candidates."""

    def __init__(
        self,
        *,
        settings: Any,
        proposal_service: Any,
        execution_coordinator: Any,
        automation: Any,
        reconciliation: Any,
        safety_state: Any,
        executions: Any,
        run_logs: Any,
        notifier: Any,
        alpaca_client: Any | None,
        strategy_governance: Any | None = None,
        institutional_governance: Any | None = None,
    ):
        self.settings = settings
        self.proposals = proposal_service
        self.execution = execution_coordinator
        self.automation = automation
        self.reconciliation = reconciliation
        self.safety = safety_state
        self.executions = executions
        self.logs = run_logs
        self.notifier = notifier
        self.alpaca = alpaca_client
        self.strategy_governance = strategy_governance
        self.institutional_governance = institutional_governance

    def candidate_blockers(self, candidate: Any) -> list[str]:
        blockers = list(self.automation.execution_blockers())
        metadata = dict(getattr(candidate, "metadata", {}) or {})
        symbol = str(getattr(candidate, "symbol", "") or "").upper()
        strategy = str(getattr(candidate, "strategy_name", "") or "")
        if self.settings.execution_mode != "paper" or bool(self.settings.enable_real_trading):
            blockers.append("paper_only_policy")
        if not str(getattr(self.settings, "alpaca_expected_account_number", "") or "").strip():
            blockers.append("alpaca_expected_account_not_configured")
        if not bool(getattr(self.settings, "paper_auto_approve_proposals", False)):
            blockers.append("paper_auto_approve_disabled")
        if not bool(getattr(self.settings, "auto_execution_worker_enabled", False)):
            blockers.append("auto_execution_worker_disabled")
        operation_mode = str(getattr(self.settings, "paper_auto_operation_mode", "shadow"))
        if operation_mode != "unattended":
            blockers.append(f"paper_auto_operation_mode_{operation_mode}")
        if (
            operation_mode == "unattended"
            and self.institutional_governance is not None
            and not self.institutional_governance.readiness()["ready"]
        ):
            blockers.append("institutional_rollout_not_ready")
        if not self.reconciliation.account_verified():
            blockers.append("alpaca_account_not_verified")
        if bool(getattr(self.settings, "auto_execution_regular_hours_only", True)) and (
            self.alpaca is None or not self.alpaca.is_regular_market_open()
        ):
            blockers.append("outside_regular_market_hours")
        if not bool(getattr(candidate, "execution_ready", False)):
            blockers.append("candidate_not_execution_ready")
        if not bool(metadata.get("alert_eligible", False)):
            blockers.append("candidate_not_alert_eligible")
        if not bool(metadata.get("backtest_validated", False)):
            blockers.append("candidate_not_backtest_validated")
        if float(getattr(candidate, "score", 0.0) or 0.0) < float(
            getattr(self.settings, "auto_execution_min_score", 65.0)
        ):
            blockers.append("candidate_score_below_auto_threshold")
        if symbol not in resolve_universe(self.settings):
            blockers.append("symbol_not_in_execution_universe")
        if self.alpaca is None or not hasattr(self.alpaca, "is_supported_equity"):
            blockers.append("alpaca_asset_check_unavailable")
        else:
            try:
                if not self.alpaca.is_supported_equity(symbol):
                    blockers.append("symbol_not_supported_by_alpaca")
            except Exception:
                blockers.append("alpaca_asset_check_failed")
        if self.safety.is_blacklisted(symbol):
            blockers.append("symbol_blacklisted")
        if strategy and not self.safety.strategy_active(strategy):
            blockers.append("strategy_inactive")
        if (
            strategy
            and self.strategy_governance is not None
            and not self.strategy_governance.strategy_production_approved(strategy)
        ):
            blockers.append("strategy_not_production_approved")
        if str(getattr(candidate, "signal_role", "") or "").lower() == "entry_short":
            blockers.append("short_entries_disabled")
        if getattr(candidate, "stop_loss", None) is None or getattr(candidate, "take_profit", None) is None:
            blockers.append("bracket_prices_missing")
        return sorted(set(blockers))

    def candidate_proposal_blockers(self, candidate: Any) -> list[str]:
        """Return persistent safety blocks that apply before proposal creation."""

        symbol = str(getattr(candidate, "symbol", "") or "").upper()
        strategy = str(getattr(candidate, "strategy_name", "") or "")
        blockers: list[str] = []
        if self.safety.is_blacklisted(symbol):
            blockers.append("symbol_blacklisted")
        if strategy and not self.safety.strategy_active(strategy):
            blockers.append("strategy_inactive")
        if (
            strategy
            and self.strategy_governance is not None
            and not self.strategy_governance.strategy_production_approved(strategy)
        ):
            blockers.append("strategy_not_production_approved")
        return blockers

    def approve_enqueue_execute(self, proposal: Any, candidate: Any) -> Any | None:
        blockers = self.candidate_blockers(candidate)
        if blockers:
            self.logs.log(
                "paper_auto_candidate_blocked",
                {"proposal_id": proposal.id, "symbol": proposal.order.symbol, "blockers": blockers},
            )
            return None
        approved = self.proposals.approve_proposal(
            proposal.id,
            ApprovalDecisionRequest(reviewer="paper_auto", notes="Paper-only auto-approved by safety policy"),
        )
        queued = self.execution.enqueue_approved_proposal(approved.id)
        processed = self.execution.process_queue_item(queued.id)
        self.logs.log(
            "paper_auto_execution_processed",
            {
                "proposal_id": approved.id,
                "queue_id": processed.id,
                "symbol": processed.symbol,
                "status": processed.status,
                "reason": processed.validation_reason,
            },
        )
        self.notifier.send_text(
            "\n".join(
                [
                    "Paper auto execution",
                    f"Symbol: {processed.symbol}",
                    f"Proposal: {approved.id}",
                    f"Queue: {processed.id}",
                    f"Status: {processed.status}",
                    f"Reason: {processed.validation_reason or 'ready'}",
                ]
            )
        )
        return processed

    def process_ready_queue(self) -> list[Any]:
        if not bool(getattr(self.settings, "auto_execution_worker_enabled", False)):
            return []
        if self.automation.execution_blockers():
            return []
        return self.execution.process_ready_queue()

    def refresh_strategy_health(self) -> list[dict[str, Any]]:
        if not bool(getattr(self.settings, "strategy_health_enabled", True)):
            return []
        groups: dict[str, list[float]] = {}
        for execution in self.executions.list(limit=2000):
            strategy = str((execution.request_payload or {}).get("strategy_name") or "")
            if not strategy or strategy == "manual_smoke":
                continue
            if not self._is_reconciled_closed_execution(execution):
                continue
            groups.setdefault(strategy, []).append(float(execution.realized_pnl_usd or 0.0))
        results: list[dict[str, Any]] = []
        minimum = max(int(getattr(self.settings, "strategy_health_min_closed_trades", 20)), 1)
        rolling = max(int(getattr(self.settings, "strategy_health_rolling_trades", 30)), minimum)
        for strategy, all_pnls in groups.items():
            pnls = all_pnls[:rolling]
            wins = sum(value for value in pnls if value > 0)
            losses = abs(sum(value for value in pnls if value < 0))
            expectancy = sum(pnls) / len(pnls) if pnls else 0.0
            profit_factor = wins / losses if losses > 0 else wins
            active = len(pnls) < minimum or not (expectancy < 0 and profit_factor < 1.0)
            reason = "" if active else "negative rolling expectancy and profit factor"
            self.safety.upsert_strategy_health(
                strategy_name=strategy,
                active=active,
                closed_trades=len(pnls),
                expectancy_usd=expectancy,
                profit_factor=profit_factor,
                reason=reason,
            )
            results.append(
                {
                    "strategy_name": strategy,
                    "active": active,
                    "closed_trades": len(pnls),
                    "expectancy_usd": expectancy,
                    "profit_factor": profit_factor,
                }
            )
        return results

    @staticmethod
    def _is_reconciled_closed_execution(execution: Any) -> bool:
        if execution.status not in {
            ExecutionStatus.FILLED,
            ExecutionStatus.CANCELED,
            "filled",
            "canceled",
        }:
            return False
        broker_execution = dict((execution.response_payload or {}).get("broker_execution") or {})
        return any(
            str(leg.get("status") or "").lower() == "filled"
            for leg in list(broker_execution.get("legs") or [])
        )
