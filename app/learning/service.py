"""Continuous learning orchestration with execution-safe failure isolation."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import pandas as pd

from app.learning.critic import TradeReviewClient, sanitize_evidence
from app.learning.modeling import LearningModelService, numeric_features
from app.models.learning import (
    DecisionSnapshot,
    ExperimentProposal,
    LearningJob,
    OutcomeLabel,
    TradeLifecycleEvent,
    TradeReview,
)
from app.utils.time import utc_now


class LearningService:
    def __init__(
        self,
        *,
        settings: Any,
        repository: Any,
        executions: Any,
        proposals: Any,
        market_data: Any,
        model_service: LearningModelService,
        critic: TradeReviewClient,
        run_logs: Any,
        notifier: Any | None = None,
    ):
        self.settings = settings
        self.repository = repository
        self.executions = executions
        self.proposals = proposals
        self.market_data = market_data
        self.models = model_service
        self.critic = critic
        self.logs = run_logs
        self.notifier = notifier

    def status(self) -> dict[str, Any]:
        return {
            **self.repository.status_counts(),
            "capture_enabled": bool(self.settings.learning_capture_enabled),
            "worker_enabled": bool(self.settings.learning_worker_enabled),
            "reviews_enabled": bool(self.settings.learning_reviews_enabled),
            "training_enabled": bool(self.settings.learning_training_enabled),
            "openai_enabled": bool(self.settings.learning_openai_enabled),
            "paper_auto_promotion_enabled": bool(self.settings.learning_auto_promote_paper_enabled),
        }

    def capture_scan_decision(self, record: Any, *, signal: Any | None = None) -> DecisionSnapshot | None:
        if not bool(self.settings.learning_capture_enabled):
            return None
        payload = dict(getattr(record, "payload", {}) or {})
        features = self._features_from_payload(payload)
        signal_payload = signal.model_dump() if hasattr(signal, "model_dump") else {}
        deterministic_eligible = bool(getattr(record, "alert_eligible", False))
        adjustment = self.models.safe_adjust(
            deterministic_score=float(getattr(record, "final_score", 0.0) or 0.0),
            deterministic_eligible=deterministic_eligible,
            features=features,
        )
        snapshot = DecisionSnapshot(
            decision_key=f"scan:{record.id}",
            signal_id=signal_payload.get("id"),
            symbol=record.symbol,
            strategy_name=record.strategy_name,
            timeframe=record.timeframe,
            stage="scan_decision",
            deterministic_eligible=deterministic_eligible,
            accepted=str(record.status) in {"candidate", "alerted"} and deterministic_eligible,
            deterministic_score=record.final_score,
            adjusted_score=adjustment["adjusted_score"],
            model_version_id=adjustment["model_version_id"],
            features=features,
            decision={
                "scan_decision_id": record.id,
                "scan_task": record.scan_task,
                "status": record.status,
                "reason_codes": record.reason_codes,
                "rejection_reasons": record.rejection_reasons,
                "signal": signal_payload,
                "model_adjustment": adjustment,
            },
            created_at=record.created_at,
        )
        persisted = self.repository.record_decision(snapshot)
        self.models.record_drift(features)
        return persisted

    def apply_to_snapshot(self, snapshot: Any) -> Any:
        """Only reduce an already deterministic score; never create eligibility."""

        features = self._features_from_payload(snapshot.model_dump())
        deterministic_eligible = bool(snapshot.execution_ready and snapshot.metadata.get("alert_eligible"))
        result = self.models.safe_adjust(
            deterministic_score=float(snapshot.score),
            deterministic_eligible=deterministic_eligible,
            features=features,
        )
        update: dict[str, Any] = {
            "score": min(float(snapshot.score), float(result["adjusted_score"])),
            "metadata": {
                **dict(snapshot.metadata),
                "meta_model": result,
            },
        }
        if result["model_blocked"] and deterministic_eligible:
            update["tradable"] = False
            update["execution_ready"] = False
            update["reject_reasons"] = [*snapshot.reject_reasons, "meta_model_rejected"]
            update["metadata"]["alert_eligible"] = False
            update["metadata"]["execution_ready"] = False
            update["metadata"]["execution_blockers"] = [
                *list(snapshot.metadata.get("execution_blockers") or []),
                "meta_model_rejected",
            ]
        return snapshot.model_copy(update=update)

    def record_execution_event(self, execution: Any, *, event_type: str) -> TradeLifecycleEvent | None:
        if not bool(self.settings.learning_capture_enabled):
            return None
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "status": execution.status,
                    "realized_pnl_usd": execution.realized_pnl_usd,
                    "response": execution.response_payload,
                },
                sort_keys=True,
                default=str,
            ).encode()
        ).hexdigest()[:16]
        event = self.repository.record_event(
            TradeLifecycleEvent(
                event_key=f"execution:{execution.id}:{event_type}:{fingerprint}",
                execution_id=execution.id,
                proposal_id=execution.proposal_id,
                broker_order_id=execution.broker_order_id,
                event_type=event_type,
                payload=sanitize_evidence(
                    {
                        "status": execution.status,
                        "request": execution.request_payload,
                        "response": execution.response_payload,
                        "realized_pnl_usd": execution.realized_pnl_usd,
                    }
                ),
                occurred_at=execution.updated_at,
            )
        )
        decision = self._ensure_execution_decision(execution)
        if self._closed(execution):
            label = self._real_label(decision, execution)
            self.repository.record_label(label)
            self.repository.enqueue_job(
                LearningJob(
                    idempotency_key=f"review:{execution.id}",
                    job_type="review_trade",
                    payload={"execution_id": execution.id},
                )
            )
        return event

    def record_proposal_event(
        self,
        proposal: Any,
        *,
        event_type: str,
        payload: dict[str, Any],
    ) -> TradeLifecycleEvent | None:
        if not bool(self.settings.learning_capture_enabled):
            return None
        sanitized = sanitize_evidence(payload)
        fingerprint = hashlib.sha256(
            json.dumps(sanitized, sort_keys=True, default=str).encode()
        ).hexdigest()[:16]
        return self.repository.record_event(
            TradeLifecycleEvent(
                event_key=f"proposal:{proposal.id}:{event_type}:{fingerprint}",
                proposal_id=proposal.id,
                event_type=event_type,
                payload=sanitized,
            )
        )

    def generate_counterfactuals(self, *, limit: int = 100) -> int:
        created = 0
        for decision in self.repository.list_decisions(limit=limit):
            if decision.accepted or self.repository.has_label(decision.id, "counterfactual"):
                continue
            signal = dict(decision.decision.get("signal") or {})
            entry = float(signal.get("price") or 0.0)
            stop = float(signal.get("stop_loss") or 0.0)
            target = float(signal.get("take_profit") or 0.0)
            if not entry or not stop or not target:
                continue
            try:
                history = self.market_data.get_history(
                    decision.symbol,
                    timeframe=decision.timeframe,
                    bars=500,
                    force_refresh=False,
                )
                cutoff = pd.Timestamp(decision.created_at)
                if cutoff.tzinfo is None:
                    cutoff = cutoff.tz_localize("UTC")
                timestamps = pd.to_datetime(history["timestamp"], utc=True)
                future = history[timestamps > cutoff]
            except Exception:
                continue
            status = "timed_exit"
            exit_price = float(future.iloc[-1]["close"]) if len(future) else entry
            for _, bar in future.head(100).iterrows():
                if float(bar["low"]) <= stop:
                    status, exit_price = "stop_hit", stop
                    break
                if float(bar["high"]) >= target:
                    status, exit_price = "target_hit", target
                    break
            risk = max(entry - stop, 0.01)
            net_r = (exit_price - entry) / risk
            self.repository.record_label(
                OutcomeLabel(
                    decision_snapshot_id=decision.id,
                    label_type="counterfactual",
                    status=status,
                    net_r=net_r,
                    profitable=net_r > 0,
                    source="market_data_replay",
                    horizon="100_bars",
                    details={"entry": entry, "stop": stop, "target": target, "exit": exit_price},
                )
            )
            created += 1
        return created

    def schedule_due_jobs(self) -> list[LearningJob]:
        if not bool(self.settings.learning_worker_enabled):
            return []
        now = utc_now()
        jobs = [
            LearningJob(
                idempotency_key=f"counterfactual:{now.date().isoformat()}",
                job_type="counterfactual_labels",
            )
        ]
        if bool(self.settings.learning_training_enabled) and now.hour >= int(self.settings.learning_nightly_training_hour_utc):
            jobs.append(
                LearningJob(
                    idempotency_key=f"nightly-train:{now.date().isoformat()}",
                    job_type="train_challenger",
                )
            )
        if (
            bool(self.settings.learning_training_enabled)
            and now.weekday() == int(self.settings.learning_weekly_evaluation_weekday)
        ):
            jobs.append(
                LearningJob(
                    idempotency_key=f"weekly-evaluate:{now.date().isoformat()}",
                    job_type="evaluate_challengers",
                )
            )
        if (
            bool(self.settings.learning_reviews_enabled)
            and bool(self.settings.learning_openai_enabled)
            and now.weekday() == int(self.settings.learning_weekly_evaluation_weekday)
        ):
            jobs.append(
                LearningJob(
                    idempotency_key=f"weekly-synthesis:{now.date().isoformat()}",
                    job_type="weekly_synthesis",
                )
            )
        return [self.repository.enqueue_job(job) for job in jobs]

    def process_jobs(self, *, limit: int = 10) -> list[LearningJob]:
        if not bool(self.settings.learning_worker_enabled):
            return []
        completed: list[LearningJob] = []
        for job in self.repository.list_jobs(status="pending", limit=limit):
            job.status = "running"
            job.attempts += 1
            job.started_at = utc_now().isoformat()
            self.repository.update_job(job)
            try:
                job.result = self._run_job(job)
                job.status = "completed"
                job.error = ""
            except Exception as exc:
                job.status = (
                    "pending"
                    if job.attempts < int(self.settings.learning_job_max_attempts)
                    else "failed"
                )
                job.error = str(exc)
                self.logs.log(
                    "learning_job_failed",
                    {
                        "job_id": job.id,
                        "job_type": job.job_type,
                        "error": str(exc),
                        "attempts": job.attempts,
                        "will_retry": job.status == "pending",
                    },
                )
                if job.status == "failed":
                    self._notify_critical(f"Learning pipeline failed: {job.job_type}\n{exc}")
            job.completed_at = utc_now().isoformat() if job.status != "pending" else None
            self.repository.update_job(job)
            completed.append(job)
        return completed

    def review_execution(self, execution_id: str, *, retry: bool = False) -> TradeReview:
        existing = self.repository.get_review(execution_id)
        if existing is not None and not retry:
            return existing
        execution = self.executions.get(execution_id)
        if execution is None:
            raise KeyError(f"Execution {execution_id} not found")
        deterministic = self._deterministic_review(execution)
        review_payload = deterministic
        reviewer = "deterministic"
        critic_model = ""
        estimated_cost = 0.0
        if bool(self.settings.learning_reviews_enabled) and self._critic_budget_available():
            try:
                advisory = self.critic.review(sanitize_evidence(deterministic["evidence"]))
                review_payload = {
                    **deterministic,
                    "summary": advisory["summary"],
                    "confidence": advisory["confidence"],
                    "failure_categories": list(
                        dict.fromkeys([*deterministic["failure_categories"], *advisory["failure_categories"]])
                    ),
                    "findings": [*deterministic["findings"], *advisory["findings"]],
                    "experiments": advisory["experiments"],
                }
                reviewer = "hybrid"
                critic_model = self.critic.model_name
                estimated_cost = float(self.settings.learning_openai_estimated_review_cost_usd)
            except Exception as exc:
                review_payload["details"]["critic_error"] = str(exc)
        review = TradeReview(
            execution_id=execution_id,
            status="completed",
            reviewer=reviewer,
            summary=review_payload["summary"],
            findings=review_payload["findings"],
            failure_categories=review_payload["failure_categories"],
            confidence=review_payload["confidence"],
            critic_model=critic_model,
            estimated_cost_usd=estimated_cost,
            details=review_payload["details"],
        )
        persisted = self.repository.record_review(review)
        if existing is None:
            for proposal in review_payload.get("experiments") or []:
                self.repository.record_experiment(
                    ExperimentProposal(
                        trade_review_id=persisted.id,
                        title=proposal["title"],
                        hypothesis=proposal["hypothesis"],
                        scope=proposal["scope"],
                        details={"advisory_only": True},
                    )
                )
        return persisted

    def daily_digest(self) -> str:
        reviews = self.repository.list_reviews(limit=20)
        status = self.status()
        categories: dict[str, int] = {}
        for review in reviews:
            for category in review.failure_categories:
                categories[category] = categories.get(category, 0) + 1
        top = ", ".join(f"{key}:{value}" for key, value in sorted(categories.items(), key=lambda x: -x[1])[:5])
        return "\n".join(
            [
                "Learning daily digest",
                f"Reviews: {status['reviews']} | pending jobs: {status['pending_jobs']} | failed jobs: {status['failed_jobs']}",
                f"Active model: {status['active_model_version'] or 'none'} | mode: {status['model_deployment_mode']}",
                f"Top findings: {top or 'none'}",
            ]
        )

    def weekly_synthesis(self) -> dict[str, Any]:
        if not bool(self.settings.learning_reviews_enabled) or not bool(
            self.settings.learning_openai_enabled
        ):
            return {"status": "disabled", "experiments": 0}
        cost = float(self.settings.learning_openai_estimated_weekly_cost_usd)
        if not self._critic_budget_available(estimated_cost=cost):
            return {"status": "budget_blocked", "experiments": 0}
        reviews = [
            sanitize_evidence(review.model_dump(exclude={"id", "execution_id", "outcome_label_id"}))
            for review in self.repository.list_reviews(limit=200)
        ]
        advisory = self.critic.synthesize({"reviews": reviews})
        created = 0
        for proposal in advisory.get("experiments") or []:
            self.repository.record_experiment(
                ExperimentProposal(
                    title=proposal["title"],
                    hypothesis=proposal["hypothesis"],
                    scope=proposal["scope"],
                    details={
                        "advisory_only": True,
                        "source": "weekly_synthesis",
                        "critic_model": self.critic.weekly_model_name,
                    },
                )
            )
            created += 1
        return {
            "status": "completed",
            "summary": advisory.get("summary") or "",
            "experiments": created,
            "critic_model": self.critic.weekly_model_name,
            "estimated_cost_usd": cost,
        }

    def _run_job(self, job: LearningJob) -> dict[str, Any]:
        if job.job_type == "review_trade":
            return self.review_execution(str(job.payload["execution_id"])).model_dump()
        if job.job_type == "counterfactual_labels":
            return {"created": self.generate_counterfactuals()}
        if job.job_type == "train_challenger":
            return self.models.train_challenger().model_dump()
        if job.job_type == "evaluate_challengers":
            results = []
            rollback = self.models.rollback_if_unsafe()
            if rollback is not None:
                self._notify_critical(
                    "Learning model automatically rolled back: "
                    + ", ".join(rollback.blockers)
                )
            for model in self.repository.list_models(limit=100):
                if model.status != "challenger":
                    continue
                evaluation = self.models.evaluate_challenger(model.id)
                results.append(evaluation.model_dump())
                if not evaluation.blockers and bool(self.settings.learning_auto_promote_paper_enabled):
                    promotion = self.models.promote(model.id, target_mode="paper")
                    if promotion.approved:
                        self._notify_critical(f"Learning model promoted to paper: {model.id}")
            return {
                "evaluations": results,
                "rollback": rollback.model_dump() if rollback is not None else None,
            }
        if job.job_type == "weekly_synthesis":
            return self.weekly_synthesis()
        raise ValueError(f"Unsupported learning job type: {job.job_type}")

    def _ensure_execution_decision(self, execution: Any) -> DecisionSnapshot:
        key = f"execution:{execution.id}"
        existing = self.repository.get_decision_by_key(key)
        if existing:
            return existing
        request = dict(execution.request_payload or {})
        features = numeric_features({**request, **dict(request.get("metadata") or {})})
        return self.repository.record_decision(
            DecisionSnapshot(
                decision_key=key,
                execution_id=execution.id,
                symbol=str(request.get("symbol") or ""),
                strategy_name=str(request.get("strategy_name") or ""),
                timeframe=str(dict(request.get("metadata") or {}).get("timeframe") or "1d"),
                stage="execution",
                deterministic_eligible=True,
                accepted=True,
                deterministic_score=float(dict(request.get("metadata") or {}).get("score") or 0.0),
                adjusted_score=float(dict(request.get("metadata") or {}).get("score") or 0.0),
                features=features,
                decision={"request": sanitize_evidence(request), "proposal_id": execution.proposal_id},
                created_at=execution.created_at,
            )
        )

    def _real_label(self, decision: DecisionSnapshot, execution: Any) -> OutcomeLabel:
        request = dict(execution.request_payload or {})
        entry = float(request.get("proposed_price") or 0.0)
        stop = float(request.get("stop_loss") or 0.0)
        amount = float(request.get("amount_usd") or 0.0)
        risk_amount = abs(entry - stop) / entry * amount if entry > 0 and stop > 0 else 0.0
        pnl = float(execution.realized_pnl_usd or 0.0)
        return OutcomeLabel(
            decision_snapshot_id=decision.id,
            label_type="real",
            status="closed",
            net_pnl_usd=pnl,
            net_r=pnl / risk_amount if risk_amount > 0 else None,
            profitable=pnl > 0,
            source="broker_reconciliation",
            horizon="trade_lifecycle",
            details={"execution_id": execution.id, "risk_amount_usd": risk_amount},
        )

    def _deterministic_review(self, execution: Any) -> dict[str, Any]:
        request = dict(execution.request_payload or {})
        broker = dict((execution.response_payload or {}).get("broker_execution") or {})
        proposed = float(request.get("proposed_price") or 0.0)
        fill = float(broker.get("filled_avg_price") or 0.0)
        slippage_bps = abs(fill - proposed) / proposed * 10_000 if proposed > 0 and fill > 0 else 0.0
        pnl = float(execution.realized_pnl_usd or 0.0)
        categories = []
        findings = []
        if pnl < 0:
            categories.append("losing_trade")
        if slippage_bps > float(self.settings.execution_max_entry_drift_bps):
            categories.append("excessive_slippage")
            findings.append(
                {"category": "execution", "observation": "Fill slippage exceeded configured entry drift", "evidence": f"{slippage_bps:.2f} bps"}
            )
        summary = "Winning trade with controlled execution." if pnl > 0 else "Trade closed without positive realized P&L."
        return {
            "summary": summary,
            "confidence": 0.8,
            "failure_categories": categories,
            "findings": findings,
            "experiments": [],
            "details": {"pnl_usd": pnl, "slippage_bps": slippage_bps},
            "evidence": {
                "strategy_name": request.get("strategy_name"),
                "symbol": request.get("symbol"),
                "proposed_price": proposed,
                "stop_loss": request.get("stop_loss"),
                "take_profit": request.get("take_profit"),
                "realized_pnl_usd": pnl,
                "slippage_bps": slippage_bps,
                "broker_execution": broker,
            },
        }

    def _critic_budget_available(self, *, estimated_cost: float | None = None) -> bool:
        now = utc_now().astimezone(UTC)
        since = datetime(now.year, now.month, now.day, tzinfo=UTC).isoformat()
        requested = (
            float(estimated_cost)
            if estimated_cost is not None
            else float(self.settings.learning_openai_estimated_review_cost_usd)
        )
        return self.repository.usage_cost_since(since) + requested <= float(
            self.settings.learning_openai_daily_budget_usd
        )

    @staticmethod
    def _closed(execution: Any) -> bool:
        broker = dict((execution.response_payload or {}).get("broker_execution") or {})
        return any(
            str(leg.get("status") or "").lower() == "filled"
            and str(leg.get("side") or "").lower() == "sell"
            for leg in list(broker.get("legs") or [])
        )

    def _notify_critical(self, message: str) -> None:
        if self.notifier is None or not hasattr(self.notifier, "send_text"):
            return
        try:
            self.notifier.send_text(message)
        except Exception:
            return

    @staticmethod
    def _features_from_payload(payload: dict[str, Any]) -> dict[str, float]:
        sources = [
            payload,
            dict(payload.get("measurements") or {}),
            dict(payload.get("indicators") or {}),
            dict(payload.get("metadata") or {}),
            dict(payload.get("score_breakdown") or {}),
            dict(payload.get("market_intelligence") or {}),
        ]
        result: dict[str, float] = {}
        for source in sources:
            result.update(numeric_features(source))
        return result
