"""Execution and portfolio models."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from app.utils.ids import generate_id
from app.utils.time import utc_now


class PortfolioPosition(BaseModel):
    """A broker portfolio position."""

    symbol: str
    position_id: int | None = None
    instrument_id: int | None = None
    is_buy: bool = True
    leverage: int = 1
    quantity: float = 0.0
    average_price: float = 0.0
    market_value: float = 0.0
    unrealized_pnl: float = 0.0


class AccountSummary(BaseModel):
    """Account balance summary."""

    cash_balance: float = 0.0
    equity: float = 0.0
    daily_pnl: float = 0.0
    currency: str = "USD"


class PortfolioSummary(BaseModel):
    """Portfolio summary returned by a broker."""

    mode: str
    account: AccountSummary
    positions: list[PortfolioPosition] = Field(default_factory=list)


class BrokerOrderResponse(BaseModel):
    """Normalized broker order response."""

    order_id: str
    status: str
    mode: str
    message: str = ""
    raw_response: dict[str, Any] = Field(default_factory=dict)


class ExecutionStatus(str):
    """Execution statuses."""

    CREATED = "created"
    VALIDATED = "validated"
    SUBMITTED = "submitted"
    FAILED = "failed"
    BLOCKED = "blocked"


class ExecutionRecord(BaseModel):
    """A persisted execution attempt."""

    id: str = Field(default_factory=lambda: generate_id("exec"))
    proposal_id: str
    status: str = ExecutionStatus.CREATED
    mode: str
    broker_order_id: str | None = None
    request_payload: dict[str, Any] = Field(default_factory=dict)
    response_payload: dict[str, Any] = Field(default_factory=dict)
    error_message: str | None = None
    realized_pnl_usd: float = 0.0
    created_at: str = Field(default_factory=lambda: utc_now().isoformat())
    updated_at: str = Field(default_factory=lambda: utc_now().isoformat())
