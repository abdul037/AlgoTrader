from __future__ import annotations

import pandas as pd
import pytest

from app.models.signal import SignalAction
from app.strategies import ema_trend_stack as ema_trend_stack_module
from app.strategies import enhanced as enhanced_module
from app.strategies import vwap_reclaim as vwap_reclaim_module
from app.strategies.ema_trend_stack import EMATrendStackStrategy
from app.strategies.enhanced import (
    AnchoredVWAPPullbackContinuationStrategy,
    ATRDonchianTrendBreakoutStrategy,
    ConfluenceRecoveryBreakoutStrategy,
    EarlyBreakoutPullbackContinuationStrategy,
    EtfMegaCapRelativeStrengthRotationStrategy,
    FailedBreakdownReversalStrategy,
    GapContinuationFadeStrategy,
    InsideBarNarrowRangeBreakoutStrategy,
    LiquidityExpansionContinuationStrategy,
    MultiTimeframeTrendPullbackStrategy,
    OpeningRangeBreakoutRetestStrategy,
    RegimeAlignedTrendContinuationStrategy,
    RegimeFilteredMeanReversionStrategy,
    RelativeStrengthMomentumStrategy,
    RelativeVolumeReclaimContinuationStrategy,
    VolatilityContractionBreakoutStrategy,
)
from app.strategies.gold_momentum import GoldMomentumStrategy
from app.strategies.ma_crossover import MACrossoverStrategy
from app.strategies.momentum_breakout import MomentumBreakoutStrategy
from app.strategies.rsi_vwap_ema_confluence import RSIVWAPEMAConfluenceStrategy
from app.strategies.vwap_reclaim import VWAPReclaimStrategy


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


def _enable_weak_signal(strategy, minimum_rr: float = 1.0):
    strategy._paper_weak_signal_enabled = True
    strategy._paper_weak_signal_min_reward_to_risk = minimum_rr
    return strategy


def _enhanced_frame(rows: int = 100) -> pd.DataFrame:
    timestamps = pd.date_range("2026-01-01 14:30", periods=rows, tz="UTC", freq="h")
    close = [100.0 + (index * 0.05) for index in range(rows)]
    frame = pd.DataFrame(
        {
            "timestamp": timestamps,
            "open": [price - 0.2 for price in close],
            "high": [price + 0.8 for price in close],
            "low": [price - 0.8 for price in close],
            "close": close,
            "volume": [2_000_000 for _ in range(rows)],
        }
    )
    frame["ema_9"] = frame["close"] + 0.6
    frame["ema_20"] = frame["close"] - 1.0
    frame["ema_50"] = frame["close"] - 2.0
    frame["ema_200"] = frame["close"] - 4.0
    frame["ema_9_slope"] = 0.4
    frame["ema_20_slope"] = 0.3
    frame["ema_50_slope"] = 0.2
    frame["vwap"] = frame["close"] - 1.2
    frame["rsi_14"] = 58.0
    frame["stoch_rsi"] = 0.55
    frame["macd_hist"] = 0.4
    frame["bb_width_pct"] = 4.0
    frame["bb_mid"] = frame["close"]
    frame["bb_lower"] = frame["close"] - 3.0
    frame["atr_14"] = 2.0
    frame["atr_pct"] = 1.5
    frame["adx_14"] = 22.0
    frame["relative_volume"] = 1.4
    frame["avg_dollar_volume_20"] = 50_000_000.0
    frame["opening_range_high"] = frame["high"].rolling(5).max()
    frame["opening_range_low"] = frame["low"].rolling(5).min()
    frame["swing_high_10"] = frame["high"].rolling(10).max().shift(1)
    frame["swing_low_10"] = frame["low"].rolling(10).min().shift(1)
    frame["range_high_20"] = frame["high"].rolling(20).max().shift(1)
    frame["range_low_20"] = frame["low"].rolling(20).min().shift(1)
    return frame


