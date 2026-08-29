"""Enhanced research strategies: continuation."""

from __future__ import annotations


import pandas as pd

from app.indicators import enrich_technical_indicators
from app.models.signal import Signal
from app.strategies.base import BaseStrategy

from app.strategies.enhanced._common import (
    _condition_rejections,
    _liquidity_ok,
    _long_signal,
    _metadata,
    _recent_low,
    _reject,
    _safe_float,
    _weak_long_signal,
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
            _reject(self, rejection_reasons=["insufficient_data"])
            return None
        frame = enrich_technical_indicators(data, timeframe=self.timeframe)
        last = frame.iloc[-1]
        prev = frame.iloc[-2]
        price = _safe_float(last.get("close"))
        atr = _safe_float(last.get("atr_14"))
        vwap = _safe_float(last.get("vwap"))
        if price is None or atr is None or vwap is None:
            _reject(self, rejection_reasons=["indicator_unavailable"], row=last)
            return None
        ema_20 = _safe_float(last.get("ema_20"), price) or price
        ema_50 = _safe_float(last.get("ema_50"), ema_20) or ema_20
        tolerance = atr * self.pullback_tolerance_atr
        pulled_back = min(_safe_float(prev.get("low"), price) or price, _safe_float(last.get("low"), price) or price) <= max(vwap, ema_20) + tolerance
        reclaimed = price > max(vwap, ema_20) and price > (_safe_float(last.get("open"), price) or price)
        volume_ok = (_safe_float(last.get("relative_volume"), 0.0) or 0.0) >= self.minimum_relative_volume
        checks = {
            "trend_not_aligned": price > ema_50 and ema_20 >= ema_50,
            "pullback_not_at_support": pulled_back,
            "vwap_reclaim_not_confirmed": reclaimed,
            "relative_volume_too_low": volume_ok,
        }
        if not all(checks.values()):
            rejection_reasons = _condition_rejections(checks)
            weak = _weak_long_signal(
                self,
                symbol=symbol,
                row=last,
                price=price,
                stop=min(_recent_low(frame, 8) or price - atr, vwap - (0.6 * atr)),
                risk_multiple=max(self.risk_multiple, 1.0),
                rationale="Supervised weak-valid VWAP pullback continuation with real support reclaim but incomplete confirmation.",
                confidence=0.50,
                style="pullback_continuation",
                setup_type="anchored_vwap_pullback_continuation",
                rejection_reasons=rejection_reasons,
                setup_anchor=pulled_back and reclaimed,
                extra={"vwap_anchor": "session_or_cumulative", "weak_signal_kind": "anchored_vwap_reclaim"},
            )
            if weak is not None:
                self.last_diagnostics = {}
                return weak
            _reject(
                self,
                rejection_reasons=rejection_reasons,
                row=last,
                score=55.0 if pulled_back or reclaimed else 43.0,
                measurements={"vwap": vwap, "ema_20": ema_20, "ema_50": ema_50},
            )
            return None
        stop = min(_recent_low(frame, 8) or price - atr, vwap - (0.6 * atr))
        self.last_diagnostics = {}
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


class LiquidityExpansionContinuationStrategy(BaseStrategy):
    """Continuation entry when liquidity expands into a constructive trend."""

    name = "liquidity_expansion_continuation"
    required_bars = 50

    def __init__(
        self,
        *,
        timeframe: str = "15m",
        minimum_relative_volume: float = 1.20,
        minimum_body_to_range: float = 0.45,
        risk_multiple: float = 2.0,
    ):
        self.timeframe = timeframe
        self.minimum_relative_volume = minimum_relative_volume
        self.minimum_body_to_range = minimum_body_to_range
        self.risk_multiple = risk_multiple

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> Signal | None:
        if not self._ensure_length(data):
            _reject(self, rejection_reasons=["insufficient_data"])
            return None
        frame = enrich_technical_indicators(data, timeframe=self.timeframe)
        last = frame.iloc[-1]
        price = _safe_float(last.get("close"))
        atr = _safe_float(last.get("atr_14"))
        open_price = _safe_float(last.get("open"))
        high = _safe_float(last.get("high"))
        low = _safe_float(last.get("low"))
        if price is None or atr is None or open_price is None or high is None or low is None:
            _reject(self, rejection_reasons=["indicator_unavailable"], row=last)
            return None
        bar_range = max(high - low, 0.01)
        body_to_range = abs(price - open_price) / bar_range
        close_location = (price - low) / bar_range
        trend_ok = price > (_safe_float(last.get("ema_20"), price) or price) > (_safe_float(last.get("ema_50"), 0.0) or 0.0)
        volume_ok = (_safe_float(last.get("relative_volume"), 0.0) or 0.0) >= self.minimum_relative_volume
        candle_ok = price > open_price and body_to_range >= self.minimum_body_to_range and close_location >= 0.65
        checks = {
            "trend_not_aligned": trend_ok,
            "relative_volume_too_low": volume_ok,
            "candle_body_too_small": candle_ok,
        }
        if not all(checks.values()):
            rejection_reasons = _condition_rejections(checks)
            weak = _weak_long_signal(
                self,
                symbol=symbol,
                row=last,
                price=price,
                stop=min(_recent_low(frame, 6) or price - atr, low - (0.25 * atr)),
                risk_multiple=max(self.risk_multiple, 1.0),
                rationale="Supervised weak-valid liquidity expansion continuation with real trend candle but incomplete confirmation.",
                confidence=0.50,
                style="momentum",
                setup_type="liquidity_expansion_continuation",
                rejection_reasons=rejection_reasons,
                setup_anchor=trend_ok and candle_ok,
                extra={
                    "body_to_range": round(body_to_range, 4),
                    "close_location": round(close_location, 4),
                    "weak_signal_kind": "liquidity_expansion_anchor",
                },
            )
            if weak is not None:
                self.last_diagnostics = {}
                return weak
            _reject(
                self,
                rejection_reasons=rejection_reasons,
                row=last,
                score=56.0 if volume_ok and candle_ok else 42.0,
                measurements={"body_to_range": body_to_range, "close_location": close_location},
            )
            return None
        stop = min(_recent_low(frame, 6) or price - atr, low - (0.25 * atr))
        self.last_diagnostics = {}
        return _long_signal(
            self,
            symbol=symbol,
            price=price,
            stop=stop,
            risk_multiple=self.risk_multiple,
            rationale="Liquidity expansion continuation confirmed by strong candle structure.",
            confidence=0.64,
            metadata=_metadata(
                row=last,
                style="momentum",
                setup_type="liquidity_expansion_continuation",
                risk_reward=self.risk_multiple,
                extra={"body_to_range": round(body_to_range, 4), "close_location": round(close_location, 4)},
            ),
        )


class RelativeVolumeReclaimContinuationStrategy(BaseStrategy):
    """Paper-only continuation setup for VWAP/EMA reclaims with moderate relative volume."""

    name = "relative_volume_reclaim_continuation"
    required_bars = 55

    def __init__(
        self,
        *,
        timeframe: str = "15m",
        minimum_relative_volume: float = 0.85,
        minimum_dollar_volume: float = 2_000_000.0,
        risk_multiple: float = 2.0,
    ):
        self.timeframe = timeframe
        self.minimum_relative_volume = minimum_relative_volume
        self.minimum_dollar_volume = minimum_dollar_volume
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
        vwap = _safe_float(last.get("vwap"))
        ema_20 = _safe_float(last.get("ema_20"))
        ema_50 = _safe_float(last.get("ema_50"))
        if price is None or atr is None or vwap is None or ema_20 is None or ema_50 is None:
            _reject(self, rejection_reasons=["indicator_unavailable"], row=last)
            return None
        support = max(vwap, ema_20)
        low = _safe_float(last.get("low"), price) or price
        prev_close = _safe_float(previous.get("close"), price) or price
        open_price = _safe_float(last.get("open"), price) or price
        rsi = _safe_float(last.get("rsi_14"), 50.0) or 50.0
        macd_hist = _safe_float(last.get("macd_hist"), 0.0) or 0.0
        pulled_into_support = min(low, prev_close) <= support + (0.30 * atr)
        reclaimed = price > support and price > open_price
        trend_ok = price > ema_50 and ema_20 >= ema_50 * 0.995
        volume_ok = (_safe_float(last.get("relative_volume"), 0.0) or 0.0) >= self.minimum_relative_volume
        momentum_ok = rsi >= 48.0 and macd_hist >= -0.05
        liquidity_ok = _liquidity_ok(last, self.minimum_dollar_volume)
        checks = {
            "trend_not_aligned": trend_ok,
            "reclaim_not_confirmed": pulled_into_support and reclaimed,
            "relative_volume_too_low": volume_ok,
            "momentum_not_constructive": momentum_ok,
            "average_dollar_volume_below_threshold": liquidity_ok,
        }
        if not all(checks.values()):
            rejection_reasons = _condition_rejections(checks)
            weak = _weak_long_signal(
                self,
                symbol=symbol,
                row=last,
                price=price,
                stop=min(_recent_low(frame, 8) or price - atr, support - (0.65 * atr), price - atr),
                risk_multiple=max(self.risk_multiple, 1.0),
                rationale="Supervised weak-valid relative-volume reclaim continuation with real support reclaim but incomplete confirmation.",
                confidence=0.50,
                style="pullback_continuation",
                setup_type="relative_volume_reclaim_continuation",
                rejection_reasons=rejection_reasons,
                setup_anchor=pulled_into_support and reclaimed,
                extra={
                    "support": round(support, 4),
                    "reclaim_level": "max_vwap_ema20",
                    "weak_signal_kind": "support_reclaim_anchor",
                },
            )
            if weak is not None:
                self.last_diagnostics = {}
                return weak
            _reject(
                self,
                rejection_reasons=rejection_reasons,
                row=last,
                score=56.0 if reclaimed or volume_ok else 43.0,
                measurements={"support": support, "vwap": vwap, "ema_20": ema_20, "ema_50": ema_50},
            )
            return None
        stop = min(_recent_low(frame, 8) or price - atr, support - (0.65 * atr), price - atr)
        self.last_diagnostics = {}
        return _long_signal(
            self,
            symbol=symbol,
            price=price,
            stop=stop,
            risk_multiple=self.risk_multiple,
            rationale="Relative-volume reclaim continuation confirmed near VWAP/EMA support.",
            confidence=0.63,
            metadata=_metadata(
                row=last,
                style="pullback_continuation",
                setup_type="relative_volume_reclaim_continuation",
                risk_reward=self.risk_multiple,
                extra={"support": round(support, 4), "reclaim_level": "max_vwap_ema20"},
            ),
        )


