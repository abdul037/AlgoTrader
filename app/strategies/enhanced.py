"""Enhanced long-only research strategies for Alpaca Paper exploration.

These strategies are deliberately constrained: they emit BUY signals only,
attach validated stop/target plans, and carry metadata that keeps them in the
paper-research lane until governance promotes them.
"""

from __future__ import annotations

from math import isfinite
from typing import Any

import pandas as pd

from app.indicators import compute_confluence_score, enrich_technical_indicators, indicator_summary
from app.models.signal import Signal, SignalAction
from app.strategies.base import BaseStrategy


def _safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if not isfinite(result) or pd.isna(result):
        return default
    return result


def _round_price(value: float) -> float:
    return round(max(value, 0.01), 4)


def _recent_low(frame: pd.DataFrame, window: int) -> float | None:
    if frame.empty:
        return None
    return _safe_float(frame["low"].tail(max(window, 1)).min())


def _recent_high(frame: pd.DataFrame, window: int) -> float | None:
    if frame.empty:
        return None
    return _safe_float(frame["high"].tail(max(window, 1)).max())


def _liquidity_ok(row: pd.Series, minimum_dollar_volume: float) -> bool:
    dollar_volume = _safe_float(row.get("avg_dollar_volume_20"), 0.0) or 0.0
    return dollar_volume >= minimum_dollar_volume


def _metadata(
    *,
    row: pd.Series,
    style: str,
    setup_type: str,
    risk_reward: float,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "pack": "enhanced_research",
        "asset_class": "us_equity",
        "paper_stage": "research",
        "live_enabled": False,
        "signal_role": "entry_long",
        "style": style,
        "setup_type": setup_type,
        "risk_reward_ratio": round(risk_reward, 4),
        "indicator_confluence_score": compute_confluence_score(row),
        "indicator_summary": indicator_summary(row),
    }
    if extra:
        payload.update(extra)
    return payload


def _long_signal(
    strategy: BaseStrategy,
    *,
    symbol: str,
    price: float,
    stop: float,
    risk_multiple: float,
    rationale: str,
    confidence: float,
    metadata: dict[str, Any],
) -> Signal | None:
    if stop >= price:
        return None
    risk = price - stop
    if risk <= max(price * 0.0005, 0.01):
        return None
    target = price + (risk * risk_multiple)
    return strategy._build_signal(
        symbol=symbol.upper(),
        strategy_name=strategy.name,
        action=SignalAction.BUY,
        rationale=rationale,
        confidence=max(0.0, min(confidence, 1.0)),
        price=_round_price(price),
        stop_loss=_round_price(stop),
        take_profit=_round_price(target),
        metadata=metadata,
    )


class VolatilityContractionBreakoutStrategy(BaseStrategy):
    """Break out from a low-volatility range with trend and volume confirmation."""

    name = "volatility_contraction_breakout"
    required_bars = 60

    def __init__(
        self,
        *,
        timeframe: str = "1d",
        contraction_window: int = 20,
        breakout_window: int = 20,
        minimum_relative_volume: float = 1.05,
        risk_multiple: float = 2.5,
        minimum_dollar_volume: float = 2_000_000.0,
    ):
        self.timeframe = timeframe
        self.contraction_window = contraction_window
        self.breakout_window = breakout_window
        self.minimum_relative_volume = minimum_relative_volume
        self.risk_multiple = risk_multiple
        self.minimum_dollar_volume = minimum_dollar_volume

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> Signal | None:
        if not self._ensure_length(data):
            return None
        frame = enrich_technical_indicators(data, timeframe=self.timeframe)
        last = frame.iloc[-1]
        prior = frame.iloc[:-1]
        price = _safe_float(last.get("close"))
        atr = _safe_float(last.get("atr_14"))
        range_high = _safe_float(last.get("range_high_20"), _recent_high(prior, self.breakout_window))
        width = _safe_float(last.get("bb_width_pct"))
        width_median = _safe_float(frame["bb_width_pct"].tail(self.contraction_window + 1).iloc[:-1].median())
        if price is None or atr is None or range_high is None or width is None or width_median is None:
            return None
        trend_ok = price > (_safe_float(last.get("ema_20"), price) or price) > (_safe_float(last.get("ema_50"), 0.0) or 0.0)
        volume_ok = (_safe_float(last.get("relative_volume"), 0.0) or 0.0) >= self.minimum_relative_volume
        contraction_ok = width <= width_median * 0.9
        breakout_ok = price > range_high
        if not (trend_ok and volume_ok and contraction_ok and breakout_ok and _liquidity_ok(last, self.minimum_dollar_volume)):
            return None
        stop = min(_recent_low(frame, 10) or price - atr, price - (1.35 * atr))
        return _long_signal(
            self,
            symbol=symbol,
            price=price,
            stop=stop,
            risk_multiple=self.risk_multiple,
            rationale="Volatility contraction resolved higher with trend and volume confirmation.",
            confidence=0.68,
            metadata=_metadata(
                row=last,
                style="breakout",
                setup_type="volatility_contraction_breakout",
                risk_reward=self.risk_multiple,
            ),
        )


