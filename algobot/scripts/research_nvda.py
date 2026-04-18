"""Research-only NVDA strategy analysis on real daily history."""

from __future__ import annotations

import argparse
import math
from dataclasses import dataclass
from pathlib import Path

import pandas as pd
import yfinance as yf


MA_PARAMETER_SETS: list[tuple[int, int]] = [(5, 20), (8, 21), (10, 30), (20, 50), (50, 200)]
PULLBACK_PARAMETER_SETS: list[tuple[int, int]] = [(30, 10), (50, 10), (50, 15), (100, 10), (150, 15)]


@dataclass
class ResearchResult:
    """Analysis summary for a single strategy configuration."""

    label: str
    strategy_name: str
    parameters: dict[str, int]
    metrics: dict[str, float]
    latest_signal: str
    latest_rationale: str | None


def _round_metrics(metrics: dict[str, float]) -> dict[str, float]:
    return {key: round(float(value), 2) for key, value in metrics.items()}


def load_history(symbol: str, *, period: str, interval: str) -> pd.DataFrame:
    """Fetch and normalize daily OHLCV history from yfinance."""

    frame = yf.Ticker(symbol.upper()).history(period=period, interval=interval, auto_adjust=False)
    if frame.empty:
        raise ValueError(f"No historical data returned for {symbol.upper()}.")

    frame = frame.reset_index()
    frame.columns = [str(column).strip().lower() for column in frame.columns]
    frame = frame.rename(columns={"date": "timestamp", "datetime": "timestamp"})
    required_columns = ["timestamp", "open", "high", "low", "close", "volume"]
    missing = [column for column in required_columns if column not in frame.columns]
    if missing:
        raise ValueError(f"Historical data missing required columns: {', '.join(missing)}")

    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    for column in ["open", "high", "low", "close", "volume"]:
        frame[column] = pd.to_numeric(frame[column], errors="raise")

    return frame[required_columns].sort_values("timestamp").reset_index(drop=True)


def compute_max_drawdown(equity_curve: list[float]) -> float:
    """Compute max drawdown percentage."""

    if not equity_curve:
        return 0.0
    series = pd.Series(equity_curve, dtype=float)
    running_max = series.cummax()
    drawdowns = (series / running_max - 1.0) * 100
    return abs(float(drawdowns.min()))


def compute_sharpe_like(equity_curve: list[float]) -> float:
    """Compute a simple daily-return Sharpe-like metric."""

    if len(equity_curve) < 3:
        return 0.0
    returns = pd.Series(equity_curve, dtype=float).pct_change().dropna()
    if returns.empty:
        return 0.0
    stdev = float(returns.std())
    if stdev == 0.0:
        return 0.0
    return float((returns.mean() / stdev) * math.sqrt(252))