@pytest.mark.parametrize(
    ("strategy", "customize"),
    [
        (
            VolatilityContractionBreakoutStrategy(timeframe="1d"),
            lambda frame: frame.assign(
                close=[*frame["close"].iloc[:-1], 120.0],
                high=[*frame["high"].iloc[:-1], 121.0],
                low=[*frame["low"].iloc[:-1], 118.5],
                ema_20=[*frame["ema_20"].iloc[:-1], 116.0],
                ema_50=[*frame["ema_50"].iloc[:-1], 112.0],
                range_high_20=[*frame["range_high_20"].iloc[:-1], 118.0],
                bb_width_pct=[*frame["bb_width_pct"].iloc[:-1], 2.0],
            ),
        ),
        (
            RelativeStrengthMomentumStrategy(timeframe="1d"),
            lambda frame: frame.assign(
                close=[100.0 for _ in range(70)] + [105.0 + index for index in range(30)],
                high=[101.0 for _ in range(70)] + [106.0 + index for index in range(30)],
                low=[99.0 for _ in range(70)] + [104.0 + index for index in range(30)],
                ema_50=[110.0 for _ in range(100)],
                ema_200=[105.0 for _ in range(100)],
            ),
        ),
        (
            ATRDonchianTrendBreakoutStrategy(timeframe="1d"),
            lambda frame: frame.assign(
                close=[*frame["close"].iloc[:-1], 120.0],
                high=[*[110.0 for _ in range(len(frame) - 1)], 121.0],
                low=[*[98.0 for _ in range(len(frame) - 1)], 118.0],
                ema_20=[*frame["ema_20"].iloc[:-1], 116.0],
                ema_50=[*frame["ema_50"].iloc[:-1], 112.0],
            ),
        ),
        (
            AnchoredVWAPPullbackContinuationStrategy(timeframe="15m"),
            lambda frame: frame.assign(
                open=[*frame["open"].iloc[:-1], 104.0],
                close=[*frame["close"].iloc[:-1], 105.0],
                high=[*frame["high"].iloc[:-1], 105.8],
                low=[*frame["low"].iloc[:-2], 102.2, 101.8],
                vwap=[*frame["vwap"].iloc[:-1], 102.0],
                ema_20=[*frame["ema_20"].iloc[:-1], 103.0],
                ema_50=[*frame["ema_50"].iloc[:-1], 100.0],
                relative_volume=[*frame["relative_volume"].iloc[:-1], 1.0],
            ),
        ),
        (
            GapContinuationFadeStrategy(timeframe="15m"),
            lambda frame: frame.assign(
                open=[*frame["open"].iloc[:-1], 101.2],
                close=[*frame["close"].iloc[:-2], 100.0, 102.5],
                high=[*frame["high"].iloc[:-1], 103.0],
                low=[*frame["low"].iloc[:-1], 101.0],
                relative_volume=[*frame["relative_volume"].iloc[:-1], 1.6],
            ),
        ),
        (
            RegimeFilteredMeanReversionStrategy(timeframe="1d"),
            lambda frame: frame.assign(
                open=[*frame["open"].iloc[:-1], 95.5],
                close=[*frame["close"].iloc[:-1], 96.0],
                high=[*frame["high"].iloc[:-1], 97.0],
                low=[*frame["low"].iloc[:-1], 95.0],
                ema_50=[*frame["ema_50"].iloc[:-1], 100.0],
                ema_200=[*frame["ema_200"].iloc[:-1], 100.0],
                rsi_14=[*frame["rsi_14"].iloc[:-1], 31.0],
                stoch_rsi=[*frame["stoch_rsi"].iloc[:-1], 0.2],
                adx_14=[*frame["adx_14"].iloc[:-1], 18.0],
                bb_lower=[*frame["bb_lower"].iloc[:-1], 96.2],
                bb_mid=[*frame["bb_mid"].iloc[:-1], 100.0],
                vwap=[*frame["vwap"].iloc[:-1], 99.0],
            ),
        ),
        (
            OpeningRangeBreakoutRetestStrategy(timeframe="15m"),
            lambda frame: frame.assign(
                open=[*frame["open"].iloc[:-1], 106.0],
                close=[*frame["close"].iloc[:-1], 108.0],
                high=[*frame["high"].iloc[:-1], 109.0],
                low=[*frame["low"].iloc[:-1], 104.0],
                opening_range_high=[*frame["opening_range_high"].iloc[:-1], 106.0],
                opening_range_low=[*frame["opening_range_low"].iloc[:-1], 101.0],
                vwap=[*frame["vwap"].iloc[:-1], 104.0],
                ema_20=[*frame["ema_20"].iloc[:-1], 103.0],
                relative_volume=[*frame["relative_volume"].iloc[:-1], 1.2],
            ),
        ),
        (
            FailedBreakdownReversalStrategy(timeframe="1h"),
            lambda frame: frame.assign(
                open=[*frame["open"].iloc[:-1], 100.0],
                close=[*[100.0 for _ in range(len(frame) - 1)], 104.0],
                high=[*[105.0 for _ in range(len(frame) - 1)], 105.0],
                low=[*[99.0 for _ in range(len(frame) - 1)], 96.0],
                ema_200=[*[98.0 for _ in range(len(frame))]],
                relative_volume=[*frame["relative_volume"].iloc[:-1], 1.1],
            ),
        ),
        (
            MultiTimeframeTrendPullbackStrategy(timeframe="1h"),
            lambda frame: frame.assign(
                open=[*frame["open"].iloc[:-1], 104.0],
                close=[*frame["close"].iloc[:-1], 106.0],
                high=[*frame["high"].iloc[:-1], 107.0],
                low=[*frame["low"].iloc[:-1], 102.5],
                ema_20=[*frame["ema_20"].iloc[:-1], 103.0],
                ema_50=[*frame["ema_50"].iloc[:-1], 100.0],
                ema_200=[*frame["ema_200"].iloc[:-1], 99.0],
                relative_volume=[*frame["relative_volume"].iloc[:-1], 1.0],
            ),
        ),
        (
            InsideBarNarrowRangeBreakoutStrategy(timeframe="15m"),
            lambda frame: frame.assign(
                close=[*frame["close"].iloc[:-2], 101.0, 103.0],
                high=[*frame["high"].iloc[:-3], 104.0, 102.0, 103.5],
                low=[*frame["low"].iloc[:-3], 98.0, 100.0, 102.0],
                relative_volume=[*frame["relative_volume"].iloc[:-1], 1.2],
            ),
        ),
        (
            LiquidityExpansionContinuationStrategy(timeframe="15m"),
            lambda frame: frame.assign(
                open=[*frame["open"].iloc[:-1], 102.0],
                close=[*frame["close"].iloc[:-1], 106.0],
                high=[*frame["high"].iloc[:-1], 106.5],
                low=[*frame["low"].iloc[:-1], 101.5],
                ema_20=[*frame["ema_20"].iloc[:-1], 103.0],
                ema_50=[*frame["ema_50"].iloc[:-1], 100.0],
                relative_volume=[*frame["relative_volume"].iloc[:-1], 1.5],
            ),
        ),
        (
            EtfMegaCapRelativeStrengthRotationStrategy(timeframe="1d"),
            lambda frame: frame.assign(
                close=[100.0 for _ in range(70)] + [104.0 + index for index in range(30)],
                high=[101.0 for _ in range(70)] + [105.0 + index for index in range(30)],
                low=[99.0 for _ in range(70)] + [103.0 + index for index in range(30)],
                ema_50=[120.0 for _ in range(99)] + [112.0],
                ema_200=[110.0 for _ in range(100)],
                relative_volume=[*frame["relative_volume"].iloc[:-1], 1.0],
            ),
        ),
        (
            RelativeVolumeReclaimContinuationStrategy(timeframe="15m"),
            lambda frame: frame.assign(
                open=[*frame["open"].iloc[:-1], 104.0],
                close=[*frame["close"].iloc[:-2], 103.0, 105.0],
                high=[*frame["high"].iloc[:-1], 105.8],
                low=[*frame["low"].iloc[:-1], 102.8],
                vwap=[*frame["vwap"].iloc[:-1], 103.2],
                ema_20=[*frame["ema_20"].iloc[:-1], 103.0],
                ema_50=[*frame["ema_50"].iloc[:-1], 101.0],
                rsi_14=[*frame["rsi_14"].iloc[:-1], 54.0],
                macd_hist=[*frame["macd_hist"].iloc[:-1], 0.05],
                relative_volume=[*frame["relative_volume"].iloc[:-1], 0.95],
            ),
        ),
        (
            EarlyBreakoutPullbackContinuationStrategy(timeframe="15m"),
            lambda frame: frame.assign(
                open=[*frame["open"].iloc[:-1], 104.0],
                close=[*frame["close"].iloc[:-1], 105.0],
                high=[*[105.2 for _ in range(len(frame) - 1)], 105.6],
                low=[*frame["low"].iloc[:-1], 102.8],
                ema_20=[*frame["ema_20"].iloc[:-1], 103.4],
                ema_50=[*frame["ema_50"].iloc[:-1], 101.5],
                rsi_14=[*frame["rsi_14"].iloc[:-1], 54.0],
                relative_volume=[*frame["relative_volume"].iloc[:-1], 0.9],
            ),
        ),
        (
            RegimeAlignedTrendContinuationStrategy(timeframe="1h"),
            lambda frame: frame.assign(
                open=[*frame["open"].iloc[:-1], 104.0],
                close=[100.0 + (index * 0.12) for index in range(len(frame) - 1)] + [105.0],
                high=[*frame["high"].iloc[:-1], 105.7],
                low=[*frame["low"].iloc[:-1], 103.5],
                ema_9=[*frame["ema_9"].iloc[:-1], 104.7],
                ema_20=[*frame["ema_20"].iloc[:-1], 103.7],
                ema_50=[*frame["ema_50"].iloc[:-1], 101.5],
                ema_200=[*frame["ema_200"].iloc[:-1], 100.0],
                rsi_14=[*frame["rsi_14"].iloc[:-1], 55.0],
                adx_14=[*frame["adx_14"].iloc[:-1], 16.0],
                macd_hist=[*frame["macd_hist"].iloc[:-1], 0.08],
                relative_volume=[*frame["relative_volume"].iloc[:-1], 0.85],
            ),
        ),
        (
            ConfluenceRecoveryBreakoutStrategy(timeframe="1h"),
            lambda frame: frame.assign(
                open=[*frame["open"].iloc[:-1], 104.0],
                close=[*frame["close"].iloc[:-2], 103.8, 105.0],
                high=[*[105.1 for _ in range(len(frame) - 1)], 105.8],
                low=[*frame["low"].iloc[:-1], 103.0],
                ema_20=[*frame["ema_20"].iloc[:-1], 103.8],
                ema_50=[*frame["ema_50"].iloc[:-1], 101.5],
                vwap=[*frame["vwap"].iloc[:-1], 103.5],
                rsi_14=[*frame["rsi_14"].iloc[:-1], 56.0],
                macd_hist=[*frame["macd_hist"].iloc[:-1], 0.1],
                bb_width_pct=[*frame["bb_width_pct"].iloc[:-1], 3.5],
                relative_volume=[*frame["relative_volume"].iloc[:-1], 0.95],
            ),
        ),
    ],
)
def test_enhanced_research_strategies_emit_valid_long_only_trade_plans(
    monkeypatch: pytest.MonkeyPatch,
    strategy,
    customize,
) -> None:
    enriched = customize(_enhanced_frame())
    monkeypatch.setattr(enhanced_module, "enrich_technical_indicators", lambda data, timeframe: enriched)

    signal = strategy.generate_signal(enriched, "NVDA")

    assert signal is not None
    assert signal.action == SignalAction.BUY
    assert signal.stop_loss is not None and signal.price is not None and signal.take_profit is not None
    assert signal.stop_loss < signal.price < signal.take_profit
    assert signal.metadata["pack"] == "enhanced_research"
    assert signal.metadata["asset_class"] == "us_equity"
    assert signal.metadata["live_enabled"] is False
    assert signal.metadata["signal_role"] == "entry_long"


