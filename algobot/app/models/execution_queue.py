"""Execution queue models for approval-gated semi-automation."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.utils.ids import generate_id
from app.utils.time import utc_now


class ExecutionQueueStatus(str):
    """Execution queue states."""

    QUEUED = "queued"
    PROCESSING = "processing"
    EXECUTED = "executed"
    CANCELLED = "cancelled"
    BLOCKED = "blocked"
    FAILED = "failed"


class ExecutionQueueRecord(BaseModel):
    """Persisted execution queue item."""

    id: str = Field(default_factory=lambda: generate_id("queue"))
    proposal_id: str
    signal_id: str | None = None
    symbol: str
    strategy_name: str | None = None
    timeframe: str | None = None
    mode: str = "paper"
    status: str = ExecutionQueueStatus.QUEUED
    approval_required: bool = True
    ready_for_execution: bool = False
    requested_entry_price: float | None = None
    latest_quote_price: float | None = None
    latest_quote_timestamp: str | None = None
    validation_reason: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())
    updated_at: str = Field(default_factory=lambda: utc_now().isoformat())
    executed_at: str | None = None
