"""Enhanced research strategies: trend."""

from __future__ import annotations


import pandas as pd

from app.indicators import enrich_technical_indicators
from app.models.signal import Signal
from app.strategies.base import BaseStrategy

from app.strategies.enhanced._common import (
    _condition_rejections,
    _long_signal,
    _metadata,
    _recent_low,
    _reject,
    _safe_float,
    _weak_long_signal,
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
            _reject(self, rejection_reasons=["insufficient_data"])
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
            _reject(self, rejection_reasons=["indicator_unavailable"], row=last)
            return None
        ema_50 = _safe_float(last.get("ema_50"), price) or price
        ema_200 = _safe_float(last.get("ema_200"), ema_50) or ema_50
        regime_ok = price > ema_50 and ema_50 >= ema_200 * 0.99
        rs_ok = latest_roc > max(0.015, baseline_roc + 0.01)
        volume_ok = (_safe_float(last.get("relative_volume"), 0.0) or 0.0) >= self.minimum_relative_volume
        checks = {
            "regime_alignment_too_low": regime_ok,
            "relative_strength_market_too_low": rs_ok,
            "relative_volume_too_low": volume_ok,
        }
        if not all(checks.values()):
            _reject(
                self,
                rejection_reasons=_condition_rejections(checks),
                row=last,
                score=54.0 if rs_ok else 44.0,
                measurements={"latest_roc": latest_roc, "baseline_roc": baseline_roc},
            )
            return None
        stop = max(_recent_low(frame, 12) or price - atr, price - (2.0 * atr))
        self.last_diagnostics = {}
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


class MultiTimeframeTrendPullbackStrategy(BaseStrategy):
    """Trend pullback using fast/slow EMA structure as a higher-timeframe proxy."""

    name = "multi_timeframe_trend_pullback"
    required_bars = 90

    def __init__(
        self,
        *,
        timeframe: str = "1h",
        minimum_relative_volume: float = 0.85,
        risk_multiple: float = 2.2,
    ):
        self.timeframe = timeframe
        self.minimum_relative_volume = minimum_relative_volume
        self.risk_multiple = risk_multiple

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> Signal | None:
        if not self._ensure_length(data):
            _reject(self, rejection_reasons=["insufficient_data"])
            return None
        frame = enrich_technical_indicators(data, timeframe=self.timeframe)
        last = frame.iloc[-1]
        price = _safe_float(last.get("close"))
        atr = _safe_float(last.get("atr_14"))
        ema_20 = _safe_float(last.get("ema_20"))
        ema_50 = _safe_float(last.get("ema_50"))
        ema_200 = _safe_float(last.get("ema_200"), ema_50)
        if price is None or atr is None or ema_20 is None or ema_50 is None or ema_200 is None:
            _reject(self, rejection_reasons=["indicator_unavailable"], row=last)
            return None
        low = _safe_float(last.get("low"), price) or price
        trend_ok = price > ema_50 and ema_50 >= ema_200 * 0.98
        pulled_back = low <= ema_20 + (0.35 * atr)
        reclaimed = price > ema_20 and price > (_safe_float(last.get("open"), price) or price)
        volume_ok = (_safe_float(last.get("relative_volume"), 0.0) or 0.0) >= self.minimum_relative_volume
        checks = {
            "trend_not_aligned": trend_ok,
            "pullback_not_at_support": pulled_back,
            "ema_reclaim_not_confirmed": reclaimed,
            "relative_volume_too_low": volume_ok,
        }
        if not all(checks.values()):
            _reject(
                self,
                rejection_reasons=_condition_rejections(checks),
                row=last,
                score=56.0 if pulled_back or reclaimed else 43.0,
                measurements={"ema_20": ema_20, "ema_50": ema_50, "ema_200": ema_200},
            )
            return None
        stop = min(_recent_low(frame, 10) or price - atr, ema_50 - (0.5 * atr))
        self.last_diagnostics = {}
        return _long_signal(
            self,
            symbol=symbol,
            price=price,
            stop=stop,
            risk_multiple=self.risk_multiple,
            rationale="Multi-timeframe trend pullback reclaimed fast EMA support.",
            confidence=0.65,
            metadata=_metadata(
                row=last,
                style="pullback_continuation",
                setup_type="multi_timeframe_trend_pullback",
                risk_reward=self.risk_multiple,
            ),
        )


class EtfMegaCapRelativeStrengthRotationStrategy(BaseStrategy):
    """Mega-cap/ETF trend rotation proxy using ROC, trend, and liquidity."""

    name = "etf_mega_cap_relative_strength_rotation"
    required_bars = 90

    def __init__(
        self,
        *,
        timeframe: str = "1d",
        roc_window: int = 20,
        minimum_relative_volume: float = 0.85,
        risk_multiple: float = 2.3,
    ):
        self.timeframe = timeframe
        self.roc_window = roc_window
        self.minimum_relative_volume = minimum_relative_volume
        self.risk_multiple = risk_multiple

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> Signal | None:
        if not self._ensure_length(data):
            _reject(self, rejection_reasons=["insufficient_data"])
            return None
        frame = enrich_technical_indicators(data, timeframe=self.timeframe)
        last = frame.iloc[-1]
        price = _safe_float(last.get("close"))
        atr = _safe_float(last.get("atr_14"))
        roc = _safe_float(frame["close"].astype(float).pct_change(self.roc_window).iloc[-1])
        if price is None or atr is None or roc is None:
            _reject(self, rejection_reasons=["indicator_unavailable"], row=last)
            return None
        ema_50 = _safe_float(last.get("ema_50"), price) or price
        ema_200 = _safe_float(last.get("ema_200"), ema_50) or ema_50
        trend_ok = price > ema_50 and ema_50 >= ema_200 * 0.99
        rotation_ok = roc >= 0.015
        volume_ok = (_safe_float(last.get("relative_volume"), 0.0) or 0.0) >= self.minimum_relative_volume
        checks = {
            "trend_not_aligned": trend_ok,
            "relative_strength_market_too_low": rotation_ok,
            "relative_volume_too_low": volume_ok,
        }
        if not all(checks.values()):
            rejection_reasons = _condition_rejections(checks)
            weak = _weak_long_signal(
                self,
                symbol=symbol,
                row=last,
                price=price,
                stop=min(_recent_low(frame, 14) or price - atr, price - (1.8 * atr)),
                risk_multiple=max(self.risk_multiple, 1.0),
                rationale="Supervised weak-valid relative-strength rotation with real trend/rotation anchor but incomplete confirmation.",
                confidence=0.50,
                style="rotation",
                setup_type="etf_mega_cap_relative_strength_rotation",
                rejection_reasons=rejection_reasons,
                setup_anchor=trend_ok and rotation_ok,
                extra={"roc": round(roc, 4), "roc_window": self.roc_window, "weak_signal_kind": "rotation_anchor"},
            )
            if weak is not None:
                self.last_diagnostics = {}
                return weak
            _reject(
                self,
                rejection_reasons=rejection_reasons,
                row=last,
                score=55.0 if rotation_ok else 42.0,
                measurements={"roc": roc, "roc_window": self.roc_window},
            )
            return None
        stop = min(_recent_low(frame, 14) or price - atr, price - (1.8 * atr))
        self.last_diagnostics = {}
        return _long_signal(
            self,
            symbol=symbol,
            price=price,
            stop=stop,
            risk_multiple=self.risk_multiple,
            rationale="ETF/mega-cap relative-strength rotation aligned with daily trend.",
            confidence=0.64,
            metadata=_metadata(
                row=last,
                style="rotation",
                setup_type="etf_mega_cap_relative_strength_rotation",
                risk_reward=self.risk_multiple,
                extra={"roc": round(roc, 4), "roc_window": self.roc_window},
            ),
        )


class RegimeAlignedTrendContinuationStrategy(BaseStrategy):
    """Trend continuation when the broader EMA regime is aligned but classic confluence is incomplete."""

    name = "regime_aligned_trend_continuation"
    required_bars = 90

    def __init__(
        self,
        *,
        timeframe: str = "1h",
        roc_window: int = 12,
        minimum_relative_volume: float = 0.75,
        risk_multiple: float = 2.3,
    ):
        self.timeframe = timeframe
        self.roc_window = roc_window
        self.minimum_relative_volume = minimum_relative_volume
        self.risk_multiple = risk_multiple

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> Signal | None:
        if not self._ensure_length(data):
            _reject(self, rejection_reasons=["insufficient_data"])
            return None
        frame = enrich_technical_indicators(data, timeframe=self.timeframe)
        last = frame.iloc[-1]
        price = _safe_float(last.get("close"))
        atr = _safe_float(last.get("atr_14"))
        if price is None or atr is None:
            _reject(self, rejection_reasons=["indicator_unavailable"], row=last)
            return None
        ema_9 = _safe_float(last.get("ema_9"), price) or price
        ema_20 = _safe_float(last.get("ema_20"), ema_9) or ema_9
        ema_50 = _safe_float(last.get("ema_50"), ema_20) or ema_20
        ema_200 = _safe_float(last.get("ema_200"), ema_50) or ema_50
        low = _safe_float(last.get("low"), price) or price
        open_price = _safe_float(last.get("open"), price) or price
        rsi = _safe_float(last.get("rsi_14"), 50.0) or 50.0
        adx = _safe_float(last.get("adx_14"), 0.0) or 0.0
        macd_hist = _safe_float(last.get("macd_hist"), 0.0) or 0.0
        roc = _safe_float(frame["close"].astype(float).pct_change(self.roc_window).iloc[-1], 0.0) or 0.0
        regime_ok = price > ema_20 and ema_20 >= ema_50 * 0.995 and ema_50 >= ema_200 * 0.98
        pullback_ok = low <= ema_20 + (0.55 * atr) and price >= ema_9 * 0.995
        trend_strength_ok = adx >= 14.0 or roc >= 0.01
        continuation_ok = rsi >= 50.0 and (price > open_price or macd_hist >= 0.0)
        volume_ok = (_safe_float(last.get("relative_volume"), 0.0) or 0.0) >= self.minimum_relative_volume
        checks = {
            "regime_alignment_too_low": regime_ok,
            "pullback_not_at_support": pullback_ok,
            "trend_strength_too_low": trend_strength_ok,
            "confirmation_not_present": continuation_ok,
            "relative_volume_too_low": volume_ok,
        }
        if not all(checks.values()):
            _reject(
                self,
                rejection_reasons=_condition_rejections(checks),
                row=last,
                score=57.0 if regime_ok and trend_strength_ok else 43.0,
                measurements={"roc": roc, "ema_9": ema_9, "ema_20": ema_20, "ema_50": ema_50, "ema_200": ema_200},
            )
            return None
        stop = min(_recent_low(frame, 12) or price - atr, ema_50 - (0.40 * atr), price - (1.35 * atr))
        self.last_diagnostics = {}
        return _long_signal(
            self,
            symbol=symbol,
            price=price,
            stop=stop,
            risk_multiple=self.risk_multiple,
            rationale="Regime-aligned trend continuation reclaimed fast-trend support with constructive momentum.",
            confidence=0.64,
            metadata=_metadata(
                row=last,
                style="trend",
                setup_type="regime_aligned_trend_continuation",
                risk_reward=self.risk_multiple,
                extra={"roc": round(roc, 4), "roc_window": self.roc_window},
            ),
        )