def test_enhanced_research_strategy_records_near_miss_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    strategy = VolatilityContractionBreakoutStrategy(timeframe="1d")
    enriched = _enhanced_frame().assign(relative_volume=0.2)
    monkeypatch.setattr(enhanced_module, "enrich_technical_indicators", lambda data, timeframe: enriched)

    signal = strategy.generate_signal(enriched, "NVDA")

    assert signal is None
    assert strategy.last_diagnostics["status"] == "no_signal"
    assert "relative_volume_too_low" in strategy.last_diagnostics["rejection_reasons"]
    assert "measurements" in strategy.last_diagnostics


def test_enhanced_research_strategy_emits_supervised_weak_valid_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy = _enable_weak_signal(RelativeVolumeReclaimContinuationStrategy(timeframe="15m"))
    enriched = _enhanced_frame().assign(
        open=[*_enhanced_frame()["open"].iloc[:-1], 104.0],
        close=[*_enhanced_frame()["close"].iloc[:-2], 103.0, 105.0],
        high=[*_enhanced_frame()["high"].iloc[:-1], 105.8],
        low=[*_enhanced_frame()["low"].iloc[:-1], 102.8],
        vwap=[*_enhanced_frame()["vwap"].iloc[:-1], 103.2],
        ema_20=[*_enhanced_frame()["ema_20"].iloc[:-1], 103.0],
        ema_50=[*_enhanced_frame()["ema_50"].iloc[:-1], 101.0],
        rsi_14=[*_enhanced_frame()["rsi_14"].iloc[:-1], 54.0],
        macd_hist=[*_enhanced_frame()["macd_hist"].iloc[:-1], 0.05],
        relative_volume=[*_enhanced_frame()["relative_volume"].iloc[:-1], 0.35],
    )
    monkeypatch.setattr(enhanced_module, "enrich_technical_indicators", lambda data, timeframe: enriched)

    signal = strategy.generate_signal(enriched, "NVDA")

    assert signal is not None
    assert signal.action == SignalAction.BUY
    assert signal.stop_loss < signal.price < signal.take_profit
    assert signal.metadata["signal_classification"] == "supervised_weak_valid"
    assert signal.metadata["source"] == "supervised_weak_valid"
    assert signal.metadata["production_qualified"] is False
    assert "relative_volume_too_low" in signal.metadata["weak_signal_reasons"]


