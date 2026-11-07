"""Walk-forward folds must give strategies indicator warm-up.

Root cause of the empty backtest gate (found 2026-09-08): every fold ran the
engine on its ~10-bar test slice alone, below every strategy's indicator
warm-up, so 1.2M fold runs recorded zero trades. Folds now feed the train bars
as context and evaluate only the test window.

Second shortfall (found 2026-11-07): a 180-day daily train window holds ~124
bars, below the 210 that ``connors_rsi2_reversion`` and ``overnight_drift``
require, so both still recorded 0 OOS trades in production while the live scan
(400 daily bars) emitted them. A daily strategy needing more bars than the train
window now gets every earlier bar of the fetched history as warm-up
(``DEEP_WARMUP_TIMEFRAMES``); the others keep exactly the train window.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from app.backtesting import batch as batch_module
from app.backtesting.batch import BatchBacktestService, _deep_warmup_bars, _with_warmup
from app.backtesting.engine import BacktestEngine, EngineConfig
from app.backtesting.intraday_plan import splitter_for
from app.backtesting.walk_forward import WalkForwardSplitter
from app.strategies import get_strategy, get_strategy_specs
from tests.conftest import make_settings

DEEP = ("connors_rsi2_reversion", "overnight_drift")  # required_bars 210 > the ~124-bar train window


def _trending_history(days: int = 420) -> pd.DataFrame:
    """A noisy up-trend with periodic pullbacks so MA crossovers actually occur."""

    rng = np.random.default_rng(7)
    start = datetime(2025, 1, 2, tzinfo=timezone.utc)
    rows = []
    price = 100.0
    for i in range(days):
        drift = 0.15 if (i // 25) % 2 == 0 else -0.10
        price = max(1.0, price + drift + rng.normal(0.0, 0.8))
        rows.append(
            {
                "timestamp": start + timedelta(days=i),
                "open": price,
                "high": price + 0.6,
                "low": price - 0.6,
                "close": price,
                "volume": 1_000_000,
            }
        )
    return pd.DataFrame(rows)


def _engine() -> BacktestEngine:
    return BacktestEngine(config=EngineConfig(initial_cash=10_000.0))


def test_fold_without_warmup_cannot_trade_but_with_warmup_can() -> None:
    history = _trending_history()
    strategy = get_strategy("ma_crossover")
    splitter = WalkForwardSplitter(train_days=180, test_days=14, step_days=14, embargo_days=1, holdout_days=28)
    folds = list(splitter.split(history))
    assert folds, "fixture must yield folds"

    bare_trades = 0
    warm_trades = 0
    for window in folds:
        bare = _engine().run(symbol="SYN", strategy=strategy, data=window.test_df, file_path="bare")
        warm = _engine().run(
            symbol="SYN",
            strategy=strategy,
            data=_with_warmup(window),
            file_path="warm",
            trade_window_start=window.test_start,
        )
        bare_trades += int(bare.metrics["number_of_trades"])
        warm_trades += int(warm.metrics["number_of_trades"])
        # Evaluation stays inside the test window: no entry before test_start,
        # and the warm-up bars add no equity points (same bar count as the bare
        # run, which sees exactly the test bars).
        for trade in warm.trades:
            assert pd.Timestamp(trade["entry_time"]) >= pd.Timestamp(window.test_start)
        assert warm.metrics["bars_evaluated"] <= float(len(window.test_df))
        assert warm.metrics["bars_evaluated"] >= bare.metrics["bars_evaluated"] - 1.0

    assert bare_trades == 0  # the old behaviour: ~10 bars, below MA warm-up
    assert warm_trades > 0  # the fix: same folds now produce trades


def test_trade_window_start_excludes_warmup_from_equity_and_metrics() -> None:
    history = _trending_history(120)
    strategy = get_strategy("ma_crossover")
    cutoff = history["timestamp"].iloc[80]

    result = _engine().run(
        symbol="SYN", strategy=strategy, data=history, file_path="x", trade_window_start=cutoff
    )

    assert result.metrics["bars_evaluated"] == 40.0
    assert all(pd.Timestamp(t["entry_time"]) >= pd.Timestamp(cutoff) for t in result.trades)


def test_with_warmup_falls_back_to_test_frame_without_train() -> None:
    history = _trending_history(30)
    window = type("W", (), {"train_df": history.iloc[:0], "test_df": history})()

    assert len(_with_warmup(window)) == 30


# --- The deep daily warm-up (2026-11-07) -------------------------------------------------


def _daily_bars(seed: int = 3, n: int = 500) -> pd.DataFrame:
    """Session-stamped business days on a noisy up-drift: price spends most of the run above
    its 200-EMA, with sharp 2-day dips (RSI(2) oversold) and firm up-closes along the way."""

    rng = np.random.RandomState(seed)
    rets = rng.normal(0.0009, 0.016, n)
    close = 100 * np.exp(np.cumsum(rets))
    openp = close * (1 + rng.normal(0, 0.004, n))
    high = np.maximum(openp, close) * (1 + np.abs(rng.normal(0, 0.006, n)))
    low = np.minimum(openp, close) * (1 - np.abs(rng.normal(0, 0.006, n)))
    stamps = pd.bdate_range("2024-01-02", periods=n, tz="UTC") + pd.Timedelta(hours=4)
    volume = rng.randint(2_000_000, 9_000_000, n).astype(float)
    return pd.DataFrame(
        {"timestamp": stamps, "open": openp, "high": high, "low": low, "close": close, "volume": volume}
    )


class _Bars:
    def __init__(self, frame: pd.DataFrame):
        self.frame = frame

    def get_history(self, symbol, *, timeframe, bars=None, provider=None, force_refresh=False, backtest=False):
        return self.frame.copy()


class _Rows:
    def __init__(self):
        self.rows: list[dict] = []

    def create(self, **kwargs):
        self.rows.append(kwargs)


class _Logs:
    def log(self, event, payload):
        return None


def _service(tmp_path, frame: pd.DataFrame) -> tuple[BatchBacktestService, _Rows]:
    rows = _Rows()
    service = BatchBacktestService(
        settings=make_settings(tmp_path, walk_forward_intraday_variants=[]),
        market_data_engine=_Bars(frame),
        backtest_repository=rows,
        run_log_repository=_Logs(),
    )
    return service, rows


@pytest.mark.parametrize("name", DEEP)
def test_daily_strategies_needing_210_bars_now_trade_out_of_sample(tmp_path, monkeypatch, name) -> None:
    """The production symptom: 0 OOS trades with the train-window warm-up, trades with the deep
    one, every entry inside a fold's test window (never in the warm-up, the embargo or later).
    The mechanism for both; overnight_drift is held back from it as deployed (next test)."""

    monkeypatch.setattr(batch_module, "DEEP_WARMUP_HELD_BACK", frozenset())
    frame = _daily_bars()
    service, rows = _service(tmp_path, frame)
    service.run(symbols=["SYN"], timeframes=["1d"], strategy_names=[name], walk_forward=True)
    (row,) = rows.rows
    assert row["file_path"].endswith(":1d:SYN:walk_forward_oos")
    assert row["metrics"]["number_of_trades"] == len(row["trades"]) > 0
    assert row["metrics"]["fold_count"] > 0

    windows = list(splitter_for("1d", service.settings).split(frame))
    for trade in row["trades"]:
        entry = pd.Timestamp(trade["entry_time"])
        assert any(w.test_start <= entry <= w.test_end for w in windows), entry

    monkeypatch.setattr(batch_module, "DEEP_WARMUP_TIMEFRAMES", frozenset())  # the old warm-up
    shallow, shallow_rows = _service(tmp_path, frame)
    shallow.run(symbols=["SYN"], timeframes=["1d"], strategy_names=[name], walk_forward=True)
    assert shallow_rows.rows[0]["metrics"]["number_of_trades"] == 0  # what production recorded


class _Recorder:
    """Emits nothing; records (last bar time, bars seen) at every bar the engine evaluates."""

    name = "recorder"

    def __init__(self, required_bars: int):
        self.required_bars = required_bars
        self.calls: list[tuple[pd.Timestamp, int]] = []

    def generate_signal(self, data, symbol):
        self.calls.append((pd.Timestamp(data["timestamp"].iloc[-1]), len(data)))
        return None


def test_folds_and_holdout_see_every_earlier_bar_and_nothing_later(tmp_path, monkeypatch) -> None:
    frame = _daily_bars()
    service, _ = _service(tmp_path, frame)
    splitter = splitter_for("1d", service.settings)
    windows = list(splitter.split(frame))
    stamps = pd.to_datetime(frame["timestamp"], utc=True)
    built: list = []

    def spy(window, history=None, required_bars=0):
        fold_frame = _with_warmup(window, history, required_bars)
        built.append((window, fold_frame))
        return fold_frame

    monkeypatch.setattr(batch_module, "_with_warmup", spy)
    recorder = _Recorder(required_bars=210)
    service._run_strategy(
        engine=BacktestEngine(config=EngineConfig()), symbol="SYN", strategy=recorder,
        history=frame.copy(), timeframe="1d", provider=None, walk_forward=True, persist=False,
    )
    # Every fold, then the sealed holdout, gets every bar before its train end, then its test bars.
    assert [w.test_start for w, _ in built] == [w.test_start for w in windows] + [
        splitter.holdout_window(frame).test_start
    ]
    for window, fold_frame in built:
        before = int((stamps < window.train_end).sum())
        assert len(fold_frame) == before + len(window.test_df)
        assert fold_frame["timestamp"].max() == window.test_df["timestamp"].max()
    assert len(built[-1][1]) >= 210  # the holdout too
    # So the strategy has its 210 bars at every evaluated bar with that much history behind it.
    position = {stamp: index for index, stamp in enumerate(stamps)}
    late = [seen for stamp, seen in recorder.calls if position[stamp] >= 212]
    assert late and min(late) >= 210


def test_a_strategy_the_train_window_covers_keeps_exactly_the_train_window() -> None:
    frame = _daily_bars()
    window = next(iter(WalkForwardSplitter().split(frame)))
    plain = _with_warmup(window)
    assert len(window.train_df) >= 120
    for required in (0, 25, 120, len(window.train_df)):
        pd.testing.assert_frame_equal(_with_warmup(window, frame, required), plain)
    deep = _with_warmup(window, frame, len(window.train_df) + 1)
    stamps = pd.to_datetime(frame["timestamp"], utc=True)
    assert len(deep) == int((stamps < window.train_end).sum()) + len(window.test_df)
    # Nothing between the train end and the test start (the embargo) or after the test window.
    embargo = deep["timestamp"][(deep["timestamp"] >= window.train_end) & (deep["timestamp"] < window.test_start)]
    assert embargo.empty and deep["timestamp"].max() == window.test_df["timestamp"].max()


def test_as_deployed_connors_trades_out_of_sample_and_overnight_drift_is_held_back(tmp_path) -> None:
    """overnight_drift's evidence is a one-bar hold that paper (and the eToro copy after it)
    does not make, so it stays on the train-window warm-up: 0 OOS trades, never admitted."""

    assert "overnight_drift" in batch_module.DEEP_WARMUP_HELD_BACK
    assert _deep_warmup_bars("1d", get_strategy("overnight_drift")) == 0
    frame = _daily_bars()
    service, rows = _service(tmp_path, frame)
    service.run(symbols=["SYN"], timeframes=["1d"], strategy_names=list(DEEP), walk_forward=True)
    trades = {row["strategy_name"]: row["metrics"]["number_of_trades"] for row in rows.rows}
    assert trades["connors_rsi2_reversion"] > 0
    assert trades["overnight_drift"] == 0


def test_deep_warmup_is_daily_only() -> None:
    connors = get_strategy("connors_rsi2_reversion")
    assert _deep_warmup_bars("1d", connors) == 210
    for timeframe in ("1h", "1w", "15m", "10m", "5m", "1m"):
        assert _deep_warmup_bars(timeframe, connors) == 0, timeframe


def test_only_connors_and_overnight_need_more_than_a_daily_train_window() -> None:
    """Why no other daily result moves: every other daily strategy needs at most 120 bars, and
    a 180-day window always holds more NYSE sessions than that (~124). A new daily strategy
    above it gets the deep warm-up too: name it here and in the parity test's list."""

    needs = {
        spec.name: get_strategy(spec.name, **spec.default_kwargs).required_bars
        for spec in get_strategy_specs(timeframe="1d")
    }
    assert {name for name, bars in needs.items() if bars > 120} == set(DEEP), needs
