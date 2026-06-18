"""Persistence for governed learning evidence and jobs."""

from __future__ import annotations

import json
from typing import Any

from app.models.learning import (
    DecisionSnapshot,
    ExperimentProposal,
    LearningDatasetVersion,
    LearningDriftSnapshot,
    LearningJob,
    MetaModelEvaluation,
    MetaModelVersion,
    ModelPromotionDecision,
    OutcomeLabel,
    TradeLifecycleEvent,
    TradeReview,
)
from app.storage.db import Database
from app.utils.time import utc_now


class LearningRepository:
    def __init__(self, db: Database):
        self.db = db

    def record_decision(self, item: DecisionSnapshot) -> DecisionSnapshot:
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO learning_decision_snapshots (
                    id, decision_key, signal_id, execution_id, symbol, strategy_name,
                    timeframe, stage, deterministic_eligible, accepted,
                    deterministic_score, adjusted_score, model_version_id,
                    features_json, decision_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(decision_key) DO NOTHING
                """,
                (
                    item.id,
                    item.decision_key,
                    item.signal_id,
                    item.execution_id,
                    item.symbol,
                    item.strategy_name,
                    item.timeframe,
                    item.stage,
                    int(item.deterministic_eligible),
                    int(item.accepted),
                    item.deterministic_score,
                    item.adjusted_score,
                    item.model_version_id,
                    json.dumps(item.features, sort_keys=True),
                    json.dumps(item.decision, sort_keys=True),
                    item.created_at,
                ),
            )
        return self.get_decision_by_key(item.decision_key) or item

    def get_decision_by_key(self, decision_key: str) -> DecisionSnapshot | None:
        return self._one(
            "SELECT * FROM learning_decision_snapshots WHERE decision_key = ?",
            (decision_key,),
            self._decision,
        )

    def list_decisions(self, *, limit: int = 500) -> list[DecisionSnapshot]:
        return self._many(
            "SELECT * FROM learning_decision_snapshots ORDER BY created_at DESC LIMIT ?",
            (max(limit, 1),),
            self._decision,
        )

    def shadow_sessions_for_model(self, model_id: str) -> int:
        return len(
            {
                decision.created_at[:10]
                for decision in self.list_decisions(limit=100_000)
                if decision.model_version_id == model_id
            }
        )

    def record_label(self, item: OutcomeLabel) -> OutcomeLabel:
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO learning_outcome_labels (
                    id, decision_snapshot_id, label_type, status, net_pnl_usd,
                    net_r, profitable, source, horizon, details_json, labeled_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(decision_snapshot_id, label_type, source, horizon) DO NOTHING
                """,
                (
                    item.id,
                    item.decision_snapshot_id,
                    item.label_type,
                    item.status,
                    item.net_pnl_usd,
                    item.net_r,
                    None if item.profitable is None else int(item.profitable),
                    item.source,
                    item.horizon,
                    json.dumps(item.details, sort_keys=True),
                    item.labeled_at,
                ),
            )
        return item

    def has_label(self, decision_snapshot_id: str, label_type: str) -> bool:
        with self.db.connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM learning_outcome_labels
                WHERE decision_snapshot_id = ? AND label_type = ? LIMIT 1
                """,
                (decision_snapshot_id, label_type),
            ).fetchone()
        return row is not None

    def list_labeled_decisions(self, *, limit: int = 10000) -> list[dict[str, Any]]:
        with self.db.connect() as connection:
            rows = connection.execute(
                """
                SELECT d.*, l.id AS label_id, l.label_type, l.status AS label_status,
                       l.net_pnl_usd, l.net_r, l.profitable, l.source, l.horizon,
                       l.details_json AS label_details_json, l.labeled_at
                FROM learning_decision_snapshots AS d
                JOIN learning_outcome_labels AS l ON l.decision_snapshot_id = d.id
                ORDER BY d.created_at ASC LIMIT ?
                """,
                (max(limit, 1),),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["features"] = json.loads(item.pop("features_json") or "{}")
            item["decision"] = json.loads(item.pop("decision_json") or "{}")
            item["label_details"] = json.loads(item.pop("label_details_json") or "{}")
            item["accepted"] = bool(item["accepted"])
            item["deterministic_eligible"] = bool(item["deterministic_eligible"])
            item["profitable"] = None if item["profitable"] is None else bool(item["profitable"])
            result.append(item)
        return result

    def record_event(self, item: TradeLifecycleEvent) -> TradeLifecycleEvent:
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO learning_lifecycle_events (
                    id, event_key, execution_id, proposal_id, broker_order_id,
                    event_type, payload_json, occurred_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_key) DO NOTHING
                """,
                (
                    item.id,
                    item.event_key,
                    item.execution_id,
                    item.proposal_id,
                    item.broker_order_id,
                    item.event_type,
                    json.dumps(item.payload, sort_keys=True),
                    item.occurred_at,
                ),
            )
        return item

    def record_review(self, item: TradeReview) -> TradeReview:
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO learning_trade_reviews (
                    id, execution_id, outcome_label_id, status, reviewer, summary,
                    findings_json, failure_categories_json, confidence, critic_model,
                    estimated_cost_usd, details_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(execution_id) DO UPDATE SET
                    outcome_label_id = excluded.outcome_label_id,
                    status = excluded.status,
                    reviewer = excluded.reviewer,
                    summary = excluded.summary,
                    findings_json = excluded.findings_json,
                    failure_categories_json = excluded.failure_categories_json,
                    confidence = excluded.confidence,
                    critic_model = excluded.critic_model,
                    estimated_cost_usd = excluded.estimated_cost_usd,
                    details_json = excluded.details_json,
                    created_at = excluded.created_at
                """,
                (
                    item.id,
                    item.execution_id,
                    item.outcome_label_id,
                    item.status,
                    item.reviewer,
                    item.summary,
                    json.dumps(item.findings, sort_keys=True),
                    json.dumps(item.failure_categories),
                    item.confidence,
                    item.critic_model,
                    item.estimated_cost_usd,
                    json.dumps(item.details, sort_keys=True),
                    item.created_at,
                ),
            )
        return self.get_review(item.execution_id) or item

    def get_review(self, execution_id: str) -> TradeReview | None:
        return self._one(
            "SELECT * FROM learning_trade_reviews WHERE execution_id = ?",
            (execution_id,),
            self._review,
        )

    def list_reviews(self, *, limit: int = 200) -> list[TradeReview]:
        return self._many(
            "SELECT * FROM learning_trade_reviews ORDER BY created_at DESC LIMIT ?",
            (max(limit, 1),),
            self._review,
        )

    def review_cost_since(self, since_iso: str) -> float:
        with self.db.connect() as connection:
            row = connection.execute(
                """
                SELECT COALESCE(SUM(estimated_cost_usd), 0) AS value
                FROM learning_trade_reviews WHERE created_at >= ?
                """,
                (since_iso,),
            ).fetchone()
        return float(row["value"] if row else 0.0)

    def usage_cost_since(self, since_iso: str) -> float:
        total = self.review_cost_since(since_iso)
        for job in self.list_jobs(limit=1000):
            if job.scheduled_at >= since_iso and job.job_type == "weekly_synthesis":
                total += float(job.result.get("estimated_cost_usd") or 0.0)
        return total

    def record_experiment(self, item: ExperimentProposal) -> ExperimentProposal:
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO learning_experiments (
                    id, trade_review_id, title, hypothesis, scope, status, details_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.id,
                    item.trade_review_id,
                    item.title,
                    item.hypothesis,
                    item.scope,
                    item.status,
                    json.dumps(item.details, sort_keys=True),
                    item.created_at,
                ),
            )
        return item

    def list_experiments(self, *, limit: int = 200) -> list[ExperimentProposal]:
        return self._many(
            "SELECT * FROM learning_experiments ORDER BY created_at DESC LIMIT ?",
            (max(limit, 1),),
            lambda row: ExperimentProposal.model_validate(
                {**dict(row), "details": json.loads(row["details_json"] or "{}")}
            ),
        )

    def record_dataset(self, item: LearningDatasetVersion) -> LearningDatasetVersion:
        self._insert_model(
            """
            INSERT INTO learning_dataset_versions (
                id, status, row_count, accepted_oos_trades, feature_schema_hash,
                source_cutoff_at, train_start_at, train_end_at, holdout_start_at,
                holdout_end_at, artifact_uri, details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.id,
                item.status,
                item.row_count,
                item.accepted_oos_trades,
                item.feature_schema_hash,
                item.source_cutoff_at,
                item.train_start_at,
                item.train_end_at,
                item.holdout_start_at,
                item.holdout_end_at,
                item.artifact_uri,
                json.dumps(item.details, sort_keys=True),
                item.created_at,
            ),
        )
        return item

    def record_model(self, item: MetaModelVersion) -> MetaModelVersion:
        self._insert_model(
            """
            INSERT INTO learning_meta_model_versions (
                id, dataset_version_id, parent_version_id, model_type, status,
                deployment_mode, feature_names_json, artifact_uri, artifact_hash,
                metrics_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.id,
                item.dataset_version_id,
                item.parent_version_id,
                item.model_type,
                item.status,
                item.deployment_mode,
                json.dumps(item.feature_names),
                item.artifact_uri,
                item.artifact_hash,
                json.dumps(item.metrics, sort_keys=True),
                item.created_at,
            ),
        )
        return item

    def update_model_status(self, model_id: str, *, status: str, deployment_mode: str) -> None:
        with self.db.connect() as connection:
            connection.execute(
                "UPDATE learning_meta_model_versions SET status = ?, deployment_mode = ? WHERE id = ?",
                (status, deployment_mode, model_id),
            )

    def get_model(self, model_id: str) -> MetaModelVersion | None:
        return self._one(
            "SELECT * FROM learning_meta_model_versions WHERE id = ?",
            (model_id,),
            self._model,
        )

    def champion(self) -> MetaModelVersion | None:
        return self._one(
            """
            SELECT * FROM learning_meta_model_versions
            WHERE status = 'champion' AND deployment_mode IN ('paper','live')
            ORDER BY created_at DESC LIMIT 1
            """,
            (),
            self._model,
        )

    def list_models(self, *, limit: int = 200) -> list[MetaModelVersion]:
        return self._many(
            "SELECT * FROM learning_meta_model_versions ORDER BY created_at DESC LIMIT ?",
            (max(limit, 1),),
            self._model,
        )

    def record_evaluation(self, item: MetaModelEvaluation) -> MetaModelEvaluation:
        self._insert_model(
            """
            INSERT INTO learning_model_evaluations (
                id, model_version_id, champion_version_id, status, metrics_json,
                blockers_json, shadow_sessions, leakage_passed, schema_passed, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.id,
                item.model_version_id,
                item.champion_version_id,
                item.status,
                json.dumps(item.metrics, sort_keys=True),
                json.dumps(item.blockers),
                item.shadow_sessions,
                int(item.leakage_passed),
                int(item.schema_passed),
                item.created_at,
            ),
        )
        return item

    def latest_evaluation(self, model_id: str) -> MetaModelEvaluation | None:
        return self._one(
            """
            SELECT * FROM learning_model_evaluations
            WHERE model_version_id = ? ORDER BY created_at DESC LIMIT 1
            """,
            (model_id,),
            lambda row: MetaModelEvaluation.model_validate(
                {
                    **dict(row),
                    "metrics": json.loads(row["metrics_json"] or "{}"),
                    "blockers": json.loads(row["blockers_json"] or "[]"),
                    "leakage_passed": bool(row["leakage_passed"]),
                    "schema_passed": bool(row["schema_passed"]),
                }
            ),
        )

    def record_promotion(self, item: ModelPromotionDecision) -> ModelPromotionDecision:
        self._insert_model(
            """
            INSERT INTO learning_model_promotions (
                id, model_version_id, target_mode, approved, signed_by,
                blockers_json, evidence_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.id,
                item.model_version_id,
                item.target_mode,
                int(item.approved),
                item.signed_by,
                json.dumps(item.blockers),
                json.dumps(item.evidence, sort_keys=True),
                item.created_at,
            ),
        )
        return item

    def record_drift(self, item: LearningDriftSnapshot) -> LearningDriftSnapshot:
        self._insert_model(
            """
            INSERT INTO learning_drift_snapshots (
                id, model_version_id, drift_score, excessive, feature_drift_json,
                details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                item.id,
                item.model_version_id,
                item.drift_score,
                int(item.excessive),
                json.dumps(item.feature_drift, sort_keys=True),
                json.dumps(item.details, sort_keys=True),
                item.created_at,
            ),
        )
        return item

    def list_drift(self, *, limit: int = 200) -> list[LearningDriftSnapshot]:
        return self._many(
            "SELECT * FROM learning_drift_snapshots ORDER BY created_at DESC LIMIT ?",
            (max(limit, 1),),
            lambda row: LearningDriftSnapshot.model_validate(
                {
                    **dict(row),
                    "excessive": bool(row["excessive"]),
                    "feature_drift": json.loads(row["feature_drift_json"] or "{}"),
                    "details": json.loads(row["details_json"] or "{}"),
                }
            ),
        )

    def enqueue_job(self, item: LearningJob) -> LearningJob:
        with self.db.connect() as connection:
            connection.execute(
                """
                INSERT INTO learning_jobs (
                    id, idempotency_key, job_type, status, payload_json, result_json,
                    error, attempts, scheduled_at, started_at, completed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(idempotency_key) DO NOTHING
                """,
                (
                    item.id,
                    item.idempotency_key,
                    item.job_type,
                    item.status,
                    json.dumps(item.payload, sort_keys=True),
                    json.dumps(item.result, sort_keys=True),
                    item.error,
                    item.attempts,
                    item.scheduled_at,
                    item.started_at,
                    item.completed_at,
                ),
            )
        return self.get_job_by_key(item.idempotency_key) or item

    def get_job_by_key(self, key: str) -> LearningJob | None:
        return self._one(
            "SELECT * FROM learning_jobs WHERE idempotency_key = ?",
            (key,),
            self._job,
        )

    def list_jobs(self, *, status: str | None = None, limit: int = 100) -> list[LearningJob]:
        query = "SELECT * FROM learning_jobs"
        params: tuple[Any, ...] = ()
        if status:
            query += " WHERE status = ?"
            params = (status,)
        query += " ORDER BY scheduled_at ASC LIMIT ?"
        params = (*params, max(limit, 1))
        return self._many(query, params, self._job)

    def update_job(self, item: LearningJob) -> LearningJob:
        with self.db.connect() as connection:
            connection.execute(
                """
                UPDATE learning_jobs SET status = ?, result_json = ?, error = ?,
                    attempts = ?, started_at = ?, completed_at = ? WHERE id = ?
                """,
                (
                    item.status,
                    json.dumps(item.result, sort_keys=True),
                    item.error,
                    item.attempts,
                    item.started_at,
                    item.completed_at,
                    item.id,
                ),
            )
        return item

    def status_counts(self) -> dict[str, Any]:
        with self.db.connect() as connection:
            result: dict[str, Any] = {}
            for key, query in {
                "decisions": "SELECT COUNT(*) FROM learning_decision_snapshots",
                "labels": "SELECT COUNT(*) FROM learning_outcome_labels",
                "reviews": "SELECT COUNT(*) FROM learning_trade_reviews",
                "experiments": "SELECT COUNT(*) FROM learning_experiments",
                "models": "SELECT COUNT(*) FROM learning_meta_model_versions",
                "pending_jobs": "SELECT COUNT(*) FROM learning_jobs WHERE status = 'pending'",
                "failed_jobs": "SELECT COUNT(*) FROM learning_jobs WHERE status = 'failed'",
                "excessive_drift": "SELECT COUNT(*) FROM learning_drift_snapshots WHERE excessive = 1",
            }.items():
                result[key] = int(connection.execute(query).fetchone()[0])
        champion = self.champion()
        result["active_model_version"] = champion.id if champion else None
        result["model_deployment_mode"] = champion.deployment_mode if champion else "shadow"
        result["review_cost_today_usd"] = self.usage_cost_since(utc_now().date().isoformat())
        return result

    def _insert_model(self, query: str, values: tuple[Any, ...]) -> None:
        with self.db.connect() as connection:
            connection.execute(query, values)

    def _one(self, query: str, params: tuple[Any, ...], factory):
        with self.db.connect() as connection:
            row = connection.execute(query, params).fetchone()
        return None if row is None else factory(row)

    def _many(self, query: str, params: tuple[Any, ...], factory):
        with self.db.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [factory(row) for row in rows]

    @staticmethod
    def _decision(row: Any) -> DecisionSnapshot:
        payload = dict(row)
        payload["deterministic_eligible"] = bool(payload["deterministic_eligible"])
        payload["accepted"] = bool(payload["accepted"])
        payload["features"] = json.loads(payload.pop("features_json") or "{}")
        payload["decision"] = json.loads(payload.pop("decision_json") or "{}")
        return DecisionSnapshot.model_validate(payload)

    @staticmethod
    def _review(row: Any) -> TradeReview:
        payload = dict(row)
        payload["findings"] = json.loads(payload.pop("findings_json") or "[]")
        payload["failure_categories"] = json.loads(payload.pop("failure_categories_json") or "[]")
        payload["details"] = json.loads(payload.pop("details_json") or "{}")
        return TradeReview.model_validate(payload)

    @staticmethod
    def _model(row: Any) -> MetaModelVersion:
        payload = dict(row)
        payload["feature_names"] = json.loads(payload.pop("feature_names_json") or "[]")
        payload["metrics"] = json.loads(payload.pop("metrics_json") or "{}")
        return MetaModelVersion.model_validate(payload)

    @staticmethod
    def _job(row: Any) -> LearningJob:
        payload = dict(row)
        payload["payload"] = json.loads(payload.pop("payload_json") or "{}")
        payload["result"] = json.loads(payload.pop("result_json") or "{}")
        return LearningJob.model_validate(payload)
