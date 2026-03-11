from __future__ import annotations

import math
from datetime import UTC, datetime

from app.schemas import BacktestResult, BacktestTrade, EquityPoint, PriceBar, SupportedInterval
from app.services.signal_engine import generate_signal_report


def _round(value: float) -> float:
    return round(value, 2)


def _max_drawdown(equity_curve: list[EquityPoint]) -> float:
    if not equity_curve:
        return 0.0

    peak = equity_curve[0].equity
    max_drawdown = 0.0
    for point in equity_curve:
        peak = max(peak, point.equity)
        if peak > 0:
            max_drawdown = max(max_drawdown, ((peak - point.equity) / peak) * 100)
    return _round(max_drawdown)


def _sharpe_ratio(equity_curve: list[EquityPoint]) -> float:
    if len(equity_curve) < 3:
        return 0.0

    returns: list[float] = []
    for index in range(1, len(equity_curve)):
        previous = equity_curve[index - 1].equity
        current = equity_curve[index].equity
        if previous > 0:
            returns.append((current - previous) / previous)

    if len(returns) < 2:
        return 0.0

    average = sum(returns) / len(returns)
    variance = sum((value - average) ** 2 for value in returns) / (len(returns) - 1)
    deviation = math.sqrt(variance)
    if deviation == 0:
        return 0.0
    return round((average / deviation) * math.sqrt(252), 2)


def run_backtest(
    symbol: str,
    interval: SupportedInterval,
    provider: str,
    mode: str,
    bars: list[PriceBar],
    starting_capital: float = 10000,
) -> BacktestResult:
    if len(bars) < 120:
        raise RuntimeError("At least 120 candles are required for backtesting.")

    cash = float(starting_capital)
    quantity = 0
    entry_price = 0.0
    entry_index = -1
    trades: list[BacktestTrade] = []
    equity_curve: list[EquityPoint] = []

    for index in range(60, len(bars)):
        window = bars[: index + 1]
        report = generate_signal_report(symbol, interval, provider, mode, window)
        close = bars[index].close

        if quantity == 0 and report.signal == "buy":
            next_quantity = int(cash // close)
            if next_quantity > 0:
                quantity = next_quantity
                cash -= quantity * close
                entry_price = close
                entry_index = index
        elif quantity > 0 and report.signal == "sell":
            cash += quantity * close
            trades.append(
                BacktestTrade(
                    entryTime=bars[entry_index].time,
                    exitTime=bars[index].time,
                    entryPrice=_round(entry_price),
                    exitPrice=_round(close),
                    quantity=quantity,
                    profitLoss=_round((close - entry_price) * quantity),
                    returnPct=_round(((close - entry_price) / entry_price) * 100),
                    barsHeld=index - entry_index,
                )
            )
            quantity = 0
            entry_price = 0.0
            entry_index = -1

        equity_curve.append(EquityPoint(time=bars[index].time, equity=_round(cash + quantity * close)))

    last_bar = bars[-1]
    if quantity > 0:
        cash += quantity * last_bar.close
        trades.append(
            BacktestTrade(
                entryTime=bars[entry_index].time,
                exitTime=last_bar.time,
                entryPrice=_round(entry_price),
                exitPrice=_round(last_bar.close),
                quantity=quantity,
                profitLoss=_round((last_bar.close - entry_price) * quantity),
                returnPct=_round(((last_bar.close - entry_price) / entry_price) * 100),
                barsHeld=len(bars) - 1 - entry_index,
            )
        )

    wins = len([trade for trade in trades if trade.profit_loss > 0])
    ending_capital = _round(cash)
    buy_hold_return = _round(((last_bar.close - bars[60].close) / bars[60].close) * 100)

    return BacktestResult(
        symbol=symbol,
        interval=interval,
        startingCapital=starting_capital,
        endingCapital=ending_capital,
        totalReturnPct=_round(((ending_capital - starting_capital) / starting_capital) * 100),
        buyHoldReturnPct=buy_hold_return,
        maxDrawdownPct=_max_drawdown(equity_curve),
        winRatePct=_round((wins / len(trades)) * 100) if trades else 0.0,
        sharpeRatio=_sharpe_ratio(equity_curve),
        trades=trades,
        equityCurve=equity_curve,
        generatedAt=datetime.now(UTC).isoformat(),
    )
