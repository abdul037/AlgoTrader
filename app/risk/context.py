"""Shared risk-context builders for proposal and execution gates."""

from __future__ import annotations

from typing import Any

from app.risk.guardrails import RiskContext
from app.risk.sectors import correlation_bucket_for_symbol, sector_for_symbol
from app.utils.time import utc_now


def build_risk_context(settings: Any, broker: Any, executions_repo: Any) -> RiskContext:
    """Build the account context used by every hard risk validation gate.

    Proposal creation and queued execution both call this helper intentionally:
    proposal-time validation catches bad ideas early, while execution-time
    validation catches state changes between approval and order submission.
    """

    start_of_day = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
    trades_today = executions_repo.count_since(start_of_day)
    daily_pnl, _daily_loss_streak = executions_repo.daily_loss_stats()
    consecutive_losses = executions_repo.consecutive_losses()
    weekly_pnl = executions_repo.period_realized_pnl(days=7)

    if settings.execution_mode == "paper" and getattr(settings, "paper_broker", "") == "self_simulated":
        return RiskContext(
            account_balance=max(float(settings.paper_account_balance_usd), 1.0),
            daily_realized_pnl_usd=daily_pnl,
            weekly_realized_pnl_usd=weekly_pnl,
            open_positions=0,
            positions_by_symbol={},
            consecutive_losses_today=consecutive_losses,
            trades_today=trades_today,
            mode="paper",
        )

    portfolio = broker.get_portfolio()
    account_balance = max(portfolio.account.equity, portfolio.account.cash_balance, 1.0)
    positions_by_symbol: dict[str, int] = {}
    exposure_by_symbol_pct: dict[str, float] = {}
    exposure_by_sector_pct: dict[str, float] = {}
    exposure_by_correlation_bucket_pct: dict[str, float] = {}
    gross_market_value = 0.0
    open_unrealized_pnl = 0.0
    for position in portfolio.positions:
        symbol = str(position.symbol or "").upper()
        if not symbol:
            continue
        positions_by_symbol[symbol] = positions_by_symbol.get(symbol, 0) + 1
        market_value = abs(float(position.market_value or 0.0))
        gross_market_value += market_value
        open_unrealized_pnl += float(getattr(position, "unrealized_pnl", 0.0) or 0.0)
        exposure_pct = market_value / account_balance * 100.0
        exposure_by_symbol_pct[symbol] = exposure_by_symbol_pct.get(symbol, 0.0) + exposure_pct
        # Accumulate exposure by sector and by broad correlation bucket so the
        # sector/correlation caps measure concentration against existing
        # positions, not just the single new order.
        sector = sector_for_symbol(symbol)
        bucket = correlation_bucket_for_symbol(symbol)
        exposure_by_sector_pct[sector] = exposure_by_sector_pct.get(sector, 0.0) + exposure_pct
        exposure_by_correlation_bucket_pct[bucket] = (
            exposure_by_correlation_bucket_pct.get(bucket, 0.0) + exposure_pct
        )

    return RiskContext(
        account_balance=account_balance,
        daily_realized_pnl_usd=daily_pnl,
        weekly_realized_pnl_usd=weekly_pnl,
        open_positions=len(portfolio.positions),
        positions_by_symbol=positions_by_symbol,
        exposure_by_symbol_pct=exposure_by_symbol_pct,
        exposure_by_sector_pct=exposure_by_sector_pct,
        exposure_by_correlation_bucket_pct=exposure_by_correlation_bucket_pct,
        open_unrealized_pnl_usd=round(open_unrealized_pnl, 2),
        gross_exposure_pct=gross_market_value / account_balance * 100.0,
        correlated_exposure_pct=max(exposure_by_correlation_bucket_pct.values(), default=0.0),
        consecutive_losses_today=consecutive_losses,
        trades_today=trades_today,
        mode="paper" if settings.execution_mode == "paper" else settings.etoro_account_mode,
    )
