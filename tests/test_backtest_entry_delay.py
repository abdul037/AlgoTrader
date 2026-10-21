"""``EngineConfig.entry_delay_bars``: the live signal-to-order delay in the backtest (2026-10-20).

The live bot turns a bar's signal into an order a scan later, minutes after the bar closed,
so the minute-bar evidence fills a bar after the next bar's open. 0 keeps the next-bar-open
fill exactly; a delayed entry never carries over a session close and never comes from a
signal that fired while a position was open.
"""

from __future__ import annotations

import pandas as pd
import pytest

from app.backtesting.cost_model import zero_cost_model
from app.backtesting.engine import BacktestEngine, EngineConfig
from app.backtesting.metrics import bars_per_year_for
from app.models.signal import Signal, SignalAction

BARS_PER_SESSION = 78  # 5m bars, 09:30-16:00 New York


def _flat_sessions(days: int = 2, *, dip_at: int | None = None) -> pd.DataFrame:
    """5m bars at 100 (open = close, +/-0.1 wick); bar ``dip_at`` trades down to 98."""

    rows = []
    for day in pd.bdate_range("2026-06-01", periods=days, tz="UTC"):
        for step in range(BARS_PER_SESSION):
            index = len(rows)
            rows.append(
                {
                    "timestamp": day + pd.Timedelta(hours=13, minutes=30 + 5 * step),
                    "open": 100.0 + index * 0.01,  # distinct opens show which bar filled
                    "high": 100.1 + index * 0.01,
                    "low": (98.0 if index == dip_at else 99.9) + index * 0.01,
                    "close": 100.0 + index * 0.01,
                    "volume": 10_000,
                }
            )
    return pd.DataFrame(rows)


class BuyOnBars:
    """BUY when the window ends on one of ``bars`` (stop 1% below, far target)."""

    name = "test_buy_on_bars"

    def __init__(self, *bars: int) -> None:
        self.bars = set(bars)

    def generate_signal(self, window: pd.DataFrame, symbol: str):
        if len(window) - 1 not in self.bars:
            return None
        price = float(window["close"].iloc[-1])
        return Signal(
            symbol=symbol,
            strategy_name=self.name,
            action=SignalAction.BUY,
            rationale="test",
            price=price,
            stop_loss=price * 0.99,
            take_profit=price * 1.5,
        )


def _run(data: pd.DataFrame, strategy, *, delay: int):
    config = EngineConfig(
        initial_cash=100_000.0,
        bars_per_year=bars_per_year_for("5m"),
        cost_model=zero_cost_model(),
        flatten_at_session_end=True,
        entry_delay_bars=delay,
    )
    return (
        BacktestEngine(config=config)
        .run(symbol="SYN", strategy=strategy, data=data, file_path="t")
        .trades
    )


@pytest.mark.parametrize("delay", [0, 1, 2])
def test_a_signal_fills_delay_bars_after_the_next_open(delay: int) -> None:
    data = _flat_sessions()
    trades = _run(data, BuyOnBars(10), delay=delay)

    fill_bar = 11 + delay
    assert len(trades) == 1
    assert pd.Timestamp(trades[0]["entry_time"]) == data["timestamp"][fill_bar]
    assert trades[0]["entry_price"] == pytest.approx(data["open"][fill_bar])


def test_a_late_order_never_carries_over_the_session_close() -> None:
    data = _flat_sessions()
    last = BARS_PER_SESSION - 1

    on_time = _run(data, BuyOnBars(last - 1), delay=0)
    assert [t["reason"] for t in on_time] == ["session_close"]  # filled on the last bar, flattened
    assert _run(data, BuyOnBars(last), delay=0) == []  # the flatten bar never enters
    # Each of these would fill early in the next session.
    assert _run(data, BuyOnBars(last - 1), delay=1) == []
    assert _run(data, BuyOnBars(last), delay=1) == []
    assert _run(data, BuyOnBars(last - 1), delay=2) == []
    assert (
        len(_run(data, BuyOnBars(last + 1), delay=2)) == 1
    )  # the next session's own signal trades


def test_a_signal_fired_while_holding_is_not_acted_on_after_the_exit() -> None:
    # Bar 5's signal fills at bar 7 and stops out on bar 9; bar 8's signal fired while holding.
    data = _flat_sessions(dip_at=9)

    trades = _run(data, BuyOnBars(5, 8), delay=1)
    assert [(t["reason"], pd.Timestamp(t["entry_time"])) for t in trades] == [
        ("stop_loss", data["timestamp"][7])
    ]
    # A signal after the exit still trades a bar late.
    later = _run(data, BuyOnBars(5, 9), delay=1)
    assert [pd.Timestamp(t["entry_time"]) for t in later] == [
        data["timestamp"][7],
        data["timestamp"][11],
    ]
