from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.learning.artifacts import LocalArtifactStore
from app.learning.modeling import LearningModelService
from app.learning.repository import LearningRepository
from app.models.learning import MetaModelEvaluation, MetaModelVersion
from app.storage.db import Database
from app.storage.repositories import RunLogRepository
from tests.conftest import make_settings


class FakeBooster:
    def __init__(self, value: float):
        self.value = value

    def predict(self, rows):
        return [self.value for _row in rows]


class FakeRepository:
    def __init__(self, champion):
        self._champion = champion

    def champion(self):
        return self._champion


class FakeModelService(LearningModelService):
    def __init__(self, *, booster, **kwargs):
        super().__init__(**kwargs)
        self.booster = booster

    def _load_champion(self, champion):
        return self.booster, self.booster, champion.feature_names


def _champion() -> MetaModelVersion:
    return MetaModelVersion(
        dataset_version_id="dataset",
        status="champion",
        deployment_mode="paper",
        feature_names=["score"],
        artifact_uri="unused",
        artifact_hash="unused",
    )


def test_meta_model_can_only_reduce_eligible_deterministic_score(tmp_path) -> None:
    settings = make_settings(
        tmp_path,
        model_deployment_mode="gating",
        learning_min_probability_to_keep=0.8,
        learning_max_drift_score=1.0,
    )
    service = FakeModelService(
        settings=settings,
        repository=FakeRepository(_champion()),
        artifact_store=LocalArtifactStore(str(tmp_path)),
        run_logs=SimpleNamespace(log=lambda *_args: None),
        booster=FakeBooster(-3.0),
    )

    eligible = service.safe_adjust(
        deterministic_score=80,
        deterministic_eligible=True,
        features={"score": 80},
    )
    ineligible = service.safe_adjust(
        deterministic_score=80,
        deterministic_eligible=False,
        features={"score": 80},
    )

    assert 0 <= eligible["adjusted_score"] <= 80
    assert eligible["model_blocked"] is True
    assert ineligible["adjusted_score"] == 80
    assert ineligible["model_blocked"] is False


def test_missing_feature_schema_falls_back_to_deterministic_score(tmp_path) -> None:
    settings = make_settings(tmp_path, model_deployment_mode="gating")
    service = FakeModelService(
        settings=settings,
        repository=FakeRepository(_champion()),
        artifact_store=LocalArtifactStore(str(tmp_path)),
        run_logs=SimpleNamespace(log=lambda *_args: None),
        booster=FakeBooster(-3.0),
    )

    result = service.safe_adjust(deterministic_score=70, deterministic_eligible=True, features={})

    assert result["adjusted_score"] == 70
    assert "missing_feature_schema" in result["fallback_reason"]


def test_corrupt_model_artifact_is_rejected_before_inference(tmp_path) -> None:
    store = LocalArtifactStore(str(tmp_path))
    uri, _digest = store.put("model.json", b"corrupt")
    service = LearningModelService(
        settings=make_settings(tmp_path),
        repository=FakeRepository(None),
        artifact_store=store,
        run_logs=SimpleNamespace(log=lambda *_args: None),
    )
    model = _champion().model_copy(update={"artifact_uri": uri, "artifact_hash": "wrong"})

    with pytest.raises(RuntimeError, match="model_artifact_hash_mismatch"):
        service._load_champion(model)


def test_live_model_promotion_requires_signed_decision(tmp_path) -> None:
    settings = make_settings(tmp_path, learning_live_promotion_enabled=True)
    db = Database(settings)
    db.initialize()
    repository = LearningRepository(db)
    model = repository.record_model(
        MetaModelVersion(
            dataset_version_id="dataset",
            artifact_uri="artifact",
            artifact_hash="hash",
        )
    )
    repository.record_evaluation(
        MetaModelEvaluation(
            model_version_id=model.id,
            status="passed",
            blockers=[],
            leakage_passed=True,
            schema_passed=True,
        )
    )
    service = LearningModelService(
        settings=settings,
        repository=repository,
        artifact_store=LocalArtifactStore(str(tmp_path)),
        run_logs=RunLogRepository(db),
        institutional_service=SimpleNamespace(readiness=lambda: {"ready": True}),
    )

    unsigned = service.promote(model.id, target_mode="live")
    signed = service.promote(model.id, target_mode="live", signed_by="operator")

    assert unsigned.approved is False
    assert "live_promotion_requires_signed_approval" in unsigned.blockers
    assert signed.approved is True


def test_live_model_promotion_requires_explicit_flag_and_rollout_readiness(tmp_path) -> None:
    settings = make_settings(tmp_path, learning_live_promotion_enabled=False)
    db = Database(settings)
    db.initialize()
    repository = LearningRepository(db)
    model = repository.record_model(
        MetaModelVersion(
            dataset_version_id="dataset",
            artifact_uri="artifact",
            artifact_hash="hash",
        )
    )
    repository.record_evaluation(
        MetaModelEvaluation(
            model_version_id=model.id,
            status="passed",
            blockers=[],
            leakage_passed=True,
            schema_passed=True,
        )
    )
    service = LearningModelService(
        settings=settings,
        repository=repository,
        artifact_store=LocalArtifactStore(str(tmp_path)),
        run_logs=RunLogRepository(db),
        institutional_service=SimpleNamespace(readiness=lambda: {"ready": False}),
    )

    decision = service.promote(model.id, target_mode="live", signed_by="operator")

    assert decision.approved is False
    assert "learning_live_promotion_disabled" in decision.blockers
    assert "institutional_rollout_gates_not_ready" in decision.blockers


def test_excessive_drift_automatically_retires_paper_champion(tmp_path) -> None:
    settings = make_settings(tmp_path, learning_max_drift_score=0.0)
    db = Database(settings)
    db.initialize()
    repository = LearningRepository(db)
    model = repository.record_model(
        MetaModelVersion(
            dataset_version_id="dataset",
            status="champion",
            deployment_mode="paper",
            feature_names=["score"],
            artifact_uri="artifact",
            artifact_hash="hash",
            metrics={"expectancy": 1.0, "profit_factor": 1.5},
        )
    )
    service = LearningModelService(
        settings=settings,
        repository=repository,
        artifact_store=LocalArtifactStore(str(tmp_path)),
        run_logs=RunLogRepository(db),
    )

    drift = service.record_drift({"different_feature": 1.0})

    assert drift.excessive is True
    assert repository.champion() is None
    assert repository.get_model(model.id).status == "rolled_back"
