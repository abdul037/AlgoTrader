"""Governed continuous-learning models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.utils.ids import generate_id
from app.utils.time import utc_now


class DecisionSnapshot(BaseModel):
    id: str = Field(default_factory=lambda: generate_id("decision"))
    decision_key: str
    signal_id: str | None = None
    execution_id: str | None = None
    symbol: str
    strategy_name: str
    timeframe: str
    stage: str
    deterministic_eligible: bool = False
    accepted: bool = False
    deterministic_score: float | None = None
    adjusted_score: float | None = None
    model_version_id: str | None = None
    features: dict[str, Any] = Field(default_factory=dict)
    decision: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())


class OutcomeLabel(BaseModel):
    id: str = Field(default_factory=lambda: generate_id("label"))
    decision_snapshot_id: str
    label_type: Literal["real", "counterfactual"]
    status: str
    net_pnl_usd: float | None = None
    net_r: float | None = None
    profitable: bool | None = None
    source: str
    horizon: str = ""
    details: dict[str, Any] = Field(default_factory=dict)
    labeled_at: str = Field(default_factory=lambda: utc_now().isoformat())


class TradeLifecycleEvent(BaseModel):
    id: str = Field(default_factory=lambda: generate_id("lifecycle"))
    event_key: str
    execution_id: str | None = None
    proposal_id: str | None = None
    broker_order_id: str | None = None
    event_type: str
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: str = Field(default_factory=lambda: utc_now().isoformat())


class TradeReview(BaseModel):
    id: str = Field(default_factory=lambda: generate_id("review"))
    execution_id: str
    outcome_label_id: str | None = None
    status: str = "completed"
    reviewer: str = "deterministic"
    summary: str = ""
    findings: list[dict[str, Any]] = Field(default_factory=list)
    failure_categories: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    critic_model: str = ""
    estimated_cost_usd: float = Field(default=0.0, ge=0.0)
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())


class ExperimentProposal(BaseModel):
    id: str = Field(default_factory=lambda: generate_id("experiment"))
    trade_review_id: str | None = None
    title: str
    hypothesis: str
    scope: Literal["ranking", "rejection"] = "ranking"
    status: str = "proposed"
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())


class LearningDatasetVersion(BaseModel):
    id: str = Field(default_factory=lambda: generate_id("dataset"))
    status: str = "ready"
    row_count: int = Field(default=0, ge=0)
    accepted_oos_trades: int = Field(default=0, ge=0)
    feature_schema_hash: str
    source_cutoff_at: str
    train_start_at: str | None = None
    train_end_at: str | None = None
    holdout_start_at: str | None = None
    holdout_end_at: str | None = None
    artifact_uri: str = ""
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())


class MetaModelVersion(BaseModel):
    id: str = Field(default_factory=lambda: generate_id("metamodel"))
    dataset_version_id: str
    parent_version_id: str | None = None
    model_type: str = "lightgbm"
    status: str = "challenger"
    deployment_mode: Literal["shadow", "paper", "live", "retired"] = "shadow"
    feature_names: list[str] = Field(default_factory=list)
    artifact_uri: str
    artifact_hash: str
    metrics: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())


class MetaModelEvaluation(BaseModel):
    id: str = Field(default_factory=lambda: generate_id("modeleval"))
    model_version_id: str
    champion_version_id: str | None = None
    status: str = "blocked"
    metrics: dict[str, Any] = Field(default_factory=dict)
    blockers: list[str] = Field(default_factory=list)
    shadow_sessions: int = Field(default=0, ge=0)
    leakage_passed: bool = False
    schema_passed: bool = False
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())


class ModelPromotionDecision(BaseModel):
    id: str = Field(default_factory=lambda: generate_id("modelpromotion"))
    model_version_id: str
    target_mode: Literal["shadow", "paper", "live"] = "paper"
    approved: bool = False
    signed_by: str = ""
    blockers: list[str] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())


class LearningDriftSnapshot(BaseModel):
    id: str = Field(default_factory=lambda: generate_id("drift"))
    model_version_id: str | None = None
    drift_score: float = Field(default=0.0, ge=0.0)
    excessive: bool = False
    feature_drift: dict[str, float] = Field(default_factory=dict)
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())


class LearningJob(BaseModel):
    id: str = Field(default_factory=lambda: generate_id("learningjob"))
    idempotency_key: str
    job_type: str
    status: str = "pending"
    payload: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    error: str = ""
    attempts: int = Field(default=0, ge=0)
    scheduled_at: str = Field(default_factory=lambda: utc_now().isoformat())
    started_at: str | None = None
    completed_at: str | None = None
