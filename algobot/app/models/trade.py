"""Trade models."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, PositiveFloat


class OrderSide(str, Enum):
    """Supported order directions."""

    BUY = "buy"
    SELL = "sell"


class AssetClass(str, Enum):
    """Supported asset classes."""

    EQUITY = "equity"
    GOLD = "gold"
    UNKNOWN = "unknown"


class TradeOrder(BaseModel):
    """A normalized trade order request."""

    symbol: str
    side: OrderSide = OrderSide.BUY
    amount_usd: PositiveFloat
    leverage: int = Field(default=1, ge=1)
    proposed_price: PositiveFloat
    stop_loss: PositiveFloat | None = None
    take_profit: PositiveFloat | None = None
    asset_class: AssetClass = AssetClass.UNKNOWN
    broker_symbol: str | None = None
    strategy_name: str | None = None
    rationale: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
