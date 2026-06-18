from __future__ import annotations

from app.learning.repository import LearningRepository
from app.models.learning import (
    DecisionSnapshot,
    LearningJob,
    OutcomeLabel,
    TradeLifecycleEvent,
    TradeReview,
)
from app.storage.db import Database
from tests.conftest import make_settings


def test_learning_evidence_and_jobs_are_idempotent(tmp_path) -> None:
    db = Database(make_settings(tmp_path))
    db.initialize()
    repository = LearningRepository(db)
    decision = repository.record_decision(
        DecisionSnapshot(
            decision_key="scan:1",
            symbol="NVDA",
            strategy_name="swing",
            timeframe="1d",
            stage="scan_decision",
            features={"score": 75.0},
        )
    )
    repository.record_decision(
        DecisionSnapshot(
            decision_key="scan:1",
            symbol="NVDA",
            strategy_name="swing",
            timeframe="1d",
            stage="scan_decision",
        )
    )
    repository.record_label(
        OutcomeLabel(
            decision_snapshot_id=decision.id,
            label_type="real",
            status="closed",
            source="broker_reconciliation",
            horizon="trade_lifecycle",
        )
    )
    repository.record_label(
        OutcomeLabel(
            decision_snapshot_id=decision.id,
            label_type="real",
            status="closed",
            source="broker_reconciliation",
            horizon="trade_lifecycle",
        )
    )
    repository.record_label(
        OutcomeLabel(
            decision_snapshot_id=decision.id,
            label_type="counterfactual",
            status="target_hit",
            source="market_data_replay",
            horizon="100_bars",
        )
    )
    event = TradeLifecycleEvent(event_key="execution:1:closed", event_type="closed")
    repository.record_event(event)
    repository.record_event(event)
    job = LearningJob(idempotency_key="review:1", job_type="review_trade")
    repository.enqueue_job(job)
    repository.enqueue_job(job)

    with db.connect() as connection:
        event_count = connection.execute("SELECT COUNT(*) FROM learning_lifecycle_events").fetchone()[0]

    status = repository.status_counts()
    assert status["decisions"] == 1
    assert status["labels"] == 2
    assert status["pending_jobs"] == 1
    assert event_count == 1


def test_review_retry_updates_one_persisted_review(tmp_path) -> None:
    db = Database(make_settings(tmp_path))
    db.initialize()
    repository = LearningRepository(db)

    first = repository.record_review(TradeReview(execution_id="exec_1", summary="first"))
    second = repository.record_review(TradeReview(execution_id="exec_1", summary="revised"))

    assert first.id == second.id
    assert second.summary == "revised"
    assert repository.status_counts()["reviews"] == 1
