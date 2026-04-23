from __future__ import annotations

import pandas as pd
import pytest

from app.models.signal import SignalAction
from app.strategies import ema_trend_stack as ema_trend_stack_module
from app.strategies.ema_trend_stack import EMATrendStackStrategy
from app.strategies.gold_momentum import GoldMomentumStrategy
from app.strategies.ma_crossover import MACrossoverStrategy
from app.strategies.rsi_vwap_ema_confluence import RSIVWAPEMAConfluenceStrategy


def make_frame(prices: list[float]) -> pd.DataFrame:
    timestamps = pd.date_range("2025-01-01", periods=len(prices), tz="UTC", freq="D")
    return pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": prices,
            "high": [price + 1 for price in prices],
            "low": [price - 1 for price in prices],
            "close": prices,
            "volume": [1_000_000 for _ in prices],
        }
    )


def test_ma_crossover_emits_buy_signal() -> None:
    strategy = MACrossoverStrategy(fast_window=3, slow_window=5)
    prices = [10, 10, 10, 10, 9, 9, 10, 12]
    signal = strategy.generate_signal(make_frame(prices), "NVDA")
    assert signal is not None
    assert signal.action == SignalAction.BUY
    assert signal.stop_loss is not None


def test_gold_momentum_emits_buy_signal() -> None:
    strategy = GoldMomentumStrategy(breakout_window=5, trend_window=7)
    prices = [100, 101, 102, 101, 103, 104, 105, 104, 106, 107, 109, 111]
    signal = strategy.generate_signal(make_frame(prices), "GOLD")
    assert signal is not None
    assert signal.action == SignalAction.BUY
    assert signal.price == prices[-1]


def test_rsi_vwap_ema_confluence_captures_near_miss_diagnostics() -> None:
    strategy = RSIVWAPEMAConfluenceStrategy(timeframe="1d")
    prices = [100.0 for _ in range(90)]

    signal = strategy.generate_signal(make_frame(prices), "NVDA")

    assert signal is None
    assert strategy.last_diagnostics is not None
    assert strategy.last_diagnostics["status"] == "no_signal"
    assert strategy.last_diagnostics["score"] is not None
    assert "relative_volume_too_low" in strategy.last_diagnostics["rejection_reasons"]
    assert "adx_too_low" in strategy.last_diagnostics["rejection_reasons"]


def test_rsi_vwap_ema_confluence_timeframe_profile_relaxes_hourly_and_daily() -> None:
    strict = RSIVWAPEMAConfluenceStrategy(timeframe="15m")._threshold_profile()
    hourly = RSIVWAPEMAConfluenceStrategy(timeframe="1h")._threshold_profile()
    daily = RSIVWAPEMAConfluenceStrategy(timeframe="1d")._threshold_profile()

    assert strict["timeframe_profile"] == "strict_intraday"
    assert hourly["minimum_relative_volume"] < strict["minimum_relative_volume"]
    assert daily["minimum_relative_volume"] < strict["minimum_relative_volume"]
    assert hourly["minimum_relative_volume_relaxed"] < hourly["minimum_relative_volume"]
    assert daily["minimum_relative_volume_relaxed"] < daily["minimum_relative_volume"]
    assert hourly["minimum_confluence_score"] < strict["minimum_confluence_score"]
    assert daily["minimum_confluence_score"] < strict["minimum_confluence_score"]
    assert strict["breakout_tolerance_atr"] == 0.0
    assert hourly["breakout_tolerance_atr"] > 0.0
    assert daily["breakout_tolerance_atr"] > hourly["breakout_tolerance_atr"]
    assert strict["session_volume_floor"] == strict["minimum_relative_volume"]
    assert hourly["session_volume_floor"] < hourly["minimum_relative_volume"]
    assert daily["session_volume_floor"] < daily["minimum_relative_volume"]


def test_rsi_vwap_ema_confluence_breakout_ready_allows_daily_near_breakout_only() -> None:
    strict = RSIVWAPEMAConfluenceStrategy(timeframe="15m")._threshold_profile()
    daily = RSIVWAPEMAConfluenceStrategy(timeframe="1d")._threshold_profile()

    strict_ready, strict_gap, strict_confirmed = RSIVWAPEMAConfluenceStrategy._breakout_ready(
        side="long",
        close=100.0,
        trigger=100.2,
        atr=1.0,
        tolerance_atr=strict["breakout_tolerance_atr"],
    )
    daily_ready, daily_gap, daily_confirmed = RSIVWAPEMAConfluenceStrategy._breakout_ready(
        side="long",
        close=100.0,
        trigger=100.2,
        atr=1.0,
        tolerance_atr=daily["breakout_tolerance_atr"],
    )

    assert round(strict_gap, 4) == round(daily_gap, 4) == 0.2
    assert strict_confirmed is False
    assert daily_confirmed is False
    assert strict_ready is False
    assert daily_ready is True


def test_rsi_vwap_ema_confluence_volume_ready_is_session_aware_only_on_slow_timeframes() -> None:
    strict = RSIVWAPEMAConfluenceStrategy(timeframe="15m")._threshold_profile()
    daily = RSIVWAPEMAConfluenceStrategy(timeframe="1d")._threshold_profile()
    volume_context = {"session_volume_ratio": 0.97}

    strict_ready, strict_mode = RSIVWAPEMAConfluenceStrategy._volume_ready(
        rv=1.00,
        breakout_gap_atr=0.10,
        volume_context=volume_context,
        thresholds=strict,
    )
    daily_ready, daily_mode = RSIVWAPEMAConfluenceStrategy._volume_ready(
        rv=1.00,
        breakout_gap_atr=0.10,
        volume_context=volume_context,
        thresholds=daily,
    )
    far_ready, far_mode = RSIVWAPEMAConfluenceStrategy._volume_ready(
        rv=1.00,
        breakout_gap_atr=0.30,
        volume_context=volume_context,
        thresholds=daily,
    )

    assert strict_ready is False
    assert strict_mode == "strict_relative_volume"
    assert daily_ready is True
    assert daily_mode == "session_aware_relaxed"
    assert far_ready is False
    assert far_mode == "strict_relative_volume"


def test_ema_trend_stack_suppresses_signal_when_atr_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    strategy = EMATrendStackStrategy(timeframe="1h")
    frame = make_frame([100 + (index * 0.5) for index in range(140)])

    enriched = frame.copy()
    for column in ["ema_9", "ema_20", "ema_50", "ema_20_slope", "ema_50_slope", "swing_low_10", "swing_high_10"]:
        enriched[column] = enriched["close"]
    enriched["atr_14"] = None

    monkeypatch.setattr(
        ema_trend_stack_module,
        "enrich_technical_indicators",
        lambda data, timeframe="1h": enriched,
    )

    signal = strategy.generate_signal(frame, "NVDA")

    assert signal is None
    assert strategy.last_diagnostics is not None
    assert "atr_unavailable" in strategy.last_diagnostics["rejection_reasons"]
