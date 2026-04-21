"""Models for market universe scans and batch backtests."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.live_signal_schema import LiveSignalSnapshot


class MarketUniverseResponse(BaseModel):
    """Active universe metadata."""

    universe_name: str
    symbols: list[str] = Field(default_factory=list)
    count: int = 0


class ScreenerRunResponse(BaseModel):
    """Top ranked screener result set."""

    generated_at: str
    universe_name: str
    timeframes: list[str]
    evaluated_symbols: int
    evaluated_strategy_runs: int
    candidates: list[LiveSignalSnapshot] = Field(default_factory=list)
    suppressed: int = 0
    alerts_sent: int = 0
    errors: list[str] = Field(default_factory=list)
    rejection_summary: dict[str, int] = Field(default_factory=dict)
    closest_rejections: list[dict[str, Any]] = Field(default_factory=list)


class ScanDecisionRecord(BaseModel):
    """Persisted filter and ranking outcome for a symbol/strategy/timeframe evaluation."""

    id: int
    scan_task: str
    symbol: str
    strategy_name: str
    timeframe: str
    status: str
    final_score: float | None = None
    alert_eligible: bool = False
    freshness: str | None = None
    reason_codes: list[str] = Field(default_factory=list)
    rejection_reasons: list[str] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class BatchBacktestSummary(BaseModel):
    """Aggregate result for a multi-symbol backtest batch."""

    generated_at: str
    symbols_evaluated: int
    strategy_runs: int
    timeframe: str
    provider: str
    results: list[dict] = Field(default_factory=list)
    aggregate_metrics: dict[str, float] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