def test_enhanced_research_strategy_does_not_emit_weak_signal_without_setup_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy = _enable_weak_signal(RelativeVolumeReclaimContinuationStrategy(timeframe="15m"))
    base = _enhanced_frame()
    enriched = base.assign(
        open=[*base["open"].iloc[:-1], 103.0],
        close=[*base["close"].iloc[:-2], 103.0, 102.0],
        low=[*base["low"].iloc[:-1], 101.5],
        vwap=[*base["vwap"].iloc[:-1], 103.2],
        ema_20=[*base["ema_20"].iloc[:-1], 103.0],
        ema_50=[*base["ema_50"].iloc[:-1], 101.0],
        relative_volume=[*base["relative_volume"].iloc[:-1], 0.35],
    )
    monkeypatch.setattr(enhanced_module, "enrich_technical_indicators", lambda data, timeframe: enriched)

    signal = strategy.generate_signal(enriched, "NVDA")

    assert signal is None
    assert strategy.last_diagnostics["status"] == "no_signal"
    assert "reclaim_not_confirmed" in strategy.last_diagnostics["rejection_reasons"]


def test_momentum_breakout_emits_supervised_weak_valid_signal_on_real_breakout_anchor() -> None:
    prices = [100.0 + (index * 0.2) for index in range(29)] + [108.0]
    frame = make_frame(prices)
    frame["volume"] = [1_000_000 for _ in range(len(frame) - 1)] + [600_000]
    strategy = _enable_weak_signal(MomentumBreakoutStrategy(breakout_window=20, volume_window=20))

    signal = strategy.generate_signal(frame, "NVDA")

    assert signal is not None
    assert signal.action == SignalAction.BUY
    assert signal.stop_loss < signal.price < signal.take_profit
    assert signal.metadata["signal_classification"] == "supervised_weak_valid"
    assert signal.metadata["source"] == "supervised_weak_valid"
    assert "relative_volume_too_low" in signal.metadata["weak_signal_reasons"]


