"""Automation control models."""

from __future__ import annotations

from pydantic import BaseModel


class AutomationStatus(BaseModel):
    """Current bot automation state."""

    paused: bool
    kill_switch_enabled: bool
    auto_propose_enabled: bool
    auto_execute_after_approval: bool
    paper_auto_approve_proposals: bool = False
    auto_execution_worker_enabled: bool = False
    execution_mode: str
    require_approval: bool
    enable_real_trading: bool
    reason: str = ""
    updated_at: str | None = None
    circuit_breaker_reason: str = ""
    account_verified: bool | None = None


class AutomationStateChange(BaseModel):
    """Reason attached to a manual automation state change."""

    reason: str = ""
