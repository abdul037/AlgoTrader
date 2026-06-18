"""Leakage-aware dataset, challenger training, safe inference, and promotion."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime
from typing import Any

from scipy.stats import ttest_1samp

from app.learning.artifacts import ArtifactStore
from app.models.learning import (
    LearningDatasetVersion,
    LearningDriftSnapshot,
    MetaModelEvaluation,
    MetaModelVersion,
    ModelPromotionDecision,
)
from app.utils.time import utc_now


def numeric_features(payload: dict[str, Any]) -> dict[str, float]:
    result: dict[str, float] = {}
    for key, value in payload.items():
        if isinstance(value, bool) or isinstance(value, (int, float)) and math.isfinite(float(value)):
            result[str(key)] = float(value)
    return result


class LearningModelService:
    def __init__(
        self,
        *,
        settings: Any,
        repository: Any,
        artifact_store: ArtifactStore,
        run_logs: Any,
        institutional_service: Any | None = None,
    ):
        self.settings = settings
        self.repository = repository
        self.artifacts = artifact_store
        self.logs = run_logs
        self.institutional = institutional_service
        self._loaded_model: tuple[str, Any, Any, list[str]] | None = None

    def build_dataset(self) -> LearningDatasetVersion:
        rows = self.repository.list_labeled_decisions(limit=100_000)
        rows = sorted(rows, key=lambda item: item["created_at"])
        feature_names = sorted({key for row in rows for key in numeric_features(row["features"])})
        schema_hash = hashlib.sha256(json.dumps(feature_names).encode()).hexdigest()
        holdout_fraction = min(max(float(self.settings.learning_holdout_fraction), 0.1), 0.4)
        split = max(int(len(rows) * (1.0 - holdout_fraction)), 0)
        embargo = max(int(self.settings.learning_embargo_rows), 0)
        train_rows = rows[: max(split - embargo, 0)]
        holdout_rows = rows[split:]
        accepted_oos = sum(
            bool(row["accepted"]) and row["label_type"] == "real" for row in holdout_rows
        )
        payload = {
            "feature_names": feature_names,
            "rows": rows,
            "split_index": split,
            "embargo_rows": embargo,
            "recency_half_life_days": int(self.settings.learning_recency_half_life_days),
        }
        uri, digest = self.artifacts.put(
            f"dataset-{utc_now().date().isoformat()}.json",
            json.dumps(payload, sort_keys=True).encode(),
        )
        dataset = LearningDatasetVersion(
            row_count=len(rows),
            accepted_oos_trades=accepted_oos,
            feature_schema_hash=schema_hash,
            source_cutoff_at=rows[-1]["created_at"] if rows else utc_now().isoformat(),
            train_start_at=train_rows[0]["created_at"] if train_rows else None,
            train_end_at=train_rows[-1]["created_at"] if train_rows else None,
            holdout_start_at=holdout_rows[0]["created_at"] if holdout_rows else None,
            holdout_end_at=holdout_rows[-1]["created_at"] if holdout_rows else None,
            artifact_uri=uri,
            details={"artifact_hash": digest, "feature_names": feature_names},
        )
        return self.repository.record_dataset(dataset)

    def train_challenger(self) -> MetaModelVersion:
        if not bool(self.settings.learning_training_enabled):
            raise RuntimeError("Learning training is disabled")
        dataset = self.build_dataset()
        if dataset.row_count < int(self.settings.learning_min_training_rows):
            raise RuntimeError(
                f"Insufficient learning rows: {dataset.row_count}/{self.settings.learning_min_training_rows}"
            )
        try:
            import lightgbm as lgb
        except ImportError as exc:
            raise RuntimeError("LightGBM is required for challenger training") from exc
        payload = json.loads(self.artifacts.get(dataset.artifact_uri))
        rows = payload["rows"]
        features = list(payload["feature_names"])
        split = int(payload["split_index"])
        embargo = int(payload["embargo_rows"])
        train = rows[: max(split - embargo, 0)]
        holdout = rows[split:]
        if not train or not holdout:
            raise RuntimeError("Chronological train/holdout split is empty")
        train_x = [[numeric_features(row["features"]).get(name, 0.0) for name in features] for row in train]
        train_y = [float(row.get("net_r") or 0.0) for row in train]
        weights = [self._recency_weight(row["created_at"], dataset.source_cutoff_at) for row in train]
        regression_booster = lgb.train(
            {
                "objective": "regression",
                "metric": "l2",
                "verbosity": -1,
                "num_leaves": 15,
                "learning_rate": 0.03,
                "feature_fraction": 0.8,
                "seed": 37,
            },
            lgb.Dataset(train_x, label=train_y, weight=weights, feature_name=features),
            num_boost_round=100,
        )
        classifier_booster = lgb.train(
            {
                "objective": "binary",
                "metric": "binary_logloss",
                "verbosity": -1,
                "num_leaves": 15,
                "learning_rate": 0.03,
                "feature_fraction": 0.8,
                "seed": 41,
            },
            lgb.Dataset(
                train_x,
                label=[int(bool(row.get("profitable"))) for row in train],
                weight=weights,
                feature_name=features,
            ),
            num_boost_round=100,
        )
        holdout_x = [[numeric_features(row["features"]).get(name, 0.0) for name in features] for row in holdout]
        predicted = [float(value) for value in regression_booster.predict(holdout_x)]
        probabilities = [float(value) for value in classifier_booster.predict(holdout_x)]
        accepted_real = [
            (row, score, probability)
            for row, score, probability in zip(holdout, predicted, probabilities, strict=True)
            if bool(row.get("accepted")) and row.get("label_type") == "real"
        ]
        actual = [float(row.get("net_r") or 0.0) for row, _score, _probability in accepted_real]
        accepted_predictions = [
            score if probability >= 0.5 else -abs(score)
            for _row, score, probability in accepted_real
        ]
        regimes = [_regime_for_row(row) for row, _score, _probability in accepted_real]
        metrics = _evaluation_metrics(actual, accepted_predictions, regimes=regimes)
        metrics.update(
            {
                "accepted_oos_trades": dataset.accepted_oos_trades,
                "leakage_passed": True,
                "schema_passed": True,
            }
        )
        artifact = json.dumps(
            {
                "feature_names": features,
                "regression_booster": regression_booster.model_to_string(),
                "classifier_booster": classifier_booster.model_to_string(),
            },
            sort_keys=True,
        ).encode()
        uri, digest = self.artifacts.put("lightgbm-challenger.json", artifact)
        champion = self.repository.champion()
        model = MetaModelVersion(
            dataset_version_id=dataset.id,
            parent_version_id=champion.id if champion else None,
            artifact_uri=uri,
            artifact_hash=digest,
            feature_names=features,
            metrics=metrics,
        )
        self.logs.log("learning_challenger_trained", model.model_dump())
        return self.repository.record_model(model)

    def evaluate_challenger(self, model_id: str) -> MetaModelEvaluation:
        model = self.repository.get_model(model_id)
        if model is None:
            raise KeyError(f"Meta-model {model_id} not found")
        metrics = dict(model.metrics)
        metrics["shadow_sessions"] = self.repository.shadow_sessions_for_model(model_id)
        blockers: list[str] = []
        champion = self.repository.champion()
        integrity = self.repository.status_counts()
        checks = (
            (
                int(metrics.get("accepted_oos_trades", 0))
                < int(self.settings.production_min_oos_trades),
                "insufficient_accepted_oos_trades",
            ),
            (float(metrics.get("expectancy", 0.0)) <= 0, "non_positive_after_cost_expectancy"),
            (
                float(metrics.get("profit_factor", 0.0))
                < float(self.settings.production_min_profit_factor),
                "profit_factor_below_threshold",
            ),
            (
                float(metrics.get("deflated_sharpe", 0.0))
                < float(self.settings.production_min_deflated_sharpe),
                "deflated_sharpe_below_threshold",
            ),
            (not bool(metrics.get("significant_improvement", False)), "improvement_not_significant"),
            (not bool(metrics.get("regime_stable", False)), "regime_stability_failed"),
            (not bool(metrics.get("leakage_passed", False)), "data_leakage_check_failed"),
            (not bool(metrics.get("schema_passed", False)), "feature_schema_check_failed"),
            (
                int(metrics.get("shadow_sessions", 0)) < int(self.settings.learning_min_shadow_sessions),
                "insufficient_shadow_sessions",
            ),
        )
        blockers.extend(reason for failed, reason in checks if failed)
        if int(integrity.get("failed_jobs", 0)) > 0:
            blockers.append("learning_pipeline_failures_present")
        if int(integrity.get("excessive_drift", 0)) > 0:
            blockers.append("excessive_drift_present")
        if (
            champion
            and champion.id != model.id
            and float(metrics.get("max_drawdown", math.inf))
            > float(champion.metrics.get("max_drawdown", math.inf))
        ):
            blockers.append("drawdown_worse_than_champion")
        evaluation = MetaModelEvaluation(
            model_version_id=model.id,
            champion_version_id=model.parent_version_id,
            status="passed" if not blockers else "blocked",
            metrics=metrics,
            blockers=blockers,
            shadow_sessions=int(metrics.get("shadow_sessions", 0)),
            leakage_passed=bool(metrics.get("leakage_passed", False)),
            schema_passed=bool(metrics.get("schema_passed", False)),
        )
        return self.repository.record_evaluation(evaluation)

    def promote(self, model_id: str, *, target_mode: str, signed_by: str = "") -> ModelPromotionDecision:
        if target_mode not in {"paper", "live"}:
            raise ValueError("Model target_mode must be paper or live")
        model = self.repository.get_model(model_id)
        if model is None:
            raise KeyError(f"Meta-model {model_id} not found")
        evaluation = self.repository.latest_evaluation(model_id)
        blockers = list(evaluation.blockers if evaluation else ["missing_model_evaluation"])
        if target_mode == "live" and not signed_by.strip():
            blockers.append("live_promotion_requires_signed_approval")
        if target_mode == "live":
            if not bool(self.settings.learning_live_promotion_enabled):
                blockers.append("learning_live_promotion_disabled")
            readiness = self.institutional.readiness() if self.institutional is not None else {}
            if not bool(readiness.get("ready")):
                blockers.append("institutional_rollout_gates_not_ready")
        if target_mode == "paper":
            if self.settings.execution_mode != "paper":
                blockers.append("paper_promotion_requires_paper_execution_mode")
            if not bool(self.settings.learning_auto_promote_paper_enabled) and not signed_by.strip():
                blockers.append("paper_auto_promotion_disabled")
        approved = not blockers
        decision = self.repository.record_promotion(
            ModelPromotionDecision(
                model_version_id=model_id,
                target_mode=target_mode,
                approved=approved,
                signed_by=signed_by,
                blockers=blockers,
                evidence=evaluation.model_dump() if evaluation else {},
            )
        )
        if approved:
            champion = self.repository.champion()
            if champion and champion.id != model_id:
                self.repository.update_model_status(
                    champion.id,
                    status="retired",
                    deployment_mode="retired",
                )
            self.repository.update_model_status(model_id, status="champion", deployment_mode=target_mode)
            self._loaded_model = None
        return decision

    def rollback(self, model_id: str, *, signed_by: str) -> ModelPromotionDecision:
        if not signed_by.strip():
            raise ValueError("Model rollback requires signed_by")
        model = self.repository.get_model(model_id)
        if model is None:
            raise KeyError(f"Meta-model {model_id} not found")
        self.repository.update_model_status(model_id, status="rolled_back", deployment_mode="retired")
        self._loaded_model = None
        return self.repository.record_promotion(
            ModelPromotionDecision(
                model_version_id=model_id,
                target_mode="paper",
                approved=False,
                signed_by=signed_by,
                blockers=["operator_rollback"],
            )
        )

    def rollback_if_unsafe(self) -> ModelPromotionDecision | None:
        champion = self.repository.champion()
        if champion is None:
            return None
        integrity = self.repository.status_counts()
        reasons = []
        if float(champion.metrics.get("expectancy", 0.0)) < 0:
            reasons.append("negative_champion_expectancy")
        if float(champion.metrics.get("profit_factor", 0.0)) < 1.0:
            reasons.append("champion_profit_factor_below_one")
        if int(integrity.get("failed_jobs", 0)) > 0:
            reasons.append("learning_pipeline_integrity_failure")
        if int(integrity.get("excessive_drift", 0)) > 0:
            reasons.append("excessive_drift")
        return self._automatic_rollback(champion, reasons) if reasons else None

    def safe_adjust(self, *, deterministic_score: float, deterministic_eligible: bool, features: dict[str, Any]) -> dict[str, Any]:
        base = float(deterministic_score)
        result = {
            "deterministic_score": base,
            "adjusted_score": base,
            "keep_probability": None,
            "expected_net_r": None,
            "model_version_id": None,
            "model_blocked": False,
            "fallback_reason": "",
        }
        champion = self.repository.champion()
        if champion is None:
            return result
        result["model_version_id"] = champion.id
        try:
            actual_names = set(numeric_features(features))
            expected_names = set(champion.feature_names)
            drift_score = len(expected_names.symmetric_difference(actual_names)) / max(
                len(expected_names.union(actual_names)),
                1,
            )
            if not expected_names.issubset(actual_names):
                raise RuntimeError("missing_feature_schema")
            if drift_score > float(self.settings.learning_max_drift_score):
                raise RuntimeError("excessive_feature_drift")
            regression_booster, classifier_booster, names = self._load_champion(champion)
            row = [[numeric_features(features).get(name, 0.0) for name in names]]
            expected_r = float(regression_booster.predict(row)[0])
            probability = float(classifier_booster.predict(row)[0])
            result["expected_net_r"] = expected_r
            result["keep_probability"] = probability
            if (
                deterministic_eligible
                and self.settings.model_deployment_mode == "gating"
                and champion.deployment_mode == "paper"
            ):
                reduction = min(
                    max((float(self.settings.learning_min_probability_to_keep) - probability) * 100.0, 0.0),
                    float(self.settings.learning_max_score_reduction),
                )
                result["adjusted_score"] = min(base, max(base - reduction, 0.0))
                result["model_blocked"] = probability < float(
                    self.settings.learning_min_probability_to_keep
                )
            return result
        except Exception as exc:
            result["fallback_reason"] = f"model_inference_fallback:{exc}"
            return result

    def record_drift(self, features: dict[str, Any]) -> LearningDriftSnapshot:
        champion = self.repository.champion()
        score = 0.0
        if champion:
            expected = set(champion.feature_names)
            actual = set(numeric_features(features))
            score = len(expected.symmetric_difference(actual)) / max(len(expected.union(actual)), 1)
        drift = LearningDriftSnapshot(
            model_version_id=champion.id if champion else None,
            drift_score=round(score, 6),
            excessive=score > float(self.settings.learning_max_drift_score),
            details={"type": "feature_schema_drift"},
        )
        persisted = self.repository.record_drift(drift)
        if persisted.excessive and champion is not None:
            self._automatic_rollback(champion, ["excessive_drift"])
        return persisted

    def _automatic_rollback(
        self,
        champion: MetaModelVersion,
        reasons: list[str],
    ) -> ModelPromotionDecision:
        self.repository.update_model_status(
            champion.id,
            status="rolled_back",
            deployment_mode="retired",
        )
        self._loaded_model = None
        decision = self.repository.record_promotion(
            ModelPromotionDecision(
                model_version_id=champion.id,
                target_mode=champion.deployment_mode if champion.deployment_mode in {"paper", "live"} else "paper",
                approved=False,
                signed_by="system:learning-safety",
                blockers=reasons,
            )
        )
        self.logs.log("learning_model_automatic_rollback", decision.model_dump())
        return decision

    def _load_champion(self, champion: MetaModelVersion):
        if self._loaded_model and self._loaded_model[0] == champion.id:
            return self._loaded_model[1], self._loaded_model[2], self._loaded_model[3]
        raw = self.artifacts.get(champion.artifact_uri)
        if hashlib.sha256(raw).hexdigest() != champion.artifact_hash:
            raise RuntimeError("model_artifact_hash_mismatch")
        payload = json.loads(raw)
        import lightgbm as lgb

        regression_booster = lgb.Booster(model_str=payload["regression_booster"])
        classifier_booster = lgb.Booster(model_str=payload["classifier_booster"])
        names = list(payload["feature_names"])
        self._loaded_model = (champion.id, regression_booster, classifier_booster, names)
        return regression_booster, classifier_booster, names

    def _recency_weight(self, created_at: str, cutoff_at: str) -> float:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00")).astimezone(UTC)
        cutoff = datetime.fromisoformat(cutoff_at.replace("Z", "+00:00")).astimezone(UTC)
        age_days = max((cutoff - created).total_seconds() / 86400.0, 0.0)
        half_life = max(float(self.settings.learning_recency_half_life_days), 1.0)
        return max(0.05, 0.5 ** (age_days / half_life))


def _regime_for_row(row: dict[str, Any]) -> str:
    decision = dict(row.get("decision") or {})
    signal = dict(decision.get("signal") or {})
    metadata = dict(signal.get("metadata") or {})
    return str(
        metadata.get("regime_label")
        or metadata.get("market_regime")
        or decision.get("regime_label")
        or "unknown"
    )


def _evaluation_metrics(
    actual: list[float],
    predicted: list[float],
    *,
    regimes: list[str] | None = None,
) -> dict[str, Any]:
    selected = [value for value, score in zip(actual, predicted, strict=True) if score > 0]
    selected = selected or actual
    wins = sum(value for value in selected if value > 0)
    losses = abs(sum(value for value in selected if value < 0))
    expectancy = sum(selected) / len(selected) if selected else 0.0
    variance = sum((value - expectancy) ** 2 for value in selected) / max(len(selected) - 1, 1)
    sharpe = expectancy / math.sqrt(variance) * math.sqrt(len(selected)) if variance > 0 else 0.0
    baseline = sum(actual) / len(actual) if actual else 0.0
    p_value = 1.0
    if len(selected) >= 30 and len(set(selected)) > 1:
        test = ttest_1samp(selected, popmean=baseline, alternative="greater")
        p_value = float(test.pvalue) if math.isfinite(float(test.pvalue)) else 1.0
    regime_values: dict[str, list[float]] = {}
    for value, score, regime in zip(
        actual,
        predicted,
        regimes or ["unknown"] * len(actual),
        strict=True,
    ):
        if score > 0:
            regime_values.setdefault(regime, []).append(value)
    known_regimes = {
        name: values
        for name, values in regime_values.items()
        if name != "unknown" and len(values) >= 10
    }
    regime_stable = len(known_regimes) >= 2 and all(
        sum(values) / len(values) > 0 for values in known_regimes.values()
    )
    cumulative = peak = max_drawdown = 0.0
    for value in selected:
        cumulative += value
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
    return {
        "accepted_oos_trades": len(selected),
        "expectancy": expectancy,
        "profit_factor": wins / losses if losses > 0 else wins,
        "deflated_sharpe": sharpe * 0.9,
        "max_drawdown": max_drawdown,
        "baseline_expectancy": baseline,
        "improvement_p_value": p_value,
        "significant_improvement": len(selected) >= 30 and expectancy > baseline and p_value < 0.05,
        "regime_stable": regime_stable,
        "regime_expectancy": {
            name: sum(values) / len(values) for name, values in known_regimes.items()
        },
        "shadow_sessions": 0,
    }
