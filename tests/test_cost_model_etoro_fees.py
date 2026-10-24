"""eToro's own trade fees in the backtests behind the live evidence (operator 2026-10-18).

$1.00 on each real (1x) US stock buy and sell, about 1% a side on crypto, charged on the
entry and the exit inside ``commission_usd``. Only the batch runner (every row it writes, the
``:walk_forward_oos`` evidence rows included) and the short research built on it apply them:
``CostModel()`` itself, the Alpaca preset and every other caller keep charging nothing. The
long CFD-style overnight fee stays. That a long run with the fee at 0 is byte-identical to the
pre-short code, and that the fee is the only difference, is in test_backtest_long_parity.py.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from app.backtesting import batch as batch_module
from app.backtesting import cost_model as cost_module
from app.backtesting.batch import BatchBacktestService
from app.backtesting.cost_model import CostModel, with_etoro_trade_fees, zero_cost_model
from app.backtesting.engine import BacktestEngine, EngineConfig
from app.models.signal import Signal, SignalAction
from tests.conftest import make_settings

START = datetime(2026, 1, 5, 21, 0, tzinfo=UTC)  # a Monday
FLAT = (100.0, 101.0, 99.0, 100.0)
# Entry at bar 2's open (100), target 110 hit on bar 3: 20 units ($100 risk / $5 stop), +$200.
TARGET_BARS = [FLAT, FLAT, FLAT, (101.0, 111.0, 100.0, 110.0), FLAT]


class _Scripted:
    """Emits the scripted signal for a bar index (the bar whose close it decides on)."""

    name = "scripted"

    def __init__(self, script: dict[int, Signal]):
        self.script = script

    def generate_signal(self, data: pd.DataFrame, symbol: str):
        return self.script.get(len(data) - 1)


def _signal(action, *, stop, target, **metadata) -> Signal:
    return Signal(
        symbol="TST", strategy_name="scripted", action=action, rationale="test",
        price=100.0, stop_loss=stop, take_profit=target, metadata=metadata,
    )


def _frame(bars) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"timestamp": START + timedelta(days=i), "open": o, "high": h, "low": low, "close": c, "volume": 1e6}
            for i, (o, h, low, c) in enumerate(bars)
        ]
    )


def _run(cost_model, bars=TARGET_BARS, script=None, *, short=False):
    script = script or {1: _signal(SignalAction.BUY, stop=95.0, target=110.0)}
    engine = BacktestEngine(config=EngineConfig(cost_model=cost_model, short_entries=short))
    return engine.run(symbol="TST", strategy=_Scripted(script), data=_frame(bars), file_path="t")


def test_the_fee_schedule_is_the_one_this_account_pays() -> None:
    assert cost_module.ETORO_STOCK_FEE_USD_PER_LEG == 1.0
    assert cost_module.ETORO_CRYPTO_FEE_PER_LEG == 0.01


def test_no_other_cost_model_charges_the_fees() -> None:
    for model in (CostModel(), CostModel.alpaca_equities(), zero_cost_model()):
        assert (model.commission_flat_usd_per_leg, model.commission_pct_per_leg) == (0.0, 0.0)
        assert model.leg_fee_usd(notional_usd=5_000.0) == 0.0


def test_a_stock_pays_a_dollar_a_leg_and_keeps_every_other_leg() -> None:
    base = CostModel()
    stock = with_etoro_trade_fees(base, crypto=False)
    assert stock == dataclasses.replace(base, commission_flat_usd_per_leg=1.0)
    # Kept on purpose although a real 1x stock pays no overnight fee: dropping it loosens the evidence.
    assert (stock.overnight_fee_daily_pct, stock.include_weekend_financing) == (0.00015, True)
    assert stock.leg_fee_usd(notional_usd=2_500.0) == 1.0
    assert stock.leg_fee_usd(notional_usd=0.0) == 0.0


def test_crypto_pays_one_percent_of_each_leg_and_no_flat_fee() -> None:
    crypto = with_etoro_trade_fees(CostModel(), crypto=True)
    assert crypto == dataclasses.replace(CostModel(), commission_pct_per_leg=0.01)
    assert crypto.leg_fee_usd(notional_usd=2_500.0) == pytest.approx(25.0)


def test_a_long_pays_one_dollar_on_the_entry_and_one_on_the_exit() -> None:
    free = _run(zero_cost_model())
    paid = _run(dataclasses.replace(zero_cost_model(), commission_flat_usd_per_leg=1.0))

    (base,), (trade,) = free.trades, paid.trades
    assert (base["reason"], base["quantity"], base["pnl_usd"]) == ("take_profit", pytest.approx(20.0), pytest.approx(200.0))
    assert trade["commission_usd"] == 2.0 and base["commission_usd"] == 0.0
    assert trade["pnl_usd"] == pytest.approx(198.0, abs=1e-9)  # exactly $1 a leg lower
    assert {k: v for k, v in trade.items() if k not in ("commission_usd", "pnl_usd", "pnl_pct")} == {
        k: v for k, v in base.items() if k not in ("commission_usd", "pnl_usd", "pnl_pct")
    }
    assert paid.ending_cash == pytest.approx(free.ending_cash - 2.0, abs=1e-9)
    assert paid.cost_breakdown["commission_cost_usd"] == 2.0
    assert paid.metrics["total_cost_usd"] == pytest.approx(free.metrics["total_cost_usd"] + 2.0)


def test_a_crypto_fee_is_charged_on_both_notionals() -> None:
    (trade,) = _run(dataclasses.replace(zero_cost_model(), commission_pct_per_leg=0.01)).trades

    # 0.01 x ($2,000 in + $2,200 out)
    assert trade["commission_usd"] == pytest.approx(42.0)
    assert trade["pnl_usd"] == pytest.approx(200.0 - 42.0)


def test_a_short_pays_the_same_two_legs() -> None:
    bars = [FLAT, FLAT, FLAT, (95.0, 96.0, 89.0, 91.0), FLAT]
    script = {1: _signal(SignalAction.SELL, stop=105.0, target=90.0, signal_role="entry_short")}
    (base,) = _run(zero_cost_model(), bars, script, short=True).trades
    (trade,) = _run(dataclasses.replace(zero_cost_model(), commission_flat_usd_per_leg=1.0), bars, script, short=True).trades

    assert trade["side"] == "short" and trade["reason"] == "take_profit"
    assert trade["commission_usd"] == 2.0
    assert trade["pnl_usd"] == pytest.approx(base["pnl_usd"] - 2.0, abs=1e-9)


class _PatternBuyer:
    """Buys after two flat closes (bracket 95 / 110), so every 5-bar block trades once."""

    name = "pattern_buyer"

    def generate_signal(self, data: pd.DataFrame, symbol: str):
        if len(data) >= 2 and data["close"].iloc[-1] == 100.0 and data["close"].iloc[-2] == 100.0:
            return _signal(SignalAction.BUY, stop=95.0, target=110.0)
        return None


STOP_BARS = [FLAT, FLAT, FLAT, (99.0, 100.0, 94.0, 95.0), FLAT]


class _Bars:
    def get_history(self, symbol, *, timeframe, bars=None, provider=None, force_refresh=False):
        # 400 daily bars, a target block then a stop block: a dozen 14-day folds plus the holdout.
        return _frame((TARGET_BARS + STOP_BARS) * 40)


class _Rows:
    def __init__(self):
        self.rows: list[dict] = []

    def create(self, **kwargs):
        self.rows.append(kwargs)


class _Logs:
    def log(self, event, payload):
        pass


def _batch_rows(tmp_path, walk_forward: bool) -> dict[str, dict]:
    rows = _Rows()
    service = BatchBacktestService(
        settings=make_settings(tmp_path), market_data_engine=_Bars(), backtest_repository=rows, run_log_repository=_Logs()
    )
    summary = service.run(
        symbols=["SYNA", "BTC/USD"], timeframes=["1d"], strategy_names=["ema_trend_stack"], walk_forward=walk_forward
    )
    assert summary.errors == [] and len(rows.rows) == 2
    # Walk-forward persists only the OOS row; in-sample, the engine persists the run itself.
    assert all(row["file_path"].endswith(":walk_forward_oos") == walk_forward for row in rows.rows)
    by_symbol = {row["symbol"]: row for row in rows.rows}
    assert set(by_symbol) == {"SYNA", "BTC/USD"}
    return by_symbol


@pytest.mark.parametrize("walk_forward", [True, False])
def test_the_batch_charges_stocks_a_dollar_a_leg_and_crypto_one_percent(tmp_path, monkeypatch, walk_forward) -> None:
    monkeypatch.setattr(batch_module, "get_strategy", lambda name, **kwargs: _PatternBuyer())
    with monkeypatch.context() as free:
        free.setattr(cost_module, "ETORO_STOCK_FEE_USD_PER_LEG", 0.0)
        free.setattr(cost_module, "ETORO_CRYPTO_FEE_PER_LEG", 0.0)
        fee_free = _batch_rows(tmp_path, walk_forward)
    paid = _batch_rows(tmp_path, walk_forward)

    stock, crypto = paid["SYNA"], paid["BTC/USD"]
    assert len(stock["trades"]) >= 10 and len(crypto["trades"]) == len(stock["trades"])
    for trade in stock["trades"]:
        assert trade["commission_usd"] == 2.0
    for trade in crypto["trades"]:
        exit_notional = trade["quantity"] * trade["exit_price"]
        assert trade["commission_usd"] == pytest.approx(0.01 * (trade["notional_usd"] + exit_notional), abs=1e-6)
    assert all(t["commission_usd"] == 0.0 for row in fee_free.values() for t in row["trades"])
    if walk_forward:  # the OOS row and its sealed holdout: what the evidence gate pools
        drop = fee_free["SYNA"]["metrics"]["expectancy_usd"] - stock["metrics"]["expectancy_usd"]
        assert drop == pytest.approx(2.0, abs=0.05)  # $2 a round trip (a fold's later trades size off less cash)
        crypto_drop = fee_free["BTC/USD"]["metrics"]["expectancy_usd"] - crypto["metrics"]["expectancy_usd"]
        assert crypto_drop == pytest.approx(0.02 * crypto["trades"][0]["notional_usd"], rel=0.1)  # ~1% in, ~1% out
        assert stock["metrics"]["holdout_trades"] == fee_free["SYNA"]["metrics"]["holdout_trades"] > 0
        holdout_drop = fee_free["SYNA"]["metrics"]["holdout_expectancy_usd"] - stock["metrics"]["holdout_expectancy_usd"]
        assert holdout_drop == pytest.approx(2.0, abs=0.05)


def test_a_row_records_what_etoro_fees_cost_its_oos_and_holdout_trades(tmp_path, monkeypatch) -> None:
    """Additive row fields (2026-10-24, the learning account's fee band): eToro's fees on the
    row's OOS trades and on its holdout's, so a reader can price the same trades without them."""

    from app.backtesting.cost_model import ETORO_FEES_FIELD, HOLDOUT_ETORO_FEES_FIELD, leg_fees_usd

    monkeypatch.setattr(batch_module, "get_strategy", lambda name, **kwargs: _PatternBuyer())
    paid = _batch_rows(tmp_path, True)
    stock, crypto = paid["SYNA"]["metrics"], paid["BTC/USD"]["metrics"]
    assert stock[ETORO_FEES_FIELD] == pytest.approx(2.0 * stock["number_of_trades"])
    assert stock["holdout_trades"] > 0
    assert stock[HOLDOUT_ETORO_FEES_FIELD] == pytest.approx(2.0 * stock["holdout_trades"])
    legs = sum(t["notional_usd"] + t["quantity"] * t["exit_price"] for t in paid["BTC/USD"]["trades"])
    assert crypto[ETORO_FEES_FIELD] == pytest.approx(0.01 * legs, abs=1e-3)
    assert crypto[HOLDOUT_ETORO_FEES_FIELD] > 0
    # The fee is the commission: adding it back to the pool removes eToro's fees from it.
    commissions = sum(t["commission_usd"] for t in paid["SYNA"]["trades"])
    assert stock[ETORO_FEES_FIELD] == pytest.approx(commissions)
    with monkeypatch.context() as free:
        free.setattr(cost_module, "ETORO_STOCK_FEE_USD_PER_LEG", 0.0)
        free.setattr(cost_module, "ETORO_CRYPTO_FEE_PER_LEG", 0.0)
        fee_free = _batch_rows(tmp_path, True)
    assert fee_free["SYNA"]["metrics"][ETORO_FEES_FIELD] == 0.0
    assert fee_free["SYNA"]["metrics"][HOLDOUT_ETORO_FEES_FIELD] == 0.0
    # The helper prices each leg as the engine does, and nothing without leg fees.
    trade = {"notional_usd": 2_000.0, "quantity": 20.0, "exit_price": 110.0}
    assert leg_fees_usd(with_etoro_trade_fees(CostModel(), crypto=False), [trade, trade]) == 4.0
    assert leg_fees_usd(with_etoro_trade_fees(CostModel(), crypto=True), [trade]) == pytest.approx(42.0)
    assert leg_fees_usd(CostModel(), [trade]) == 0.0
    assert leg_fees_usd(with_etoro_trade_fees(CostModel(), crypto=False), [{"notional_usd": 0.0}]) == 0.0
