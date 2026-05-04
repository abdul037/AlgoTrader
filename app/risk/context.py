"""Shared risk-context builders for proposal and execution gates."""

from __future__ import annotations

from typing import Any

from app.risk.guardrails import RiskContext
from app.utils.time import utc_now


def build_risk_context(settings: Any, broker: Any, executions_repo: Any) -> RiskContext:
    """Build the account context used by every hard risk validation gate.

    Proposal creation and queued execution both call this helper intentionally:
    proposal-time validation catches bad ideas early, while execution-time
    validation catches state changes between approval and order submission.
    """

    start_of_day = utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
    trades_today = executions_repo.count_since(start_of_day)
    daily_pnl, consecutive_losses = executions_repo.daily_loss_stats()
    weekly_pnl = executions_repo.period_realized_pnl(days=7)

    if settings.execution_mode == "paper":
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
    for position in portfolio.positions:
        symbol = str(position.symbol or "").upper()
        if not symbol:
            continue
        positions_by_symbol[symbol] = positions_by_symbol.get(symbol, 0) + 1

    return RiskContext(
        account_balance=account_balance,
        daily_realized_pnl_usd=daily_pnl,
        weekly_realized_pnl_usd=weekly_pnl,
        open_positions=len(portfolio.positions),
        positions_by_symbol=positions_by_symbol,
        consecutive_losses_today=consecutive_losses,
        trades_today=trades_today,
        mode=settings.etoro_account_mode,
    )
