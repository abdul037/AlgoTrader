"""Position sizing helpers."""

from __future__ import annotations

from pydantic import BaseModel, Field


class PositionSizingResult(BaseModel):
    """Calculated position sizing information."""

    amount_usd: float = Field(gt=0)
    quantity: float = Field(gt=0)
    risk_amount_usd: float = Field(gt=0)


def calculate_position_size(
    *,
    account_balance: float,
    risk_pct: float,
    entry_price: float,
    stop_price: float,
    leverage: int = 1,
) -> PositionSizingResult:
    """Size a position from balance, stop distance, and risk budget."""

    if account_balance <= 0:
        raise ValueError("account_balance must be positive")
    if entry_price <= 0 or stop_price <= 0:
        raise ValueError("entry_price and stop_price must be positive")
    if entry_price == stop_price:
        raise ValueError("stop_price must differ from entry_price")
    if leverage < 1:
        raise ValueError("leverage must be at least 1")

    risk_budget = account_balance * (risk_pct / 100)
    stop_distance_pct = abs(entry_price - stop_price) / entry_price
    gross_exposure = risk_budget / stop_distance_pct
    amount_usd = gross_exposure / leverage
    quantity = gross_exposure / entry_price
    return PositionSizingResult(
        amount_usd=round(amount_usd, 2),
        quantity=round(quantity, 6),
        risk_amount_usd=round(risk_budget, 2),
    )
