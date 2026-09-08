"""Risk-based notional for auto-created proposals.

Until 2026-09-08 every auto-proposal requested a flat ``default_trade_amount_usd``
regardless of the stop distance, so ``max_risk_per_trade_pct`` never applied to
the unattended path: a $1,000 notional with a 1.5% stop risked ~$15 on a $100k
account, which cannot produce a measurable track record either way.

With ``auto_propose_risk_based_sizing`` on, the notional is sized so that the
distance from entry to stop equals ``max_risk_per_trade_pct`` of equity, then
capped by the per-trade notional caps (so a very tight stop can never turn into
an oversized position). Any missing/invalid input falls back to the flat
default, so this can never *block* a proposal — it only changes its size.
"""

from __future__ import annotations

from typing import Any

from app.risk.position_sizing import calculate_position_size


def risk_based_proposal_notional(
    settings: Any,
    *,
    entry_price: float | None,
    stop_price: float | None,
    equity_usd: float | None,
) -> tuple[float, dict[str, Any]]:
    """Return ``(amount_usd, details)`` for a new proposal."""

    flat = float(getattr(settings, "default_trade_amount_usd", 1000.0) or 1000.0)
    cap = min(flat, float(getattr(settings, "max_trade_amount_usd", flat) or flat))
    details: dict[str, Any] = {"sizing_mode": "flat_default", "amount_usd": flat}
    if not bool(getattr(settings, "auto_propose_risk_based_sizing", False)):
        return flat, details
    try:
        entry = float(entry_price or 0.0)
        stop = float(stop_price or 0.0)
        equity = float(equity_usd or 0.0)
    except (TypeError, ValueError):
        return flat, {**details, "fallback_reason": "unparseable_inputs"}
    if entry <= 0 or stop <= 0 or entry == stop or equity <= 0:
        return flat, {**details, "fallback_reason": "missing_entry_stop_or_equity"}
    risk_pct = float(getattr(settings, "max_risk_per_trade_pct", 1.0) or 1.0)
    sized = calculate_position_size(
        account_balance=equity,
        risk_pct=risk_pct,
        entry_price=entry,
        stop_price=stop,
        leverage=1,
    )
    amount = round(min(float(sized.amount_usd), cap), 2)
    stop_distance_pct = abs(entry - stop) / entry * 100.0
    return amount, {
        "sizing_mode": "risk_based",
        "amount_usd": amount,
        "uncapped_amount_usd": float(sized.amount_usd),
        "notional_cap_usd": cap,
        "capped": float(sized.amount_usd) > cap,
        "risk_pct": risk_pct,
        "risk_budget_usd": float(sized.risk_amount_usd),
        # Effective risk after the cap: what the trade actually risks at the stop.
        "effective_risk_usd": round(amount * stop_distance_pct / 100.0, 2),
        "stop_distance_pct": round(stop_distance_pct, 4),
        "equity_usd": equity,
    }
