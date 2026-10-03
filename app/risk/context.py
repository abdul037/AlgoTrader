"""Shared risk-context builders for proposal and execution gates."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from app.risk.guardrails import RiskContext
from app.risk.rules import effective_max_risk_per_trade_pct
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
    # The loss-streak cooldown is per trading day (operator sign-off 2026-09-27).
    # The all-history streak never reset: a halted bot can't book the win that
    # would clear it, which locked trading out from 2026-09-16 onward.
    daily_pnl, consecutive_losses = executions_repo.daily_loss_stats()
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
    # Broker position objects don't carry their protective stop (it lives on the
    # bracket legs), so exact entry->stop risk isn't recoverable here. Use a
    # conservative proxy: assume each open position still carries its full initial
    # per-trade risk budget. This never under-estimates portfolio heat, which is
    # the safe direction for an aggregate risk cap.
    per_position_risk_usd = account_balance * (
        effective_max_risk_per_trade_pct(settings, live=settings.execution_mode == "live") / 100.0
    )
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
        recently_stopped_symbols=recently_stopped_symbols(settings, executions_repo),
        entries_today_by_correlation_bucket=entries_today_by_bucket(executions_repo, start_of_day),
        open_trade_risks_usd=[per_position_risk_usd] * len(portfolio.positions),
        mode="paper" if settings.execution_mode == "paper" else settings.etoro_account_mode,
    )


def recently_stopped_symbols(settings: Any, executions_repo: Any) -> list[str]:
    """Symbols whose most recent closed trade lost money within the re-entry cooldown."""

    minutes = int(getattr(settings, "reentry_cooldown_minutes_after_loss", 0) or 0)
    if minutes <= 0 or not hasattr(executions_repo, "list"):
        return []
    cutoff = utc_now() - timedelta(minutes=minutes)
    latest_exit: dict[str, tuple[datetime, float]] = {}
    for execution in executions_repo.list(limit=200):
        pnl = float(getattr(execution, "realized_pnl_usd", 0.0) or 0.0)
        symbol = str((execution.request_payload or {}).get("symbol") or "").upper()
        if not pnl or not symbol:
            continue
        payload = dict(execution.response_payload or {})
        fills = [
            leg.get("filled_at")
            for leg in [*(dict(payload.get("broker_execution") or {}).get("legs") or []), dict(payload.get("exit_fill") or {})]
            if str(leg.get("status") or "").lower() == "filled" and leg.get("filled_at")
        ]
        if not fills:
            continue
        exited = datetime.fromisoformat(str(max(fills)).replace("Z", "+00:00"))
        exited = exited if exited.tzinfo else exited.replace(tzinfo=UTC)
        if symbol not in latest_exit or exited > latest_exit[symbol][0]:
            latest_exit[symbol] = (exited, pnl)
    return sorted(symbol for symbol, (exited, pnl) in latest_exit.items() if pnl < 0 and exited >= cutoff)


_LIVE_ENTRY_STATUSES = {"filled", "submitted", "partially_filled", "accepted", "new", "pending_new"}


def entries_today_by_bucket(executions_repo: Any, start_of_day: datetime) -> dict[str, int]:
    """Count today's entries (placed or filled, not failed) per correlation bucket."""

    if not hasattr(executions_repo, "list"):
        return {}
    since = start_of_day.astimezone(UTC).isoformat()
    counts: dict[str, int] = {}
    for execution in executions_repo.list(limit=200):
        if str(getattr(execution, "created_at", "") or "") < since:
            continue
        if str(getattr(execution, "status", "") or "").lower() not in _LIVE_ENTRY_STATUSES:
            continue
        symbol = str((execution.request_payload or {}).get("symbol") or "").upper()
        if symbol:
            bucket = correlation_bucket_for_symbol(symbol)
            counts[bucket] = counts.get(bucket, 0) + 1
    return counts