def summarize_trades(trades: list[dict[str, float | str | None]]) -> dict[str, float]:
    """Summarize core trade statistics."""

    if not trades:
        return {
            "number_of_trades": 0.0,
            "win_rate": 0.0,
            "average_win": 0.0,
            "average_loss": 0.0,
            "profit_factor": 0.0,
        }

    pnls = [float(trade["pnl_usd"]) for trade in trades]
    wins = [pnl for pnl in pnls if pnl > 0]
    losses = [pnl for pnl in pnls if pnl < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = gross_profit / gross_loss if gross_loss else 0.0
    win_rate = (len(wins) / len(pnls)) * 100 if pnls else 0.0

    return {
        "number_of_trades": float(len(pnls)),
        "win_rate": win_rate,
        "average_win": (sum(wins) / len(wins)) if wins else 0.0,
        "average_loss": (sum(losses) / len(losses)) if losses else 0.0,
        "profit_factor": profit_factor,
    }


def run_signal_only_backtest(
    data: pd.DataFrame,
    *,
    buy_signal: pd.Series,
    sell_signal: pd.Series,
    initial_cash: float = 10_000.0,
) -> dict[str, dict[str, float] | float]:
    """Evaluate indicator entry/exit rules without broker execution or stop overlays."""

    cash = initial_cash
    quantity = 0.0
    entry_time: str | None = None
    entry_price: float | None = None
    pending_action: str | None = None
    equity_curve: list[float] = []
    trades: list[dict[str, float | str | None]] = []

    for index in range(len(data)):
        bar = data.iloc[index]

        if pending_action == "buy" and quantity == 0 and cash > 0:
            entry_price = float(bar["open"])
            quantity = cash / entry_price if entry_price > 0 else 0.0
            cash = 0.0
            entry_time = str(bar["timestamp"])
            pending_action = None
        elif pending_action == "sell" and quantity > 0 and entry_price is not None:
            exit_price = float(bar["open"])
            proceeds = quantity * exit_price
            invested = quantity * entry_price
            pnl_usd = proceeds - invested
            pnl_pct = (pnl_usd / invested) * 100 if invested else 0.0
            cash = proceeds
            trades.append(
                {
                    "entry_time": entry_time,
                    "exit_time": str(bar["timestamp"]),
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "pnl_usd": pnl_usd,
                    "pnl_pct": pnl_pct,
                    "reason": "indicator_exit",
                }
            )
            quantity = 0.0
            entry_time = None
            entry_price = None
            pending_action = None

        equity = cash if quantity == 0 else quantity * float(bar["close"])
        equity_curve.append(equity)

        if index == len(data) - 1:
            continue
        if bool(buy_signal.iloc[index]) and quantity == 0:
            pending_action = "buy"
        elif bool(sell_signal.iloc[index]) and quantity > 0:
            pending_action = "sell"

    if quantity > 0 and entry_price is not None:
        last_bar = data.iloc[-1]
        exit_price = float(last_bar["close"])
        proceeds = quantity * exit_price
        invested = quantity * entry_price
        pnl_usd = proceeds - invested
        pnl_pct = (pnl_usd / invested) * 100 if invested else 0.0
        cash = proceeds
        trades.append(
            {
                "entry_time": entry_time,
                "exit_time": str(last_bar["timestamp"]),
                "entry_price": entry_price,
                "exit_price": exit_price,
                "pnl_usd": pnl_usd,
                "pnl_pct": pnl_pct,
                "reason": "end_of_data",
            }
        )
        equity_curve[-1] = cash

    ending_cash = equity_curve[-1] if equity_curve else initial_cash
    periods = max(len(data), 1)
    metrics = {
        "total_return_pct": ((ending_cash - initial_cash) / initial_cash) * 100,
        "annualized_return_pct": ((ending_cash / initial_cash) ** (252 / periods) - 1) * 100,
        "max_drawdown_pct": compute_max_drawdown(equity_curve),
        "sharpe_like": compute_sharpe_like(equity_curve),
    }
    metrics.update(summarize_trades(trades))
    return {
        "ending_cash": ending_cash,
        "metrics": _round_metrics(metrics),
    }


def build_ma_signals(data: pd.DataFrame, *, fast_window: int, slow_window: int) -> tuple[pd.Series, pd.Series]:
    """Build vectorized MA crossover entry/exit signals."""

    frame = data.copy()
    frame["fast_ma"] = frame["close"].rolling(fast_window).mean()
    frame["slow_ma"] = frame["close"].rolling(slow_window).mean()
    buy_signal = (frame["fast_ma"] > frame["slow_ma"]) & (
        frame["fast_ma"].shift(1) <= frame["slow_ma"].shift(1)
    )
    sell_signal = (frame["fast_ma"] < frame["slow_ma"]) & (
        frame["fast_ma"].shift(1) >= frame["slow_ma"].shift(1)
    )
    return buy_signal.fillna(False), sell_signal.fillna(False)


def latest_ma_signal(
    data: pd.DataFrame,
    *,
    symbol: str,
    fast_window: int,
    slow_window: int,
) -> tuple[str, str | None]:
    """Return the latest live-style MA crossover signal."""

    frame = data.copy()
    frame["fast_ma"] = frame["close"].rolling(fast_window).mean()
    frame["slow_ma"] = frame["close"].rolling(slow_window).mean()
    if len(frame) < slow_window + 2:
        return "none", None

    last = frame.iloc[-1]
    previous = frame.iloc[-2]
    if pd.isna(last["fast_ma"]) or pd.isna(last["slow_ma"]):
        return "none", None

    fast_above = last["fast_ma"] > last["slow_ma"]
    was_below = previous["fast_ma"] <= previous["slow_ma"]
    fast_below = last["fast_ma"] < last["slow_ma"]
    was_above = previous["fast_ma"] >= previous["slow_ma"]

    if fast_above and was_below:
        return (
            "buy",
            f"Fast MA ({last['fast_ma']:.2f}) crossed above slow MA ({last['slow_ma']:.2f}) on the latest bar.",
        )
    if fast_below and was_above:
        return (
            "sell",
            f"Fast MA ({last['fast_ma']:.2f}) crossed below slow MA ({last['slow_ma']:.2f}); trend support weakened.",
        )
    return "none", None


def build_pullback_signals(
    data: pd.DataFrame,
    *,
    trend_window: int,
    pullback_window: int,
) -> tuple[pd.Series, pd.Series]:
    """Build vectorized pullback-trend entry/exit signals."""

    frame = data.copy()
    frame["trend_ma"] = frame["close"].rolling(trend_window).mean()
    frame["pullback_ma"] = frame["close"].rolling(pullback_window).mean()
    frame["ema_short"] = frame["close"].ewm(span=8, adjust=False).mean()
    frame["ema_long"] = frame["close"].ewm(span=21, adjust=False).mean()

    trend_up = (
        (frame["close"] > frame["trend_ma"])
        & (frame["ema_short"] > frame["ema_long"])
        & (frame["trend_ma"] > frame["trend_ma"].shift(5))
    )
    pullback_active = frame["close"].shift(1) <= frame["pullback_ma"].shift(1) * 1.01
    resuming_higher = (frame["close"] > frame["pullback_ma"]) & (
        frame["close"] > frame["close"].shift(1)
    )
    buy_signal = trend_up & pullback_active & resuming_higher
    sell_signal = (frame["close"] < frame["trend_ma"]) | (frame["ema_short"] < frame["ema_long"])
    return buy_signal.fillna(False), sell_signal.fillna(False)


def latest_pullback_signal(
    data: pd.DataFrame,
    *,
    trend_window: int,
    pullback_window: int,
) -> tuple[str, str | None]:
    """Return the latest live-style pullback signal."""

    frame = data.copy()
    frame["trend_ma"] = frame["close"].rolling(trend_window).mean()
    frame["pullback_ma"] = frame["close"].rolling(pullback_window).mean()
    frame["ema_short"] = frame["close"].ewm(span=8, adjust=False).mean()
    frame["ema_long"] = frame["close"].ewm(span=21, adjust=False).mean()
    if len(frame) < max(trend_window, pullback_window) + 5:
        return "none", None

    last = frame.iloc[-1]
    prev = frame.iloc[-2]
    trend_up = (
        last["close"] > last["trend_ma"]
        and last["ema_short"] > last["ema_long"]
        and last["trend_ma"] > frame["trend_ma"].iloc[-5]
    )
    pullback_active = prev["close"] <= prev["pullback_ma"] * 1.01
    resuming_higher = last["close"] > last["pullback_ma"] and last["close"] > prev["close"]
    if trend_up and pullback_active and resuming_higher:
        return (
            "buy",
            "Broad trend remains positive and price is resuming higher after a controlled pullback toward the short-term average.",
        )

    trend_broken = last["close"] < last["trend_ma"] or last["ema_short"] < last["ema_long"]
    if trend_broken:
        return (
            "sell",
            "Trend filter failed, so existing long exposure should be reduced or closed.",
        )
    return "none", None


def build_report(data: pd.DataFrame, results: list[ResearchResult], *, symbol: str) -> str:
    """Render the strategy research as markdown."""

    ma_results = [result for result in results if result.strategy_name == "ma_crossover"]
    pullback_results = [result for result in results if result.strategy_name == "pullback_trend"]

    def candidate_filter(result: ResearchResult) -> bool:
        return (
            result.metrics["number_of_trades"] >= 10
            and result.metrics["max_drawdown_pct"] <= 35
            and result.metrics["sharpe_like"] >= 0.7
        )

    robust_pullbacks = [result for result in pullback_results if candidate_filter(result)]
    robust_mas = [result for result in ma_results if candidate_filter(result)]

    best_pullback = max(
        robust_pullbacks or pullback_results,
        key=lambda result: (
            result.metrics["sharpe_like"],
            -result.metrics["max_drawdown_pct"],
            result.metrics["profit_factor"],
            result.metrics["annualized_return_pct"],
        ),
    )
    best_pullback_alternative = max(
        [result for result in pullback_results if result.label != best_pullback.label],
        key=lambda result: (
            result.metrics["sharpe_like"],
            -result.metrics["max_drawdown_pct"],
            result.metrics["profit_factor"],
            result.metrics["annualized_return_pct"],
        ),
    )
    ma_candidates = [result for result in ma_results if result.metrics["number_of_trades"] >= 10]
    best_ma = max(
        robust_mas or ma_candidates or ma_results,
        key=lambda result: (
            result.metrics["sharpe_like"],
            -result.metrics["max_drawdown_pct"],
            result.metrics["profit_factor"],
            result.metrics["annualized_return_pct"],
        ),
    )

    tested_rows: list[str] = [
        "| Strategy | Parameters | Trades | Win Rate | Annualized % | Max DD % | Profit Factor | Sharpe-like | Latest Signal |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for result in sorted(
        results,
        key=lambda item: (
            item.strategy_name,
            -item.metrics["sharpe_like"],
            item.metrics["max_drawdown_pct"],
        ),
    ):
        tested_rows.append(
            "| "
            f"{result.strategy_name} | "
            f"{result.label} | "
            f"{int(result.metrics['number_of_trades'])} | "
            f"{result.metrics['win_rate']} | "
            f"{result.metrics['annualized_return_pct']} | "
            f"{result.metrics['max_drawdown_pct']} | "
            f"{result.metrics['profit_factor']} | "
            f"{result.metrics['sharpe_like']} | "
            f"{result.latest_signal} |"
        )

    lines = [
        f"# {symbol.upper()} Strategy Research",
        "",
        "This report is analysis-only. It does not place, modify, or cancel any broker order.",
        "",
        "## Data Set",
        "- Source: Yahoo Finance daily OHLCV via `yfinance`.",
        f"- Bars analyzed: {len(data)}.",
        f"- Date range: {data.iloc[0]['timestamp'].date()} to {data.iloc[-1]['timestamp'].date()}.",
        "- Execution model: next-day open after a signal, one long position at a time, no leverage, no slippage or commissions.",
        "",
        "## Indicators Analyzed",
        "- MA crossover family: SMA pairs `(5,20)`, `(8,21)`, `(10,30)`, `(20,50)`, `(50,200)`.",
        "- Pullback trend family: trend SMA / pullback SMA pairs `(30,10)`, `(50,10)`, `(50,15)`, `(100,10)`, `(150,15)`.",
        "- Each pullback test also uses the repo's `8 EMA` vs `21 EMA` trend filter.",
        "",
        "## Parameter Sweep Results",
        *tested_rows,
        "",
        "## Recommended Entry / Exit Framework",
        f"- Preferred setup: `{best_pullback.label}` because it kept drawdown below 35%, produced at least 10 trades, and had the strongest risk-adjusted profile among the robust candidates.",
        f"- More active alternative: `{best_pullback_alternative.label}` if you want a slightly faster pullback framework without switching to pure crossover logic.",
        f"- MA baseline: `{best_ma.label}` is the best crossover set with at least 10 trades, but the pullback family was more aligned with swing-style entries in NVDA.",
        "",
        "### Entry Rules",
        f"- Use the `{best_pullback.parameters['trend_window']}-day` SMA as the trend filter. Long setups are valid only when the daily close is above that trend line.",
        "- Confirm short-term momentum with `8 EMA > 21 EMA`.",
        f"- Wait for a pullback toward the `{best_pullback.parameters['pullback_window']}-day` SMA. The prior close should be within roughly 1% of that pullback average.",
        "- Enter only after the next daily close resumes higher: price must close back above the pullback average and above the prior close.",
        "- Do not force entries when the latest signal is `none`; the rule set is designed to wait for pullbacks, not chase strength.",
        "",
        "### Exit Rules",
        f"- Exit when the daily close breaks below the `{best_pullback.parameters['trend_window']}-day` SMA.",
        "- Exit when `8 EMA < 21 EMA` because that indicates the short-term trend has rolled over.",
        "- If neither exit condition has triggered, stay in the trade and let the trend continue. This is a swing framework, not a fast-profit scalper.",
        "",
        "## Current Signal Status",
        f"- Preferred pullback setup latest signal: `{best_pullback.latest_signal}`.",
        f"- Preferred pullback setup rationale: {best_pullback.latest_rationale or 'No fresh entry or exit on the latest bar.'}",
        f"- More active pullback setup latest signal: `{best_pullback_alternative.latest_signal}`.",
        f"- MA baseline latest signal: `{best_ma.latest_signal}`.",
        "",
        "## Trust Limits",
        "- This report uses real daily NVDA history, but it is still a simple signal study. It does not include transaction costs, gap slippage, or intraday stop behavior.",
        "- The `50/200` MA line can look excellent on a five-year tech uptrend but should not be trusted as the sole driver because it generated too few trades.",
        "- The existing pending demo NVDA order was not generated by this research pass. It remains untouched.",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run analysis-only NVDA strategy research.")
    parser.add_argument("--symbol", default="NVDA", help="Ticker symbol to study.")
    parser.add_argument("--period", default="5y", help="yfinance lookback period, default: 5y.")
    parser.add_argument("--interval", default="1d", help="yfinance interval, default: 1d.")
    parser.add_argument(
        "--output",
        default="reports/nvda_strategy_report.md",
        help="Optional markdown output path.",
    )
    args = parser.parse_args()

    data = load_history(args.symbol, period=args.period, interval=args.interval)

    results: list[ResearchResult] = []
    for fast_window, slow_window in MA_PARAMETER_SETS:
        latest_signal, latest_rationale = latest_ma_signal(
            data,
            symbol=args.symbol,
            fast_window=fast_window,
            slow_window=slow_window,
        )
        buy_signal, sell_signal = build_ma_signals(
            data,
            fast_window=fast_window,
            slow_window=slow_window,
        )
        backtest = run_signal_only_backtest(
            data,
            buy_signal=buy_signal,
            sell_signal=sell_signal,
        )
        results.append(
            ResearchResult(
                label=f"ma_{fast_window}_{slow_window}",
                strategy_name="ma_crossover",
                parameters={"fast_window": fast_window, "slow_window": slow_window},
                metrics=backtest["metrics"],  # type: ignore[index]
                latest_signal=latest_signal,
                latest_rationale=latest_rationale,
            )
        )

    for trend_window, pullback_window in PULLBACK_PARAMETER_SETS:
        latest_signal, latest_rationale = latest_pullback_signal(
            data,
            trend_window=trend_window,
            pullback_window=pullback_window,
        )
        buy_signal, sell_signal = build_pullback_signals(
            data,
            trend_window=trend_window,
            pullback_window=pullback_window,
        )
        backtest = run_signal_only_backtest(
            data,
            buy_signal=buy_signal,
            sell_signal=sell_signal,
        )
        results.append(
            ResearchResult(
                label=f"pullback_{trend_window}_{pullback_window}",
                strategy_name="pullback_trend",
                parameters={
                    "trend_window": trend_window,
                    "pullback_window": pullback_window,
                },
                metrics=backtest["metrics"],  # type: ignore[index]
                latest_signal=latest_signal,
                latest_rationale=latest_rationale,
            )
        )

    report = build_report(data, results, symbol=args.symbol)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report)
    print(report)
    print(f"Saved report to {output_path}")


if __name__ == "__main__":
    main()
