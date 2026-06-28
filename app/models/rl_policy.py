"""Paper-only reinforcement-learning policy models."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.utils.ids import generate_id
from app.utils.time import utc_now


class RLPolicyVersion(BaseModel):
    """Offline contextual-bandit policy evidence and gates."""

    id: str = Field(default_factory=lambda: generate_id("rlpol"))
    status: Literal["shadow", "paper_candidate", "blocked", "retired"] = "shadow"
    dataset_version: str
    reward_model_version: str = "offline_contextual_bandit_v1"
    row_count: int = Field(default=0, ge=0)
    accepted_rows: int = Field(default=0, ge=0)
    metrics: dict[str, Any] = Field(default_factory=dict)
    policy: dict[str, Any] = Field(default_factory=dict)
    blockers: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())


class RLPolicyProposal(BaseModel):
    """Auditable RL policy proposal or rejection attempt."""

    id: str = Field(default_factory=lambda: generate_id("rlprop"))
    decision_key: str
    policy_version_id: str | None = None
    scan_decision_id: int | None = None
    proposal_id: str | None = None
    symbol: str
    strategy_name: str
    timeframe: str = "1d"
    status: Literal["proposed", "rejected", "blocked", "queued"] = "blocked"
    score: float = 0.0
    blockers: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())