def test_vwap_reclaim_emits_supervised_weak_valid_signal_on_real_reclaim_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy = _enable_weak_signal(VWAPReclaimStrategy(timeframe="5m"))
    base = _enhanced_frame(rows=60)
    enriched = base.assign(
        close=[*base["close"].iloc[:-1], 105.0],
        low=[*base["low"].iloc[:-1], 102.8],
        vwap=[*base["vwap"].iloc[:-1], 103.8],
        ema_9=[*base["ema_9"].iloc[:-1], 104.8],
        ema_20=[*base["ema_20"].iloc[:-1], 104.0],
        macd_hist=[*base["macd_hist"].iloc[:-1], -0.05],
        relative_volume=[*base["relative_volume"].iloc[:-1], 0.40],
    )
    monkeypatch.setattr(vwap_reclaim_module, "enrich_technical_indicators", lambda data, timeframe="5m": enriched)

    signal = strategy.generate_signal(enriched, "NVDA")

    assert signal is not None
    assert signal.action == SignalAction.BUY
    assert signal.stop_loss < signal.price < signal.take_profit
    assert signal.metadata["signal_classification"] == "supervised_weak_valid"
    assert signal.metadata["source"] == "supervised_weak_valid"
    assert set(signal.metadata["weak_signal_reasons"]) == {"relative_volume_too_low", "confirmation_too_weak"}


def test_ema_trend_stack_emits_supervised_weak_valid_signal_on_real_pullback_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    strategy = _enable_weak_signal(EMATrendStackStrategy(timeframe="1h"))
    enriched = _enhanced_frame(rows=140)
    monkeypatch.setattr(
        ema_trend_stack_module,
        "enrich_technical_indicators",
        lambda data, timeframe="1h": enriched,
    )

    signal = strategy.generate_signal(enriched, "NVDA")

    assert signal is not None
    assert signal.action == SignalAction.BUY
    assert signal.stop_loss < signal.price < signal.take_profit
    assert signal.metadata["signal_classification"] == "supervised_weak_valid"
    assert signal.metadata["source"] == "supervised_weak_valid"
    assert "confirmation_too_weak" in signal.metadata["weak_signal_reasons"]