class RelativeStrengthMomentumStrategy(BaseStrategy):
    """Momentum continuation using a symbol-relative strength proxy and regime filter."""

    name = "relative_strength_momentum"
    required_bars = 80

    def __init__(
        self,
        *,
        timeframe: str = "1d",
        roc_window: int = 20,
        baseline_window: int = 60,
        minimum_relative_volume: float = 1.0,
        risk_multiple: float = 2.4,
    ):
        self.timeframe = timeframe
        self.roc_window = roc_window
        self.baseline_window = baseline_window
        self.minimum_relative_volume = minimum_relative_volume
        self.risk_multiple = risk_multiple

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> Signal | None:
        if not self._ensure_length(data):
            return None
        frame = enrich_technical_indicators(data, timeframe=self.timeframe)
        close = frame["close"].astype("float64")
        roc = close.pct_change(self.roc_window)
        baseline = roc.rolling(self.baseline_window).median()
        last = frame.iloc[-1]
        price = _safe_float(last.get("close"))
        atr = _safe_float(last.get("atr_14"))
        latest_roc = _safe_float(roc.iloc[-1])
        baseline_roc = _safe_float(baseline.iloc[-1], 0.0) or 0.0
        if price is None or atr is None or latest_roc is None:
            return None
        ema_50 = _safe_float(last.get("ema_50"), price) or price
        ema_200 = _safe_float(last.get("ema_200"), ema_50) or ema_50
        regime_ok = price > ema_50 and ema_50 >= ema_200 * 0.99
        rs_ok = latest_roc > max(0.015, baseline_roc + 0.01)
        volume_ok = (_safe_float(last.get("relative_volume"), 0.0) or 0.0) >= self.minimum_relative_volume
        if not (regime_ok and rs_ok and volume_ok):
            return None
        stop = max(_recent_low(frame, 12) or price - atr, price - (2.0 * atr))
        return _long_signal(
            self,
            symbol=symbol,
            price=price,
            stop=stop,
            risk_multiple=self.risk_multiple,
            rationale="Relative-strength momentum is leading its rolling baseline inside a constructive regime.",
            confidence=0.66,
            metadata=_metadata(
                row=last,
                style="momentum",
                setup_type="relative_strength_momentum",
                risk_reward=self.risk_multiple,
                extra={
                    "relative_strength_proxy": "symbol_roc_vs_rolling_baseline",
                    "roc_window": self.roc_window,
                    "baseline_window": self.baseline_window,
                    "latest_roc": round(latest_roc, 4),
                    "baseline_roc": round(baseline_roc, 4),
                },
            ),
        )


class ATRDonchianTrendBreakoutStrategy(BaseStrategy):
    """ATR-normalized Donchian breakout with trend filter."""

    name = "atr_donchian_trend_breakout"
    required_bars = 70

    def __init__(
        self,
        *,
        timeframe: str = "1d",
        channel_window: int = 20,
        minimum_adx: float = 16.0,
        risk_multiple: float = 3.0,
    ):
        self.timeframe = timeframe
        self.channel_window = channel_window
        self.minimum_adx = minimum_adx
        self.risk_multiple = risk_multiple

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> Signal | None:
        if not self._ensure_length(data):
            return None
        frame = enrich_technical_indicators(data, timeframe=self.timeframe)
        high = frame["high"].astype("float64")
        low = frame["low"].astype("float64")
        donchian_high = high.rolling(self.channel_window).max().shift(1)
        donchian_low = low.rolling(self.channel_window).min().shift(1)
        last = frame.iloc[-1]
        price = _safe_float(last.get("close"))
        atr = _safe_float(last.get("atr_14"))
        channel_high = _safe_float(donchian_high.iloc[-1])
        channel_low = _safe_float(donchian_low.iloc[-1])
        if price is None or atr is None or channel_high is None or channel_low is None:
            return None
        trend_ok = price > (_safe_float(last.get("ema_20"), price) or price) > (_safe_float(last.get("ema_50"), 0.0) or 0.0)
        adx_ok = (_safe_float(last.get("adx_14"), 0.0) or 0.0) >= self.minimum_adx
        atr_ok = 0.25 <= (_safe_float(last.get("atr_pct"), 0.0) or 0.0) <= 8.0
        if not (price > channel_high and trend_ok and adx_ok and atr_ok):
            return None
        stop = max(channel_low, price - (2.2 * atr))
        return _long_signal(
            self,
            symbol=symbol,
            price=price,
            stop=stop,
            risk_multiple=self.risk_multiple,
            rationale="ATR-normalized Donchian breakout aligned with the prevailing trend.",
            confidence=0.67,
            metadata=_metadata(
                row=last,
                style="trend_breakout",
                setup_type="atr_donchian_trend_breakout",
                risk_reward=self.risk_multiple,
                extra={"channel_window": self.channel_window},
            ),
        )


