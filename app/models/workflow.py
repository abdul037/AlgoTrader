"""Models for scheduled scans, tracked signals, and alert history."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.live_signal_schema import LiveSignalSnapshot


class TrackedSignalRecord(BaseModel):
    """Persisted tracked signal opened from a screener alert."""

    id: int
    symbol: str
    strategy_name: str
    timeframe: str
    status: str
    origin: str | None = None
    opened_at: str
    updated_at: str
    closed_at: str | None = None
    entry_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    last_price: float | None = None
    snapshot: LiveSignalSnapshot


class AlertHistoryRecord(BaseModel):
    """Persisted Telegram or workflow alert event."""

    id: int
    category: str
    symbol: str | None = None
    strategy_name: str | None = None
    timeframe: str | None = None
    status: str
    message_text: str
    payload: dict = Field(default_factory=dict)
    created_at: str


class WorkflowTaskResponse(BaseModel):
    """Response for manual workflow task execution."""

    task: str
    status: str
    detail: str
    skipped: bool = False
    alerts_sent: int = 0
    candidates: int = 0
    open_signals: int = 0
    closed_signals: int = 0
    errors: list[str] = Field(default_factory=list)


class WorkflowBucketStatus(BaseModel):
    """Operator-facing status for a named scheduler bucket."""

    name: str
    enabled: bool
    paused: bool = False
    last_run_at: str | None = None
    last_success_at: str | None = None
    next_due_at: str | None = None
    last_status: str | None = None
    last_error: str | None = None


class WorkflowStatusResponse(BaseModel):
    """Current workflow scheduler state."""

    scheduler_enabled: bool
    schedule_timezone: str = "UTC"
    last_premarket_scan_at: str | None = None
    last_market_open_scan_at: str | None = None
    last_intelligent_scan_at: str | None = None
    last_swing_scan_at: str | None = None
    last_intraday_scan_at: str | None = None
    last_end_of_day_scan_at: str | None = None
    last_open_signal_check_at: str | None = None
    last_ledger_cycle_at: str | None = None
    last_daily_summary_at: str | None = None
    open_signals: int = 0
    alert_history_count: int = 0
