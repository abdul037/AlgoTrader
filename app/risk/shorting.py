"""Explicit short-sale borrow, margin, and capital controls."""

from __future__ import annotations

from typing import Any

from app.models.trade import TradeOrder
from app.risk.short_side import order_opens_short, paper_short_order


class ShortTradePolicy:
    """Reject short entries until every deferred short-sale control passes.

    Shorts step 3 (operator 2026-11-16, docs/notes/paper_shorts.md): every order this gate sees
    is an entry, so every SELL (or an order marked short) opens a short and meets these controls
    whether or not it carries ``opens_short``. Shorting is switched on by the generic
    ``short_trading_enabled`` (still off) or, for one tagged paper-only short on the Alpaca paper
    account, by the paper switch (``paper_short_order``); every other control applies either way.
    A short outside paper mode, or with real trading on, is refused whatever the switches say:
    real-money shorts are Phase B.
    """

    def __init__(self, settings: Any):
        self.settings = settings

    def blockers(self, order: TradeOrder, *, account_equity_usd: float) -> list[str]:
        if not order_opens_short(order):
            return []
        metadata = order.metadata if isinstance(order.metadata, dict) else {}
        blockers: list[str] = []
        if not (
            bool(self.settings.short_trading_enabled) or paper_short_order(self.settings, order)
        ):
            blockers.append("short_trading_disabled")
        if str(getattr(self.settings, "execution_mode", "paper")) != "paper" or bool(
            getattr(self.settings, "enable_real_trading", False)
        ):
            blockers.append("short_trading_live_not_enabled")
        if account_equity_usd < self.settings.short_minimum_account_equity_usd:
            blockers.append("short_minimum_account_equity_not_met")
        if not bool(metadata.get("shortable")):
            blockers.append("short_not_shortable")
        if metadata.get("marginable") is False:
            blockers.append("short_not_marginable")
        if metadata.get("account_shorting_enabled") is False:
            blockers.append("short_account_shorting_disabled")
        if self.settings.short_require_easy_to_borrow and not bool(metadata.get("easy_to_borrow")):
            blockers.append("short_not_easy_to_borrow")
        borrow_cost = metadata.get("borrow_cost_annual_pct")
        if borrow_cost is None:
            blockers.append("short_borrow_cost_missing")
        elif float(borrow_cost) > self.settings.short_max_borrow_cost_annual_pct:
            blockers.append("short_borrow_cost_above_limit")
        if self.settings.short_require_margin_requirement and metadata.get(
            "margin_requirement_pct"
        ) is None:
            blockers.append("short_margin_requirement_missing")
        return blockers
