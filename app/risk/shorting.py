"""Explicit short-sale borrow, margin, and capital controls."""

from __future__ import annotations

from typing import Any

from app.models.trade import OrderSide, TradeOrder


class ShortTradePolicy:
    """Reject short entries until every deferred short-sale control passes."""

    def __init__(self, settings: Any):
        self.settings = settings

    def blockers(self, order: TradeOrder, *, account_equity_usd: float) -> list[str]:
        if order.side != OrderSide.SELL or not bool(order.metadata.get("opens_short")):
            return []
        blockers: list[str] = []
        if not self.settings.short_trading_enabled:
            blockers.append("short_trading_disabled")
        if account_equity_usd < self.settings.short_minimum_account_equity_usd:
            blockers.append("short_minimum_account_equity_not_met")
        if self.settings.short_require_easy_to_borrow and not bool(
            order.metadata.get("easy_to_borrow")
        ):
            blockers.append("short_not_easy_to_borrow")
        borrow_cost = order.metadata.get("borrow_cost_annual_pct")
        if borrow_cost is None:
            blockers.append("short_borrow_cost_missing")
        elif float(borrow_cost) > self.settings.short_max_borrow_cost_annual_pct:
            blockers.append("short_borrow_cost_above_limit")
        if self.settings.short_require_margin_requirement and order.metadata.get(
            "margin_requirement_pct"
        ) is None:
            blockers.append("short_margin_requirement_missing")
        return blockers
