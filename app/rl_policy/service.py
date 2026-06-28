"""Offline contextual-bandit policy for Alpaca Paper proposals."""

from __future__ import annotations

import hashlib
from typing import Any

from app.execution.interfaces import SignalApprovalAdapter
from app.live_signal_schema import LiveSignalSnapshot
from app.models.rl_policy import RLPolicyProposal, RLPolicyVersion
from app.utils.time import utc_now


class RLPolicyService:
    """Train and apply a paper-only policy over deterministic scanner candidates."""

    def __init__(
        self,
        *,
        settings: Any,
        repository: Any,
        scan_decisions: Any,
        strategy_lab: Any,
        strategy_governance: Any,
        proposal_service: Any,
        auto_trading: Any,
        automation: Any,
        reconciliation: Any,
        safety_state: Any,
        alpaca_client: Any | None,
        run_logs: Any,
    ):
        self.settings = settings
        self.repository = repository
        self.scan_decisions = scan_decisions
        self.strategy_lab = strategy_lab
        self.strategy_governance = strategy_governance
        self.proposals = proposal_service
        self.auto_trading = auto_trading
        self.automation = automation
        self.reconciliation = reconciliation
        self.safety = safety_state
        self.alpaca = alpaca_client
        self.logs = run_logs
        self.adapter = SignalApprovalAdapter()

    def status(self) -> dict[str, Any]:
        latest = self.repository.latest_version()
        counts = self.repository.counts()
        return {
            "enabled": bool(getattr(self.settings, "rl_policy_enabled", False)),
            "training_enabled": bool(getattr(self.settings, "rl_policy_training_enabled", False)),
            "paper_proposals_enabled": bool(getattr(self.settings, "rl_policy_paper_proposals_enabled", False)),
            "max_notional_usd": float(getattr(self.settings, "rl_policy_max_notional_usd", 500.0)),
            "max_proposals_per_day": int(getattr(self.settings, "rl_policy_max_proposals_per_day", 1)),
            "latest_policy": latest.model_dump() if latest else None,
            "counts": counts,
            "blockers": self._base_blockers(include_policy=False),
        }

    def train(self) -> RLPolicyVersion:
        if not bool(getattr(self.settings, "rl_policy_enabled", False)):
            raise ValueError("rl_policy_disabled")
        if not bool(getattr(self.settings, "rl_policy_training_enabled", False)):
            raise ValueError("rl_policy_training_disabled")
        decisions = self.scan_decisions.list(limit=10_000)
        generated = self.strategy_lab.repository.list_generated(limit=1000)
        backtests = []
        for item in generated:
            backtest = self.strategy_lab.repository.latest_backtest(item.id)
            if backtest is not None:
                backtests.append(backtest)
        arms = self._build_arms(decisions=decisions, backtests=backtests)
        row_count = len(decisions) + sum(int(bt.metrics.get("number_of_trades") or 0) for bt in backtests if bt)
        accepted_rows = len([item for item in decisions if item.status in {"candidate", "watchlist"}])
        eligible_arms = {
            key: value
            for key, value in arms.items()
            if int(value.get("number_of_trades", 0) or 0) >= int(getattr(self.settings, "rl_policy_min_backtest_trades", 150))
            and float(value.get("profit_factor", 0.0) or 0.0) >= float(getattr(self.settings, "rl_policy_min_profit_factor", 1.2))
            and float(value.get("max_drawdown_pct", 999.0) or 999.0) <= float(getattr(self.settings, "rl_policy_max_drawdown_pct", 12.0))
            and float(value.get("expectancy_usd", 0.0) or 0.0) > 0.0
        }
        blockers: list[str] = []
        if not eligible_arms:
            blockers.append("no_strategy_arm_passed_rl_policy_gates")
        if row_count <= 0:
            blockers.append("empty_rl_training_dataset")
        dataset_version = "rl-dataset-" + hashlib.sha256(
            f"{row_count}:{accepted_rows}:{utc_now().date().isoformat()}".encode()
        ).hexdigest()[:12]
        policy = {
            "arms": arms,
            "eligible_arms": sorted(eligible_arms),
            "selection": "highest_reward_score_from_deterministic_candidates",
            "constraints": {
                "max_notional_usd": float(getattr(self.settings, "rl_policy_max_notional_usd", 500.0)),
                "paper_only": True,
                "broker": "alpaca",
            },
        }
        metrics = {
            "arm_count": len(arms),
            "eligible_arm_count": len(eligible_arms),
            "scan_decision_rows": len(decisions),
            "strategy_lab_backtests": len(backtests),
            "accepted_rows": accepted_rows,
        }
        version = RLPolicyVersion(
            status="paper_candidate" if not blockers else "blocked",
            dataset_version=dataset_version,
            row_count=row_count,
            accepted_rows=accepted_rows,
            metrics=metrics,
            policy=policy,
            blockers=blockers,
        )
        self.logs.log("rl_policy_trained", version.model_dump())
        return self.repository.record_version(version)

    def propose(self) -> RLPolicyProposal:
        blockers = self._base_blockers(include_policy=True)
        policy = self.repository.latest_version(eligible_only=True)
        if policy is None:
            blockers.append("rl_policy_missing")
        elif policy.status != "paper_candidate":
            blockers.extend(policy.blockers or ["rl_policy_not_paper_candidate"])
        if blockers:
            return self._record_blocked(blockers=blockers)
        candidates = self._candidate_snapshots(policy)
        if not candidates:
            return self._record_blocked(blockers=["no_deterministic_candidate_available"], policy=policy)
        snapshot, scan_decision, score = candidates[0]
        decision_key = f"rl:{policy.id}:{scan_decision.id}"
        existing = self.repository.get_proposal_by_key(decision_key)
        if existing is not None and existing.proposal_id:
            return existing
        if self._daily_proposal_limit_reached():
            return self._record_blocked(blockers=["rl_policy_daily_proposal_limit_reached"], policy=policy)
        metadata = self._policy_metadata(policy=policy, scan_decision_id=scan_decision.id, score=score)
        snapshot.metadata.update(metadata)
        request = self.adapter.build_proposal_request(
            snapshot,
            amount_usd=min(
                float(getattr(self.settings, "rl_policy_max_notional_usd", 500.0)),
                float(getattr(self.settings, "max_trade_amount_usd", 500.0)),
            ),
            notes="RL paper policy proposal; existing paper safety gates still apply.",
        )
        request.metadata = metadata
        proposal_record = self.repository.record_proposal(
            RLPolicyProposal(
                decision_key=decision_key,
                policy_version_id=policy.id,
                scan_decision_id=scan_decision.id,
                symbol=snapshot.symbol,
                strategy_name=snapshot.strategy_name,
                timeframe=snapshot.timeframe,
                status="proposed",
                score=score,
                metadata=metadata,
            )
        )
        try:
            proposal = self.proposals.create_proposal(request)
            processed = self.auto_trading.approve_enqueue_execute(proposal, snapshot)
        except Exception as exc:  # noqa: BLE001 - persist safety failure evidence
            self.logs.log("rl_policy_proposal_failed", {"decision_key": decision_key, "error": str(exc)})
            return self.repository.update_proposal_status(
                decision_key,
                status="blocked",
                blockers=[str(exc)],
            ) or proposal_record
        status = "queued" if processed is not None else "blocked"
        blockers_after = [] if processed is not None else ["auto_trading_policy_blocked"]
        return self.repository.update_proposal_status(
            decision_key,
            status=status,
            proposal_id=proposal.id,
            blockers=blockers_after,
            metadata={"queue_id": getattr(processed, "id", None), "queue_status": str(getattr(processed, "status", ""))},
        ) or proposal_record

    def _base_blockers(self, *, include_policy: bool) -> list[str]:
        blockers: list[str] = []
        if not bool(getattr(self.settings, "rl_policy_enabled", False)):
            blockers.append("rl_policy_disabled")
        if include_policy and not bool(getattr(self.settings, "rl_policy_paper_proposals_enabled", False)):
            blockers.append("rl_policy_paper_proposals_disabled")
        if self.settings.execution_mode != "paper" or bool(self.settings.enable_real_trading):
            blockers.append("paper_only_policy")
        if str(getattr(self.settings, "paper_broker", "") or "") != "alpaca":
            blockers.append("alpaca_paper_required")
        if str(getattr(self.settings, "alpaca_expected_account_number", "") or "") != "PA3B287XBZYU":
            blockers.append("unexpected_alpaca_account_config")
        blockers.extend(self.automation.execution_blockers())
        if not self.reconciliation.account_verified():
            blockers.append("alpaca_account_not_verified")
        if self.alpaca is None or not self.alpaca.is_regular_market_open():
            blockers.append("outside_regular_market_hours")
        if not bool(getattr(self.settings, "alpaca_require_bracket_orders", True)):
            blockers.append("bracket_orders_not_required")
        return sorted(set(blockers))

    def _candidate_snapshots(self, policy: RLPolicyVersion) -> list[tuple[LiveSignalSnapshot, Any, float]]:
        eligible = set(policy.policy.get("eligible_arms") or [])
        rows = self.scan_decisions.list(limit=500)
        candidates: list[tuple[LiveSignalSnapshot, Any, float]] = []
        for row in rows:
            if row.status not in {"candidate", "watchlist"} or not row.alert_eligible:
                continue
            arm_key = self._arm_key(row.strategy_name, row.timeframe)
            if arm_key not in eligible:
                continue
            try:
                snapshot = LiveSignalSnapshot.model_validate(row.payload)
            except Exception:
                continue
            if not self._strategy_paper_approved(snapshot.strategy_name):
                continue
            if not bool(snapshot.execution_ready):
                continue
            if str(snapshot.signal_role or "").lower() == "entry_short":
                continue
            if snapshot.stop_loss is None or snapshot.take_profit is None:
                continue
            if self.safety.is_blacklisted(snapshot.symbol):
                continue
            arm_score = float((policy.policy.get("arms") or {}).get(arm_key, {}).get("reward_score", 0.0) or 0.0)
            score = float(snapshot.score or 0.0) + arm_score
            if score < float(getattr(self.settings, "rl_policy_min_score_to_propose", 65.0)):
                continue
            candidates.append((snapshot, row, score))
        return sorted(candidates, key=lambda item: item[2], reverse=True)

    def _record_blocked(self, *, blockers: list[str], policy: RLPolicyVersion | None = None) -> RLPolicyProposal:
        decision_key = "rl:block:" + hashlib.sha256(
            f"{utc_now().date().isoformat()}:{','.join(sorted(blockers))}".encode()
        ).hexdigest()[:16]
        return self.repository.record_proposal(
            RLPolicyProposal(
                decision_key=decision_key,
                policy_version_id=policy.id if policy else None,
                symbol="N/A",
                strategy_name="rl_policy",
                status="blocked",
                score=0.0,
                blockers=sorted(set(blockers)),
                metadata={"source": "rl_policy"},
            )
        )

    def _policy_metadata(self, *, policy: RLPolicyVersion, scan_decision_id: int, score: float) -> dict[str, Any]:
        return {
            "source": "rl_policy",
            "policy_version_id": policy.id,
            "training_dataset_version": policy.dataset_version,
            "reward_model_version": policy.reward_model_version,
            "scan_decision_id": scan_decision_id,
            "rl_policy_score": round(score, 4),
        }

    def _daily_proposal_limit_reached(self) -> bool:
        start_of_day = utc_now().replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        return self.repository.proposal_count_since(start_of_day, statuses={"proposed", "queued"}) >= int(
            getattr(self.settings, "rl_policy_max_proposals_per_day", 1)
        )

    def _strategy_paper_approved(self, strategy_name: str) -> bool:
        if self.strategy_governance.strategy_paper_exploration_approved(strategy_name):
            return True
        return bool(self.strategy_governance.strategy_production_approved(strategy_name))

    @staticmethod
    def _arm_key(strategy_name: str, timeframe: str) -> str:
        return f"{strategy_name}:{timeframe}"

    def _build_arms(self, *, decisions: list[Any], backtests: list[Any]) -> dict[str, dict[str, Any]]:
        arms: dict[str, dict[str, Any]] = {}
        for decision in decisions:
            key = self._arm_key(decision.strategy_name, decision.timeframe)
            arm = arms.setdefault(
                key,
                {
                    "strategy_name": decision.strategy_name,
                    "timeframe": decision.timeframe,
                    "scan_rows": 0,
                    "candidate_rows": 0,
                    "average_score": 0.0,
                    "number_of_trades": 0,
                    "profit_factor": 0.0,
                    "max_drawdown_pct": 999.0,
                    "expectancy_usd": 0.0,
                },
            )
            count = int(arm["scan_rows"])
            score = float(decision.final_score or 0.0)
            arm["average_score"] = ((float(arm["average_score"]) * count) + score) / (count + 1)
            arm["scan_rows"] = count + 1
            if decision.status in {"candidate", "watchlist"}:
                arm["candidate_rows"] = int(arm["candidate_rows"]) + 1
        for backtest in backtests:
            if backtest is None:
                continue
            for result in backtest.results:
                key = self._arm_key(str(result.get("strategy_name") or ""), str(result.get("timeframe") or ""))
                if not key.strip(":"):
                    continue
                arm = arms.setdefault(
                    key,
                    {
                        "strategy_name": result.get("strategy_name"),
                        "timeframe": result.get("timeframe"),
                        "scan_rows": 0,
                        "candidate_rows": 0,
                        "average_score": 0.0,
                    },
                )
                arm["number_of_trades"] = max(int(arm.get("number_of_trades", 0) or 0), int(backtest.metrics.get("number_of_trades", 0) or 0))
                arm["profit_factor"] = max(float(arm.get("profit_factor", 0.0) or 0.0), float(backtest.metrics.get("profit_factor", 0.0) or 0.0))
                arm["max_drawdown_pct"] = min(float(arm.get("max_drawdown_pct", 999.0) or 999.0), float(backtest.metrics.get("max_drawdown_pct", 999.0) or 999.0))
                arm["expectancy_usd"] = max(float(arm.get("expectancy_usd", 0.0) or 0.0), float(backtest.metrics.get("expectancy_usd", 0.0) or 0.0))
        for arm in arms.values():
            arm["reward_score"] = round(
                float(arm.get("average_score", 0.0) or 0.0)
                + (float(arm.get("profit_factor", 0.0) or 0.0) * 5.0)
                + (float(arm.get("expectancy_usd", 0.0) or 0.0) * 0.1)
                - (float(arm.get("max_drawdown_pct", 0.0) or 0.0) * 0.5),
                4,
            )
        return arms
