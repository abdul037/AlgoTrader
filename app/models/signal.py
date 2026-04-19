"""Signal models."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from app.utils.ids import generate_id
from app.utils.time import utc_now


class SignalAction(str, Enum):
    """Signal actions."""

    BUY = "buy"
    SELL = "sell"


class Signal(BaseModel):
    """A strategy-generated trading signal."""

    id: str = Field(default_factory=lambda: generate_id("sig"))
    symbol: str
    strategy_name: str
    action: SignalAction
    rationale: str
    timestamp: str = Field(default_factory=lambda: utc_now().isoformat())
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    price: float | None = Field(default=None, gt=0.0)
    stop_loss: float | None = Field(default=None, gt=0.0)
    take_profit: float | None = Field(default=None, gt=0.0)
    metadata: dict[str, Any] = Field(default_factory=dict)
