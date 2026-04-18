"""Models for live signal evaluation and Telegram delivery."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class SignalState(str, Enum):
    """Normalized signal state."""

    BUY = "buy"
    SELL = "sell"
    NONE = "none"


class MarketQuote(BaseModel):
    """Normalized market quote from eToro market data."""

    symbol: str
    instrument_id: int | None = None
    bid: float | None = None
    ask: float | None = None
    last_execution: float | None = None
    timestamp: str | None = None
    source: str | None = None
    is_primary: bool | None = None
    used_fallback: bool = False
    from_cache: bool = False
    quote_derived_from_history: bool = False
    data_age_seconds: float | None = None


class LiveSignalSnapshot(BaseModel):
    """Current live signal state for a symbol."""

    symbol: str
    strategy_name: str
    state: SignalState
    timeframe: str = "1d"
    generated_at: str | None = None
    signal_generated_at: str | None = None
    candle_timestamp: str | None = None
    rate_timestamp: str | None = None
    current_price: float | None = None
    current_bid: float | None = None
    current_ask: float | None = None
    entry_price: float | None = None
    exit_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    targets: list[float] = Field(default_factory=list)
    risk_reward_ratio: float | None = None
    signal_role: str | None = None
    direction_label: str | None = None
    confidence_label: str | None = None
    freshness: str | None = None
    rank: int | None = None
    rationale: str = ""
    score: float = 0.0
    score_breakdown: dict[str, float] = Field(default_factory=dict)
    confidence: float | None = None
    tradable: bool = False
    execution_ready: bool = False
    supported: bool = False
    asset_class: str | None = None
    pass_reasons: list[str] = Field(default_factory=list)
    reject_reasons: list[str] = Field(default_factory=list)
    indicators: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    backtest_snapshot: dict[str, Any] = Field(default_factory=dict)


class SignalScanResponse(BaseModel):
    """Ranked market scan response."""

    generated_at: str | None = None
    timeframe: str | None = None
    scan_name: str | None = None
    evaluated_count: int
    limit: int
    alerts_sent: int = 0
    candidates: list[LiveSignalSnapshot] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class TelegramAlertResponse(BaseModel):
    """Telegram delivery response."""

    sent: bool
    detail: str
