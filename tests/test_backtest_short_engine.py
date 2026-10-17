"""Short entries in the backtest engine (research only: ``EngineConfig.short_entries``).

A short opens only on a bracketed ``entry_short`` SELL, fills at the next bar's open at the
bid, stops at the worse of (stop, open) at the ask, takes profit at the target, is closed by
the opposite signal (a BUY), and pays the overnight fee plus borrow and the US sell-side fees
on its ENTRY. Long runs are covered byte-for-byte by tests/test_backtest_long_parity.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from app.backtesting.cost_model import CostModel, zero_cost_model
from app.backtesting.engine import (
    BacktestEngine,
    EngineConfig,
    OpenTrade,
    _mark_to_market,
    _override_cash,
)
from app.models.signal import Signal, SignalAction

START = datetime(2026, 1, 5, 21, 0, tzinfo=UTC)  # a Monday


class _Scripted:
    """Emits the scripted signal for a bar index (the bar whose close it decides on)."""

    name = "scripted"

    def __init__(self, script: dict[int, Signal]):
        self.script = script

    def generate_signal(self, data: pd.DataFrame, symbol: str):
        return self.script.get(len(data) - 1)


def _signal(action, price=100.0, stop=None, target=None, role=None, **metadata) -> Signal:
    if role is not None:
        metadata["signal_role"] = role
    return Signal(
        symbol="TST", strategy_name="scripted", action=action, rationale="test",
        price=price, stop_loss=stop, take_profit=target, metadata=metadata,
    )


def _short(price=100.0, stop=105.0, target=90.0, role="entry_short", **metadata) -> Signal:
    return _signal(SignalAction.SELL, price, stop, target, role, **metadata)


def _frame(bars, *, start=START, step=timedelta(days=1)) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"timestamp": start + step * i, "open": o, "high": h, "low": low, "close": c, "volume": 1_000_000}
            for i, (o, h, low, c) in enumerate(bars)
        ]
    )


FLAT = (100.0, 101.0, 99.0, 100.0)


def _run(bars, script, *, short=True, cost_model=None, **frame_kwargs):
    config = EngineConfig(short_entries=short, cost_model=cost_model or zero_cost_model())
    engine = BacktestEngine(config=config)
    return engine.run(symbol="TST", strategy=_Scripted(script), data=_frame(bars, **frame_kwargs), file_path="t")


def test_a_short_fills_at_the_next_open_at_the_bid_and_is_recorded_as_short() -> None:
    cost = CostModel(spread_bps=20.0, overnight_fee_daily_pct=0.0, include_weekend_financing=False)
    result = _run([FLAT, FLAT, (98.0, 99.0, 97.0, 98.0), FLAT], {1: _short()}, cost_model=cost)

    (trade,) = result.trades
    assert trade["side"] == "short"
    assert trade["entry_price"] == pytest.approx(98.0 * (1 - 0.001))  # the bid, not the ask
    assert trade["entry_time"] == (START + timedelta(days=2)).isoformat()  # never the signal bar
    assert trade["reason"] == "end_of_data"
    assert trade["exit_price"] == pytest.approx(100.0 * (1 + 0.001))  # the cover pays the ask
    assert trade["spread_usd"] > 0


def test_a_gap_through_the_stop_fills_at_the_open_and_loses_more_than_one_r() -> None:
    bars = [FLAT, FLAT, FLAT, (108.0, 109.0, 107.0, 108.5), FLAT]
    (trade,) = _run(bars, {1: _short()}).trades

    assert trade["reason"] == "stop_loss"
    assert trade["exit_price"] == pytest.approx(108.0)  # max(stop 105, open 108)
    assert trade["quantity"] == pytest.approx(20.0)  # $100 risk / $5 stop distance
    assert trade["pnl_usd"] == pytest.approx(-160.0)  # -1.6 R


def test_a_stop_inside_the_bar_fills_at_the_stop_at_the_ask() -> None:
    cost = CostModel(spread_bps=20.0, overnight_fee_daily_pct=0.0, include_weekend_financing=False)
    bars = [FLAT, FLAT, FLAT, (101.0, 106.0, 100.0, 104.0), FLAT]
    (trade,) = _run(bars, {1: _short()}, cost_model=cost).trades

    assert trade["reason"] == "stop_loss"
    assert trade["exit_price"] == pytest.approx(105.0 * 1.001)


def test_the_target_fills_at_the_target() -> None:
    bars = [FLAT, FLAT, FLAT, (95.0, 96.0, 89.0, 91.0), FLAT]
    (trade,) = _run(bars, {1: _short()}).trades

    assert trade["reason"] == "take_profit"
    assert trade["exit_price"] == pytest.approx(90.0)
    assert trade["pnl_usd"] == pytest.approx(200.0)  # 20 shares x $10, +2 R


def test_the_stop_wins_when_one_bar_touches_both() -> None:
    bars = [FLAT, FLAT, FLAT, (100.0, 106.0, 89.0, 95.0), FLAT]
    (trade,) = _run(bars, {1: _short()}).trades

    assert trade["reason"] == "stop_loss"
    assert trade["pnl_usd"] < 0


def test_pnl_is_quantity_times_entry_minus_exit_minus_every_cost() -> None:
    cost = CostModel(
        spread_bps=10.0, overnight_fee_daily_pct=0.00015, commission_per_share=0.01,
        slippage_bps=2.0, sec_fee_per_dollar=0.0000278, finra_taf_per_share=0.000166,
        finra_taf_max_usd=8.30, short_borrow_fee_daily_pct=0.0001,
    )
    bars = [FLAT, FLAT, FLAT, (99.0, 99.5, 96.0, 97.0), (96.0, 96.5, 94.0, 95.0)]
    (trade,) = _run(bars, {1: _short(stop=104.0, target=80.0), 3: _signal(SignalAction.BUY)}, cost_model=cost).trades

    qty, entry, exit_ = trade["quantity"], trade["entry_price"], trade["exit_price"]
    costs = (
        trade["financing_usd"] + trade["commission_usd"] + trade["slippage_usd"] + trade["regulatory_usd"]
    )
    assert trade["reason"] == "strategy_exit"
    assert exit_ < entry and trade["pnl_usd"] > 0  # the price fell: a short wins
    assert trade["pnl_usd"] == pytest.approx(qty * (entry - exit_) - costs, abs=1e-5)


def test_the_regulatory_fee_is_charged_on_the_entry_notional_not_the_exit() -> None:
    cost = CostModel(
        spread_bps=0.0, overnight_fee_daily_pct=0.0, include_weekend_financing=False,
        sec_fee_per_dollar=0.0000278, finra_taf_per_share=0.000166, finra_taf_max_usd=8.30,
    )
    bars = [FLAT, FLAT, FLAT, (95.0, 96.0, 89.0, 91.0), FLAT]
    (trade,) = _run(bars, {1: _short()}, cost_model=cost).trades

    on_entry = cost.regulatory_sell_fee_usd(notional_usd=trade["notional_usd"], quantity=trade["quantity"])
    on_exit = cost.regulatory_sell_fee_usd(notional_usd=trade["quantity"] * 90.0, quantity=trade["quantity"])
    assert on_entry != pytest.approx(on_exit)
    assert trade["regulatory_usd"] == pytest.approx(round(on_entry, 6))


def test_financing_is_the_overnight_fee_plus_borrow() -> None:
    cost = CostModel(
        spread_bps=0.0, overnight_fee_daily_pct=0.00015, weekend_multiplier=3.0,
        short_borrow_fee_daily_pct=0.0001,
    )
    thursday = datetime(2026, 1, 7, 21, 0, tzinfo=UTC)  # bar 1 fills Thursday's open
    bars = [FLAT] * 6
    (trade,) = _run(bars, {0: _short(), 4: _signal(SignalAction.BUY)}, cost_model=cost, start=thursday - timedelta(days=1)).trades

    notional = trade["notional_usd"]
    held = (datetime.fromisoformat(trade["exit_time"]).date() - datetime.fromisoformat(trade["entry_time"]).date()).days
    assert held == 4  # Thursday -> Monday
    overnight = notional * 0.00015 * (held + 1 * (3.0 - 1.0))  # one Friday close crossed
    borrow = notional * 0.0001 * held
    assert trade["financing_usd"] == pytest.approx(round(overnight + borrow, 6))


@pytest.mark.parametrize(
    "signal",
    [
        _signal(SignalAction.BUY, stop=95.0, target=110.0),
        _short(role="entry_short_watch"),
        _signal(SignalAction.SELL),  # a bare exit SELL
        _signal(SignalAction.SELL, stop=None, target=None, role="entry_short"),
        _signal(SignalAction.SELL, stop=105.0, target=None, role="entry_short"),
        _short(role=None),
    ],
    ids=["buy", "short_watch", "bare_sell", "unbracketed", "no_target", "no_role"],
)
def test_short_mode_opens_only_on_a_bracketed_entry_short(signal) -> None:
    assert _run([FLAT] * 5, {1: signal}).trades == []


def test_the_opposite_buy_closes_a_short_but_its_own_repeated_sell_does_not() -> None:
    bars = [FLAT, FLAT, FLAT, FLAT, FLAT, (97.0, 98.0, 96.0, 97.0), FLAT]
    script = {1: _short(), 2: _short(), 3: _short(role="entry_short_watch"), 4: _signal(SignalAction.BUY)}
    (trade,) = _run(bars, script).trades

    assert trade["reason"] == "strategy_exit"
    assert trade["exit_time"] == (START + timedelta(days=5)).isoformat()  # the BUY bar's next open
    assert trade["exit_price"] == pytest.approx(97.0)


def test_long_mode_never_opens_a_short_and_a_sell_still_closes_a_long() -> None:
    bars = [FLAT, FLAT, FLAT, FLAT, FLAT, (103.0, 104.0, 102.0, 103.0), FLAT]
    script = {1: _short(), 2: _signal(SignalAction.BUY, stop=95.0, target=120.0), 4: _short()}
    (trade,) = _run(bars, script, short=False).trades

    assert "side" not in trade  # long trade dicts keep their exact keys
    assert trade["entry_time"] == (START + timedelta(days=3)).isoformat()
    assert trade["reason"] == "strategy_exit"
    assert trade["exit_price"] == pytest.approx(103.0)


def test_a_short_time_stop_and_end_of_data_close_at_the_ask() -> None:
    cost = CostModel(spread_bps=20.0, overnight_fee_daily_pct=0.0, include_weekend_financing=False)
    timed = _run([FLAT] * 6, {1: _short(max_hold_bars=2)}, cost_model=cost).trades
    assert [t["reason"] for t in timed] == ["time_stop"]
    assert timed[0]["exit_time"] == (START + timedelta(days=4)).isoformat()
    assert timed[0]["exit_price"] == pytest.approx(100.0 * 1.001)

    held = _run([FLAT] * 4 + [(99.0, 99.5, 97.0, 98.0)], {1: _short()}, cost_model=cost).trades
    assert [t["reason"] for t in held] == ["end_of_data"]
    assert held[0]["exit_price"] == pytest.approx(98.0 * 1.001)


def test_a_short_marks_to_market_up_as_the_price_falls() -> None:
    trade = OpenTrade(
        entry_time=START, entry_timestamp_raw=START, entry_price=100.0, quantity=10.0, side="sell",
        stop_loss=105.0, take_profit=90.0, entry_notional=1_000.0,
    )
    assert _mark_to_market(cash=9_000.0, open_trade=trade, bar=pd.Series({"close": 100.0})) == 10_000.0
    assert _mark_to_market(cash=9_000.0, open_trade=trade, bar=pd.Series({"close": 90.0})) == 10_100.0
    assert _mark_to_market(cash=9_000.0, open_trade=trade, bar=pd.Series({"close": 110.0})) == 9_900.0


def test_a_cash_override_keeps_the_short_flag() -> None:
    assert _override_cash(EngineConfig(short_entries=True), 5_000.0).short_entries is True
    assert _override_cash(EngineConfig(), 5_000.0).short_entries is False
    engine = BacktestEngine(config=EngineConfig(short_entries=True, cost_model=zero_cost_model()))
    result = engine.run(
        symbol="TST", strategy=_Scripted({1: _short()}), data=_frame([FLAT] * 4), file_path="t", initial_cash=5_000.0
    )
    assert [t.get("side") for t in result.trades] == ["short"]
    assert result.trades[0]["quantity"] == pytest.approx(10.0)  # $50 risk / $5


def test_the_intraday_stop_floor_never_moves_a_short_stop() -> None:
    from app.backtesting.engine import _attempt_entry

    bar = pd.Series({"timestamp": START, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0})
    config = EngineConfig(short_entries=True, cost_model=zero_cost_model())
    attempt = _attempt_entry(signal=_short(), next_bar=bar, cash=10_000.0, config=config, min_stop_distance=50.0)
    assert (attempt.open_trade.side, attempt.open_trade.stop_loss, attempt.open_trade.take_profit) == ("sell", 105.0, 90.0)


def test_ending_equity_books_the_short_gain() -> None:
    bars = [FLAT, FLAT, FLAT, (95.0, 96.0, 89.0, 91.0), FLAT]
    result = _run(bars, {1: _short()})
    assert result.ending_cash == pytest.approx(10_200.0)
    assert result.metrics["total_return_pct"] == pytest.approx(2.0)