class AnchoredVWAPPullbackContinuationStrategy(BaseStrategy):
    """Continuation entry after a pullback into VWAP/EMA support."""

    name = "anchored_vwap_pullback_continuation"
    required_bars = 55

    def __init__(
        self,
        *,
        timeframe: str = "15m",
        pullback_tolerance_atr: float = 0.35,
        risk_multiple: float = 2.2,
        minimum_relative_volume: float = 0.85,
    ):
        self.timeframe = timeframe
        self.pullback_tolerance_atr = pullback_tolerance_atr
        self.risk_multiple = risk_multiple
        self.minimum_relative_volume = minimum_relative_volume

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> Signal | None:
        if not self._ensure_length(data):
            return None
        frame = enrich_technical_indicators(data, timeframe=self.timeframe)
        last = frame.iloc[-1]
        prev = frame.iloc[-2]
        price = _safe_float(last.get("close"))
        atr = _safe_float(last.get("atr_14"))
        vwap = _safe_float(last.get("vwap"))
        if price is None or atr is None or vwap is None:
            return None
        ema_20 = _safe_float(last.get("ema_20"), price) or price
        ema_50 = _safe_float(last.get("ema_50"), ema_20) or ema_20
        tolerance = atr * self.pullback_tolerance_atr
        pulled_back = min(_safe_float(prev.get("low"), price) or price, _safe_float(last.get("low"), price) or price) <= max(vwap, ema_20) + tolerance
        reclaimed = price > max(vwap, ema_20) and price > (_safe_float(last.get("open"), price) or price)
        volume_ok = (_safe_float(last.get("relative_volume"), 0.0) or 0.0) >= self.minimum_relative_volume
        if not (price > ema_50 and ema_20 >= ema_50 and pulled_back and reclaimed and volume_ok):
            return None
        stop = min(_recent_low(frame, 8) or price - atr, vwap - (0.6 * atr))
        return _long_signal(
            self,
            symbol=symbol,
            price=price,
            stop=stop,
            risk_multiple=self.risk_multiple,
            rationale="Trend pullback reclaimed anchored VWAP support with continuation confirmation.",
            confidence=0.65,
            metadata=_metadata(
                row=last,
                style="pullback_continuation",
                setup_type="anchored_vwap_pullback_continuation",
                risk_reward=self.risk_multiple,
                extra={"vwap_anchor": "session_or_cumulative"},
            ),
        )


