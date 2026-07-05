"""Paper trading models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.utils.ids import generate_id
from app.utils.time import utc_now


class PaperPositionRecord(BaseModel):
    """Open paper position."""

    id: str = Field(default_factory=lambda: generate_id("paperpos"))
    proposal_id: str | None = None
    signal_id: str | None = None
    symbol: str
    strategy_name: str
    timeframe: str
    side: str
    regime_label: str | None = None
    hold_style: str | None = None
    status: str = "open"
    quantity: float = 0.0
    entry_price: float = 0.0
    current_price: float = 0.0
    stop_loss: float | None = None
    target_1: float | None = None
    target_2: float | None = None
    target_3: float | None = None
    opened_at: str = Field(default_factory=lambda: utc_now().isoformat())
    updated_at: str = Field(default_factory=lambda: utc_now().isoformat())
    closed_at: str | None = None
    realized_pnl_usd: float = 0.0
    unrealized_pnl_usd: float = 0.0
    payload: dict[str, Any] = Field(default_factory=dict)


class PaperTradeRecord(BaseModel):
    """Closed paper trade record."""

    id: str = Field(default_factory=lambda: generate_id("papertrade"))
    position_id: str
    proposal_id: str | None = None
    signal_id: str | None = None
    symbol: str
    strategy_name: str
    timeframe: str
    side: str
    regime_label: str | None = None
    hold_style: str | None = None
    outcome: str
    entry_price: float
    exit_price: float
    quantity: float
    realized_pnl_usd: float
    realized_pnl_pct: float
    opened_at: str
    closed_at: str = Field(default_factory=lambda: utc_now().isoformat())
    payload: dict[str, Any] = Field(default_factory=dict)


class PaperBrokerOrderLeg(BaseModel):
    """Broker order or protective leg snapshot for Alpaca Paper execution visibility."""

    broker_order_id: str | None = None
    client_order_id: str | None = None
    side: str | None = None
    order_type: str | None = None
    status: str | None = None
    quantity: float = 0.0
    filled_qty: float = 0.0
    filled_avg_price: float | None = None
    limit_price: float | None = None
    stop_price: float | None = None
    created_at: str | None = None
    filled_at: str | None = None
    canceled_at: str | None = None


class PaperBrokerExecutionRecord(BaseModel):
    """Real broker-side Alpaca Paper execution lifecycle."""

    execution_id: str
    proposal_id: str
    queue_id: str | None = None
    symbol: str
    strategy_name: str | None = None
    source: str = "unknown"
    mode: str
    status: str
    broker_order_id: str | None = None
    client_order_id: str | None = None
    side: str | None = None
    order_class: str | None = None
    quantity: float = 0.0
    filled_qty: float = 0.0
    entry_fill_price: float | None = None
    exit_order_id: str | None = None
    exit_fill_price: float | None = None
    realized_pnl_usd: float = 0.0
    created_at: str
    updated_at: str
    submitted_at: str | None = None
    filled_at: str | None = None
    canceled_at: str | None = None
    legs: list[PaperBrokerOrderLeg] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)


class PaperLifecycleFlags(BaseModel):
    """Completeness flags for one broker-backed paper trade lifecycle."""

    entry_submitted: bool = False
    entry_filled: bool = False
    bracket_legs_verified: bool = False
    exit_filled_or_position_flat: bool = False
    reconciled: bool = False
    review_created: bool = False
    duplicate_order_absent: bool = True


class PaperTradeLifecycleRecord(BaseModel):
    """End-to-end evidence view for one paper execution lifecycle."""

    id: str
    execution_id: str
    proposal_id: str
    queue_id: str | None = None
    symbol: str
    strategy_name: str | None = None
    source: str = "unknown"
    autonomous: bool = False
    status: str
    broker_order_id: str | None = None
    client_order_id: str | None = None
    entry_fill_price: float | None = None
    exit_fill_price: float | None = None
    realized_pnl_usd: float = 0.0
    created_at: str
    updated_at: str
    flags: PaperLifecycleFlags = Field(default_factory=PaperLifecycleFlags)
    blockers: list[str] = Field(default_factory=list)
    execution: PaperBrokerExecutionRecord


class PaperPerformanceSummary(BaseModel):
    """Aggregated paper trading performance view."""

    mode: str = "paper"
    total_trades: int = 0
    open_positions: int = 0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    realized_pnl_usd: float = 0.0
    unrealized_pnl_usd: float = 0.0
    expectancy_usd: float = 0.0
    average_reward_to_risk: float = 0.0
    average_r_multiple: float = 0.0
    max_drawdown_usd: float = 0.0
    watchlist_signals: int = 0
    trigger_ready_signals: int = 0
    execution_ready_signals: int = 0
    pnl_by_timeframe: dict[str, float] = Field(default_factory=dict)
    pnl_by_strategy: dict[str, float] = Field(default_factory=dict)
    pnl_by_symbol: dict[str, float] = Field(default_factory=dict)
    pnl_by_regime: dict[str, float] = Field(default_factory=dict)
    rejection_reason_counts: dict[str, int] = Field(default_factory=dict)


class BotPerformanceDashboard(BaseModel):
    """Operator dashboard for paper trading, scan quality, and risk controls."""

    paper: PaperPerformanceSummary
    open_positions: list[PaperPositionRecord] = Field(default_factory=list)
    recent_trades: list[PaperTradeRecord] = Field(default_factory=list)
    recent_broker_executions: list[PaperBrokerExecutionRecord] = Field(default_factory=list)
    recent_scan_decisions: list[dict[str, Any]] = Field(default_factory=list)
    provider_health: dict[str, Any] = Field(default_factory=dict)
    calibration_suggestions: list[str] = Field(default_factory=list)
    risk_controls: dict[str, Any] = Field(default_factory=dict)
