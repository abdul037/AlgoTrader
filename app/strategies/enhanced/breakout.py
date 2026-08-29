"""Enhanced research strategies: breakout."""

from __future__ import annotations


import pandas as pd

from app.indicators import compute_confluence_score, enrich_technical_indicators
from app.models.signal import Signal
from app.strategies.base import BaseStrategy

from app.strategies.enhanced._common import (
    _condition_rejections,
    _liquidity_ok,
    _long_signal,
    _metadata,
    _recent_high,
    _recent_low,
    _reject,
    _safe_float,
    _weak_long_signal,
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
            _reject(self, rejection_reasons=["insufficient_data"])
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
            _reject(self, rejection_reasons=["indicator_unavailable"], row=last)
            return None
        trend_ok = price > (_safe_float(last.get("ema_20"), price) or price) > (_safe_float(last.get("ema_50"), 0.0) or 0.0)
        volume_ok = (_safe_float(last.get("relative_volume"), 0.0) or 0.0) >= self.minimum_relative_volume
        contraction_ok = width <= width_median * 0.9
        breakout_ok = price > range_high
        liquidity_ok = _liquidity_ok(last, self.minimum_dollar_volume)
        checks = {
            "trend_not_aligned": trend_ok,
            "relative_volume_too_low": volume_ok,
            "volatility_not_contracting": contraction_ok,
            "breakout_level_not_cleared": breakout_ok,
            "average_dollar_volume_below_threshold": liquidity_ok,
        }
        if not all(checks.values()):
            _reject(
                self,
                rejection_reasons=_condition_rejections(checks),
                row=last,
                score=55.0 if breakout_ok or contraction_ok else 42.0,
                measurements={"range_high": range_high, "bb_width_median": width_median},
            )
            return None
        stop = min(_recent_low(frame, 10) or price - atr, price - (1.35 * atr))
        self.last_diagnostics = {}
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
            _reject(self, rejection_reasons=["insufficient_data"])
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
            _reject(self, rejection_reasons=["indicator_unavailable"], row=last)
            return None
        trend_ok = price > (_safe_float(last.get("ema_20"), price) or price) > (_safe_float(last.get("ema_50"), 0.0) or 0.0)
        adx_ok = (_safe_float(last.get("adx_14"), 0.0) or 0.0) >= self.minimum_adx
        atr_ok = 0.25 <= (_safe_float(last.get("atr_pct"), 0.0) or 0.0) <= 8.0
        breakout_ok = price > channel_high
        checks = {
            "breakout_level_not_cleared": breakout_ok,
            "trend_not_aligned": trend_ok,
            "adx_too_low": adx_ok,
            "volatility_out_of_range": atr_ok,
        }
        if not all(checks.values()):
            _reject(
                self,
                rejection_reasons=_condition_rejections(checks),
                row=last,
                score=56.0 if breakout_ok else 43.0,
                measurements={"channel_high": channel_high, "channel_low": channel_low},
            )
            return None
        stop = max(channel_low, price - (2.2 * atr))
        self.last_diagnostics = {}
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


class InsideBarNarrowRangeBreakoutStrategy(BaseStrategy):
    """Breakout from inside-bar or narrow-range compression."""

    name = "inside_bar_narrow_range_breakout"
    required_bars = 50

    def __init__(
        self,
        *,
        timeframe: str = "1h",
        narrow_window: int = 7,
        minimum_relative_volume: float = 0.95,
        risk_multiple: float = 2.1,
    ):
        self.timeframe = timeframe
        self.narrow_window = narrow_window
        self.minimum_relative_volume = minimum_relative_volume
        self.risk_multiple = risk_multiple

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> Signal | None:
        if not self._ensure_length(data):
            _reject(self, rejection_reasons=["insufficient_data"])
            return None
        frame = enrich_technical_indicators(data, timeframe=self.timeframe)
        last = frame.iloc[-1]
        previous = frame.iloc[-2]
        price = _safe_float(last.get("close"))
        atr = _safe_float(last.get("atr_14"))
        prev_high = _safe_float(previous.get("high"))
        prev_low = _safe_float(previous.get("low"))
        if price is None or atr is None or prev_high is None or prev_low is None:
            _reject(self, rejection_reasons=["indicator_unavailable"], row=last)
            return None
        ranges = (frame["high"].astype(float) - frame["low"].astype(float)).tail(self.narrow_window + 1).iloc[:-1]
        current_range = (_safe_float(previous.get("high"), 0.0) or 0.0) - (_safe_float(previous.get("low"), 0.0) or 0.0)
        narrow = bool(len(ranges) and current_range <= float(ranges.median()) * 0.75)
        inside = (_safe_float(previous.get("high"), 0.0) or 0.0) <= (_safe_float(frame.iloc[-3].get("high"), prev_high) or prev_high) and (
            _safe_float(previous.get("low"), 0.0) or 0.0
        ) >= (_safe_float(frame.iloc[-3].get("low"), prev_low) or prev_low)
        breakout = price > prev_high
        volume_ok = (_safe_float(last.get("relative_volume"), 0.0) or 0.0) >= self.minimum_relative_volume
        checks = {
            "compression_not_present": narrow or inside,
            "breakout_level_not_cleared": breakout,
            "relative_volume_too_low": volume_ok,
        }
        if not all(checks.values()):
            rejection_reasons = _condition_rejections(checks)
            weak = _weak_long_signal(
                self,
                symbol=symbol,
                row=last,
                price=price,
                stop=min(prev_low, price - (1.0 * atr)),
                risk_multiple=max(self.risk_multiple, 1.0),
                rationale="Supervised weak-valid inside/narrow-range breakout with real compression breakout but incomplete confirmation.",
                confidence=0.50,
                style="breakout",
                setup_type="inside_bar_narrow_range_breakout",
                rejection_reasons=rejection_reasons,
                setup_anchor=(narrow or inside) and breakout,
                extra={"weak_signal_kind": "inside_narrow_breakout_anchor"},
            )
            if weak is not None:
                self.last_diagnostics = {}
                return weak
            _reject(
                self,
                rejection_reasons=rejection_reasons,
                row=last,
                score=55.0 if breakout else 42.0,
                measurements={"previous_high": prev_high, "previous_low": prev_low, "inside_bar": inside, "narrow_range": narrow},
            )
            return None
        stop = min(prev_low, price - (1.0 * atr))
        self.last_diagnostics = {}
        return _long_signal(
            self,
            symbol=symbol,
            price=price,
            stop=stop,
            risk_multiple=self.risk_multiple,
            rationale="Inside-bar/narrow-range compression broke higher with volume confirmation.",
            confidence=0.63,
            metadata=_metadata(
                row=last,
                style="breakout",
                setup_type="inside_bar_narrow_range_breakout",
                risk_reward=self.risk_multiple,
            ),
        )


class EarlyBreakoutPullbackContinuationStrategy(BaseStrategy):
    """Near-breakout continuation for candidates repeatedly rejected as not fully cleared."""

    name = "early_breakout_pullback_continuation"
    required_bars = 65

    def __init__(
        self,
        *,
        timeframe: str = "15m",
        channel_window: int = 20,
        breakout_tolerance_atr: float = 0.35,
        minimum_relative_volume: float = 0.80,
        risk_multiple: float = 2.2,
    ):
        self.timeframe = timeframe
        self.channel_window = channel_window
        self.breakout_tolerance_atr = breakout_tolerance_atr
        self.minimum_relative_volume = minimum_relative_volume
        self.risk_multiple = risk_multiple

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> Signal | None:
        if not self._ensure_length(data):
            _reject(self, rejection_reasons=["insufficient_data"])
            return None
        frame = enrich_technical_indicators(data, timeframe=self.timeframe)
        prior = frame.iloc[:-1]
        last = frame.iloc[-1]
        price = _safe_float(last.get("close"))
        atr = _safe_float(last.get("atr_14"))
        channel_high = _recent_high(prior, self.channel_window)
        if price is None or atr is None or channel_high is None:
            _reject(self, rejection_reasons=["indicator_unavailable"], row=last)
            return None
        ema_20 = _safe_float(last.get("ema_20"), price) or price
        ema_50 = _safe_float(last.get("ema_50"), ema_20) or ema_20
        low = _safe_float(last.get("low"), price) or price
        open_price = _safe_float(last.get("open"), price) or price
        rsi = _safe_float(last.get("rsi_14"), 50.0) or 50.0
        gap_atr = (channel_high - price) / max(atr, 0.01)
        near_breakout = -0.20 <= gap_atr <= self.breakout_tolerance_atr
        trend_ok = price > ema_20 and ema_20 >= ema_50 * 0.995
        pullback_ok = low <= ema_20 + (0.45 * atr)
        confirmation_ok = price > open_price and rsi >= 50.0
        volume_ok = (_safe_float(last.get("relative_volume"), 0.0) or 0.0) >= self.minimum_relative_volume
        checks = {
            "breakout_level_not_cleared": near_breakout,
            "trend_not_aligned": trend_ok,
            "pullback_not_at_support": pullback_ok,
            "confirmation_not_present": confirmation_ok,
            "relative_volume_too_low": volume_ok,
        }
        if not all(checks.values()):
            _reject(
                self,
                rejection_reasons=_condition_rejections(checks),
                row=last,
                score=56.0 if near_breakout else 43.0,
                measurements={"channel_high": channel_high, "breakout_gap_atr": gap_atr},
            )
            return None
        stop = min(_recent_low(frame, 10) or price - atr, ema_50 - (0.35 * atr), price - (1.15 * atr))
        self.last_diagnostics = {}
        return _long_signal(
            self,
            symbol=symbol,
            price=price,
            stop=stop,
            risk_multiple=self.risk_multiple,
            rationale="Early breakout pullback is within ATR tolerance of resistance and reclaiming trend support.",
            confidence=0.62,
            metadata=_metadata(
                row=last,
                style="breakout",
                setup_type="early_breakout_pullback_continuation",
                risk_reward=self.risk_multiple,
                extra={"channel_high": round(channel_high, 4), "breakout_gap_atr": round(gap_atr, 4)},
            ),
        )


class ConfluenceRecoveryBreakoutStrategy(BaseStrategy):
    """Recovery breakout that accepts partial confluence when compression, trend, and volume align."""

    name = "confluence_recovery_breakout"
    required_bars = 70

    def __init__(
        self,
        *,
        timeframe: str = "1h",
        breakout_window: int = 20,
        minimum_relative_volume: float = 0.85,
        minimum_confluence_score: float = 0.35,
        risk_multiple: float = 2.1,
    ):
        self.timeframe = timeframe
        self.breakout_window = breakout_window
        self.minimum_relative_volume = minimum_relative_volume
        self.minimum_confluence_score = minimum_confluence_score
        self.risk_multiple = risk_multiple

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> Signal | None:
        if not self._ensure_length(data):
            _reject(self, rejection_reasons=["insufficient_data"])
            return None
        frame = enrich_technical_indicators(data, timeframe=self.timeframe)
        prior = frame.iloc[:-1]
        last = frame.iloc[-1]
        previous = frame.iloc[-2]
        price = _safe_float(last.get("close"))
        atr = _safe_float(last.get("atr_14"))
        range_high = _recent_high(prior, self.breakout_window)
        width = _safe_float(last.get("bb_width_pct"))
        width_median = _safe_float(frame["bb_width_pct"].tail(self.breakout_window + 1).iloc[:-1].median())
        if price is None or atr is None or range_high is None or width is None or width_median is None:
            _reject(self, rejection_reasons=["indicator_unavailable"], row=last)
            return None
        ema_20 = _safe_float(last.get("ema_20"), price) or price
        ema_50 = _safe_float(last.get("ema_50"), ema_20) or ema_20
        vwap = _safe_float(last.get("vwap"), ema_20) or ema_20
        open_price = _safe_float(last.get("open"), price) or price
        prev_close = _safe_float(previous.get("close"), price) or price
        confluence = compute_confluence_score(last)
        compression_ok = width <= max(width_median * 1.05, 4.5)
        trend_ok = price > ema_50 and ema_20 >= ema_50 * 0.995
        reclaim_ok = price > max(ema_20, vwap) and prev_close <= max(ema_20, vwap) + (0.50 * atr)
        breakout_ready = price >= range_high - (0.25 * atr) and price > open_price
        volume_ok = (_safe_float(last.get("relative_volume"), 0.0) or 0.0) >= self.minimum_relative_volume
        confluence_ok = confluence >= self.minimum_confluence_score
        checks = {
            "compression_not_present": compression_ok,
            "trend_not_aligned": trend_ok,
            "reclaim_not_confirmed": reclaim_ok,
            "breakout_level_not_cleared": breakout_ready,
            "relative_volume_too_low": volume_ok,
            "confluence_too_low": confluence_ok,
        }
        if not all(checks.values()):
            _reject(
                self,
                rejection_reasons=_condition_rejections(checks),
                row=last,
                score=57.0 if breakout_ready and confluence_ok else 43.0,
                measurements={
                    "range_high": range_high,
                    "bb_width_median": width_median,
                    "confluence": confluence,
                    "vwap": vwap,
                },
            )
            return None
        stop = min(_recent_low(frame, 10) or price - atr, ema_50 - (0.45 * atr), price - (1.20 * atr))
        self.last_diagnostics = {}
        return _long_signal(
            self,
            symbol=symbol,
            price=price,
            stop=stop,
            risk_multiple=self.risk_multiple,
            rationale="Compression recovery breakout aligned enough confluence, volume, and trend for paper exploration.",
            confidence=0.63,
            metadata=_metadata(
                row=last,
                style="breakout",
                setup_type="confluence_recovery_breakout",
                risk_reward=self.risk_multiple,
                extra={"range_high": round(range_high, 4), "confluence": round(confluence, 4)},
            ),
        )
