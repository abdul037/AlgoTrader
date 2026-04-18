"""Reusable backtesting engine."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd
from pydantic import BaseModel, Field

from app.backtesting.metrics import compute_max_drawdown, compute_sharpe_like, summarize_trades
from app.storage.repositories import BacktestRepository
from app.utils.ids import generate_id
from app.utils.time import utc_now


@dataclass
class OpenTrade:
    """State for an open backtest position."""

    entry_time: str
    entry_price: float
    quantity: float
    stop_loss: float | None
    take_profit: float | None


class BacktestResult(BaseModel):
    """Backtest summary."""

    id: str
    symbol: str
    strategy_name: str
    initial_cash: float
    ending_cash: float
    metrics: dict[str, float]
    trades: list[dict[str, Any]] = Field(default_factory=list)


class BacktestEngine:
    """Execute a strategy over historical OHLCV data."""

    def __init__(self, repository: BacktestRepository | None = None):
        self.repository = repository

    def run(
        self,
        *,
        symbol: str,
        strategy: Any,
        data: pd.DataFrame,
        file_path: str,
        initial_cash: float = 10000.0,
    ) -> BacktestResult:
        started_at = utc_now().isoformat()
        cash = initial_cash
        open_trade: OpenTrade | None = None
        equity_curve: list[float] = []
        trades: list[dict[str, Any]] = []

        for index in range(len(data)):
            window = data.iloc[: index + 1].copy()
            bar = window.iloc[-1]
            signal = strategy.generate_signal(window, symbol)

            if open_trade is not None:
                exit_price: float | None = None
                exit_reason = ""
                if open_trade.stop_loss and bar["low"] <= open_trade.stop_loss:
                    exit_price = open_trade.stop_loss
                    exit_reason = "stop_loss"
                elif open_trade.take_profit and bar["high"] >= open_trade.take_profit:
                    exit_price = open_trade.take_profit
                    exit_reason = "take_profit"
                elif signal is not None and signal.action.value == "sell":
                    exit_price = float(bar["close"])
                    exit_reason = "strategy_exit"
                elif index == len(data) - 1:
                    exit_price = float(bar["close"])
                    exit_reason = "end_of_data"

                if exit_price is not None:
                    proceeds = open_trade.quantity * exit_price
                    invested = open_trade.quantity * open_trade.entry_price
                    cash = proceeds
                    pnl_usd = proceeds - invested
                    pnl_pct = (pnl_usd / invested) * 100 if invested else 0.0
                    trades.append(
                        {
                            "entry_time": open_trade.entry_time,
                            "exit_time": str(bar["timestamp"]),
                            "entry_price": open_trade.entry_price,
                            "exit_price": exit_price,
                            "pnl_usd": pnl_usd,
                            "pnl_pct": pnl_pct,
                            "reason": exit_reason,
                        }
                    )
                    open_trade = None

            if open_trade is None and signal is not None and signal.action.value == "buy":
                entry_price = float(signal.price or bar["close"])
                if entry_price > 0:
                    quantity = cash / entry_price
                    open_trade = OpenTrade(
                        entry_time=str(bar["timestamp"]),
                        entry_price=entry_price,
                        quantity=quantity,
                        stop_loss=signal.stop_loss,
                        take_profit=signal.take_profit,
                    )
                    cash = 0.0

            equity = cash if open_trade is None else open_trade.quantity * float(bar["close"])
            equity_curve.append(equity)

        ending_cash = equity_curve[-1] if equity_curve else initial_cash
        total_return_pct = ((ending_cash - initial_cash) / initial_cash) * 100 if initial_cash else 0.0
        periods = max(len(data), 1)
        annualized_return_pct = ((ending_cash / initial_cash) ** (252 / periods) - 1) * 100 if initial_cash else 0.0

        metrics = {
            "total_return_pct": total_return_pct,
            "annualized_return_pct": annualized_return_pct,
            "max_drawdown_pct": compute_max_drawdown(equity_curve),
            "sharpe_like": compute_sharpe_like(equity_curve),
        }
        metrics.update(summarize_trades(trades))

        result = BacktestResult(
            id=generate_id("bt"),
            symbol=symbol.upper(),
            strategy_name=strategy.name,
            initial_cash=initial_cash,
            ending_cash=ending_cash,
            metrics=metrics,
            trades=trades,
        )

        if self.repository is not None:
            self.repository.create(
                backtest_id=result.id,
                symbol=result.symbol,
                strategy_name=result.strategy_name,
                file_path=file_path,
                started_at=started_at,
                completed_at=utc_now().isoformat(),
                metrics=result.metrics,
                trades=result.trades,
            )
        return result
