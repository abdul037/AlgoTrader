"""Pure risk rules."""

from __future__ import annotations

from pydantic import BaseModel, Field


class RiskValidationResult(BaseModel):
    """Risk validation outcome."""

    passed: bool
    reasons: list[str] = Field(default_factory=list)
    risk_amount_usd: float = 0.0
    risk_pct_of_balance: float = 0.0
    room_authority: str = "paper"  # "etoro_live" when eToro's own limits decided room


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


def effective_max_risk_per_trade_pct(settings: object, *, live: bool = False) -> float:
    """Per-trade risk cap the hard gate enforces; sizers must size to the same number.

    Mirrors the cap in ``RiskGuardrails``: with institutional portfolio controls
    on, it tightens to the future-paper (or micro-live) limit. Sizing to
    ``max_risk_per_trade_pct`` alone produced proposals the gate then rejected
    ("Estimated trade risk 0.53-1.00% exceeds the 0.50% cap").
    """

    cap = float(getattr(settings, "max_risk_per_trade_pct", 1.0) or 1.0)
    if bool(getattr(settings, "institutional_portfolio_controls_enabled", False)):
        name = "portfolio_micro_live_max_risk_per_trade_pct" if live else "portfolio_future_max_risk_per_trade_pct"
        tighter = getattr(settings, name, None)
        if tighter is not None:
            cap = min(cap, float(tighter))
    return cap


# Size a hair under the gate: rounding the notional half-up, or equity drifting
# between proposal and the execution-time re-check, otherwise lands sizes at
# 0.50000005% and the strict ``risk_pct > cap`` gate rejects about half of them.
SIZING_HEADROOM = 0.98


def sizing_risk_pct(settings: object, *, live: bool = False) -> float:
    """Risk budget sizers use: the enforced cap less a small headroom."""

    return effective_max_risk_per_trade_pct(settings, live=live) * SIZING_HEADROOM