class GapContinuationFadeStrategy(BaseStrategy):
    """Long-only gap continuation or gap-fade setup with liquidity guardrails."""

    name = "gap_continuation_fade"
    required_bars = 35

    def __init__(
        self,
        *,
        timeframe: str = "15m",
        minimum_gap_pct: float = 0.8,
        minimum_relative_volume: float = 1.25,
        minimum_dollar_volume: float = 5_000_000.0,
        risk_multiple: float = 2.0,
    ):
        self.timeframe = timeframe
        self.minimum_gap_pct = minimum_gap_pct
        self.minimum_relative_volume = minimum_relative_volume
        self.minimum_dollar_volume = minimum_dollar_volume
        self.risk_multiple = risk_multiple

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> Signal | None:
        if not self._ensure_length(data):
            return None
        frame = enrich_technical_indicators(data, timeframe=self.timeframe)
        last = frame.iloc[-1]
        prev = frame.iloc[-2]
        price = _safe_float(last.get("close"))
        open_price = _safe_float(last.get("open"))
        prev_close = _safe_float(prev.get("close"))
        atr = _safe_float(last.get("atr_14"))
        if price is None or open_price is None or prev_close is None or atr is None or prev_close <= 0:
            return None
        gap_pct = ((open_price - prev_close) / prev_close) * 100.0
        day_range = max((_safe_float(last.get("high"), price) or price) - (_safe_float(last.get("low"), price) or price), 0.01)
        close_location = (price - (_safe_float(last.get("low"), price) or price)) / day_range
        volume_ok = (_safe_float(last.get("relative_volume"), 0.0) or 0.0) >= self.minimum_relative_volume
        liquidity_ok = _liquidity_ok(last, self.minimum_dollar_volume)
        continuation = gap_pct >= self.minimum_gap_pct and price > open_price and close_location >= 0.65
        fade = gap_pct <= -self.minimum_gap_pct and price > open_price and close_location >= 0.75
        if not (volume_ok and liquidity_ok and (continuation or fade)):
            return None
        setup = "gap_up_continuation" if continuation else "gap_down_fade"
        stop = min(_safe_float(last.get("low"), price - atr) or price - atr, price - (1.2 * atr))
        return _long_signal(
            self,
            symbol=symbol,
            price=price,
            stop=stop,
            risk_multiple=self.risk_multiple,
            rationale=f"{setup.replace('_', ' ').title()} confirmed by relative volume and close location.",
            confidence=0.62,
            metadata=_metadata(
                row=last,
                style="gap",
                setup_type=setup,
                risk_reward=self.risk_multiple,
                extra={"gap_pct": round(gap_pct, 4), "close_location": round(close_location, 4)},
            ),
        )


class RegimeFilteredMeanReversionStrategy(BaseStrategy):
    """Long mean reversion only when the higher-level regime is not bearish."""

    name = "regime_filtered_mean_reversion"
    required_bars = 70

    def __init__(
        self,
        *,
        timeframe: str = "1d",
        max_rsi: float = 38.0,
        max_adx: float = 32.0,
        risk_multiple: float = 1.8,
    ):
        self.timeframe = timeframe
        self.max_rsi = max_rsi
        self.max_adx = max_adx
        self.risk_multiple = risk_multiple

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> Signal | None:
        if not self._ensure_length(data):
            return None
        frame = enrich_technical_indicators(data, timeframe=self.timeframe)
        last = frame.iloc[-1]
        price = _safe_float(last.get("close"))
        atr = _safe_float(last.get("atr_14"))
        if price is None or atr is None:
            return None
        ema_50 = _safe_float(last.get("ema_50"), price) or price
        ema_200 = _safe_float(last.get("ema_200"), ema_50) or ema_50
        rsi = _safe_float(last.get("rsi_14"), 50.0) or 50.0
        adx = _safe_float(last.get("adx_14"), 0.0) or 0.0
        lower_band = _safe_float(last.get("bb_lower"), price) or price
        mean_target = max(_safe_float(last.get("bb_mid"), price) or price, _safe_float(last.get("vwap"), price) or price)
        regime_ok = ema_50 >= ema_200 * 0.98 and price >= ema_200 * 0.94
        oversold = price <= lower_band * 1.01 and rsi <= self.max_rsi
        reversal_bar = price > (_safe_float(last.get("open"), price) or price) or (_safe_float(last.get("stoch_rsi"), 1.0) or 1.0) <= 0.25
        if not (regime_ok and oversold and reversal_bar and adx <= self.max_adx):
            return None
        stop = min(_recent_low(frame, 8) or price - atr, price - (1.25 * atr))
        risk = price - stop
        if mean_target <= price and risk > 0:
            mean_target = price + (risk * self.risk_multiple)
        adjusted_multiple = max((mean_target - price) / risk, 1.2) if risk > 0 else self.risk_multiple
        return _long_signal(
            self,
            symbol=symbol,
            price=price,
            stop=stop,
            risk_multiple=adjusted_multiple,
            rationale="Mean-reversion entry is oversold but still inside a constructive higher-level regime.",
            confidence=0.63,
            metadata=_metadata(
                row=last,
                style="mean_reversion",
                setup_type="regime_filtered_mean_reversion",
                risk_reward=adjusted_multiple,
                extra={"regime_filter": "ema50_vs_ema200_non_bearish"},
            ),
        )
