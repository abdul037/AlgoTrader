"""Pure risk rules."""

from __future__ import annotations

from pydantic import BaseModel, Field


class RiskValidationResult(BaseModel):
    """Risk validation outcome."""

    passed: bool
    reasons: list[str] = Field(default_factory=list)
    risk_amount_usd: float = 0.0
    risk_pct_of_balance: float = 0.0


def leverage_cap_for_asset(
    *,
    asset_class: str,
    max_equity_leverage: int,
    max_gold_leverage: int,
) -> int:
    """Return the leverage cap by asset class."""

    if asset_class == "gold":
        return max_gold_leverage
    return max_equity_leverage


def estimate_risk_amount(entry_price: float, stop_loss: float, amount_usd: float, leverage: int) -> float:
    """Estimate per-trade dollar risk from stop distance."""

    stop_distance_pct = abs(entry_price - stop_loss) / entry_price
    notional = amount_usd * leverage
    return notional * stop_distance_pct
