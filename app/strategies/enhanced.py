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
from app.strategies.weak_signals import build_supervised_weak_long_signal


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


def _diagnostic_measurements(row: pd.Series | None, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    keys = [
        "close",
        "relative_volume",
        "avg_dollar_volume_20",
        "atr_14",
        "atr_pct",
        "adx_14",
        "rsi_14",
        "bb_width_pct",
        "ema_20",
        "ema_50",
        "ema_200",
        "vwap",
    ]
    measurements = {
        key: _safe_float(row.get(key)) if row is not None and key in row else None
        for key in keys
    }
    if extra:
        measurements.update(extra)
    return measurements


def _set_diagnostics(
    strategy: BaseStrategy,
    *,
    status: str,
    rejection_reasons: list[str],
    row: pd.Series | None = None,
    score: float | None = None,
    measurements: dict[str, Any] | None = None,
) -> None:
    strategy.last_diagnostics = {
        "status": status,
        "rejection_reasons": list(dict.fromkeys(rejection_reasons)),
        "reason_codes": list(dict.fromkeys(rejection_reasons)),
        "score": score,
        "measurements": _diagnostic_measurements(row, measurements),
    }


def _reject(
    strategy: BaseStrategy,
    *,
    rejection_reasons: list[str],
    row: pd.Series | None = None,
    score: float | None = 45.0,
    measurements: dict[str, Any] | None = None,
) -> None:
    _set_diagnostics(
        strategy,
        status="no_signal",
        rejection_reasons=rejection_reasons or ["no_strategy_signal"],
        row=row,
        score=score,
        measurements=measurements,
    )


def _condition_rejections(checks: dict[str, bool]) -> list[str]:
    return [name for name, passed in checks.items() if not passed]


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
        _set_diagnostics(
            strategy,
            status="invalid_trade_plan",
            rejection_reasons=["invalid_stop_or_target_generation"],
            measurements={"price": price, "stop": stop, "risk_multiple": risk_multiple},
        )
        return None
    risk = price - stop
    if risk <= max(price * 0.0005, 0.01):
        _set_diagnostics(
            strategy,
            status="invalid_trade_plan",
            rejection_reasons=["risk_too_small_for_trade_plan"],
            measurements={"price": price, "stop": stop, "risk": risk, "risk_multiple": risk_multiple},
        )
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


def _weak_long_signal(
    strategy: BaseStrategy,
    *,
    symbol: str,
    row: pd.Series,
    price: float,
    stop: float,
    risk_multiple: float,
    rationale: str,
    confidence: float,
    style: str,
    setup_type: str,
    rejection_reasons: list[str],
    setup_anchor: bool,
    extra: dict[str, Any] | None = None,
) -> Signal | None:
    return build_supervised_weak_long_signal(
        strategy,
        symbol=symbol,
        price=price,
        stop=stop,
        risk_multiple=risk_multiple,
        rationale=rationale,
        confidence=confidence,
        metadata=_metadata(
            row=row,
            style=style,
            setup_type=setup_type,
            risk_reward=risk_multiple,
            extra=extra,
        ),
        rejection_reasons=rejection_reasons,
        setup_anchor=setup_anchor,
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
            _reject(self, rejection_reasons=["insufficient_data"])
            return None
        frame = enrich_technical_indicators(data, timeframe=self.timeframe)
        last = frame.iloc[-1]
        prev = frame.iloc[-2]
        price = _safe_float(last.get("close"))
        open_price = _safe_float(last.get("open"))
        prev_close = _safe_float(prev.get("close"))
        atr = _safe_float(last.get("atr_14"))
        if price is None or open_price is None or prev_close is None or atr is None or prev_close <= 0:
            _reject(self, rejection_reasons=["indicator_unavailable"], row=last)
            return None
        gap_pct = ((open_price - prev_close) / prev_close) * 100.0
        day_range = max((_safe_float(last.get("high"), price) or price) - (_safe_float(last.get("low"), price) or price), 0.01)
        close_location = (price - (_safe_float(last.get("low"), price) or price)) / day_range
        volume_ok = (_safe_float(last.get("relative_volume"), 0.0) or 0.0) >= self.minimum_relative_volume
        liquidity_ok = _liquidity_ok(last, self.minimum_dollar_volume)
        continuation = gap_pct >= self.minimum_gap_pct and price > open_price and close_location >= 0.65
        fade = gap_pct <= -self.minimum_gap_pct and price > open_price and close_location >= 0.75
        checks = {
            "relative_volume_too_low": volume_ok,
            "average_dollar_volume_below_threshold": liquidity_ok,
            "gap_setup_not_confirmed": continuation or fade,
        }
        if not all(checks.values()):
            _reject(
                self,
                rejection_reasons=_condition_rejections(checks),
                row=last,
                score=54.0 if continuation or fade else 40.0,
                measurements={"gap_pct": gap_pct, "close_location": close_location},
            )
            return None
        setup = "gap_up_continuation" if continuation else "gap_down_fade"
        stop = min(_safe_float(last.get("low"), price - atr) or price - atr, price - (1.2 * atr))
        self.last_diagnostics = {}
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
            _reject(self, rejection_reasons=["insufficient_data"])
            return None
        frame = enrich_technical_indicators(data, timeframe=self.timeframe)
        last = frame.iloc[-1]
        price = _safe_float(last.get("close"))
        atr = _safe_float(last.get("atr_14"))
        if price is None or atr is None:
            _reject(self, rejection_reasons=["indicator_unavailable"], row=last)
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
        checks = {
            "regime_alignment_too_low": regime_ok,
            "mean_reversion_not_oversold": oversold,
            "reversal_bar_not_confirmed": reversal_bar,
            "adx_too_high_for_mean_reversion": adx <= self.max_adx,
        }
        if not all(checks.values()):
            rejection_reasons = _condition_rejections(checks)
            stop = min(_recent_low(frame, 8) or price - atr, price - (1.25 * atr))
            weak_risk = price - stop
            weak_multiple = self.risk_multiple
            if weak_risk > 0 and mean_target > price:
                weak_multiple = max((mean_target - price) / weak_risk, 1.0)
            weak = _weak_long_signal(
                self,
                symbol=symbol,
                row=last,
                price=price,
                stop=stop,
                risk_multiple=weak_multiple,
                rationale="Supervised weak-valid regime-filtered mean reversion with real oversold anchor but incomplete confirmation.",
                confidence=0.50,
                style="mean_reversion",
                setup_type="regime_filtered_mean_reversion",
                rejection_reasons=rejection_reasons,
                setup_anchor=oversold,
                extra={"regime_filter": "ema50_vs_ema200_non_bearish", "weak_signal_kind": "oversold_anchor"},
            )
            if weak is not None:
                self.last_diagnostics = {}
                return weak
            _reject(
                self,
                rejection_reasons=rejection_reasons,
                row=last,
                score=55.0 if oversold or reversal_bar else 42.0,
                measurements={"lower_band": lower_band, "mean_target": mean_target},
            )
            return None
        stop = min(_recent_low(frame, 8) or price - atr, price - (1.25 * atr))
        risk = price - stop
        if mean_target <= price and risk > 0:
            mean_target = price + (risk * self.risk_multiple)
        adjusted_multiple = max((mean_target - price) / risk, 1.2) if risk > 0 else self.risk_multiple
        self.last_diagnostics = {}
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


class OpeningRangeBreakoutRetestStrategy(BaseStrategy):
    """Long opening-range breakout or retest continuation."""

    name = "opening_range_breakout_retest"
    required_bars = 45

    def __init__(
        self,
        *,
        timeframe: str = "15m",
        opening_range_bars: int = 5,
        minimum_relative_volume: float = 0.95,
        risk_multiple: float = 2.0,
    ):
        self.timeframe = timeframe
        self.opening_range_bars = opening_range_bars
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
        opening_high = _safe_float(last.get("opening_range_high"), _recent_high(frame.iloc[:-1], self.opening_range_bars))
        opening_low = _safe_float(last.get("opening_range_low"), _recent_low(frame.iloc[:-1], self.opening_range_bars))
        if price is None or atr is None or opening_high is None or opening_low is None:
            _reject(self, rejection_reasons=["indicator_unavailable"], row=last)
            return None
        prev_close = _safe_float(previous.get("close"), price) or price
        retest_zone = opening_high - (0.35 * atr)
        breakout = price > opening_high and prev_close <= opening_high * 1.01
        retest_reclaim = price > opening_high and (_safe_float(last.get("low"), price) or price) <= max(retest_zone, opening_low)
        volume_ok = (_safe_float(last.get("relative_volume"), 0.0) or 0.0) >= self.minimum_relative_volume
        trend_ok = price > (_safe_float(last.get("vwap"), price) or price) and price > (_safe_float(last.get("ema_20"), price) or price)
        checks = {
            "opening_range_not_cleared": breakout or retest_reclaim,
            "relative_volume_too_low": volume_ok,
            "trend_not_aligned": trend_ok,
        }
        if not all(checks.values()):
            rejection_reasons = _condition_rejections(checks)
            weak = _weak_long_signal(
                self,
                symbol=symbol,
                row=last,
                price=price,
                stop=min(opening_low, price - (1.1 * atr)),
                risk_multiple=max(self.risk_multiple, 1.0),
                rationale="Supervised weak-valid opening-range setup with real range break/retest but incomplete confirmation.",
                confidence=0.50,
                style="opening_range",
                setup_type="opening_range_breakout_retest",
                rejection_reasons=rejection_reasons,
                setup_anchor=breakout or retest_reclaim,
                extra={
                    "opening_range_high": opening_high,
                    "opening_range_low": opening_low,
                    "weak_signal_kind": "opening_range_anchor",
                },
            )
            if weak is not None:
                self.last_diagnostics = {}
                return weak
            _reject(
                self,
                rejection_reasons=rejection_reasons,
                row=last,
                score=56.0 if breakout or retest_reclaim else 43.0,
                measurements={"opening_range_high": opening_high, "opening_range_low": opening_low},
            )
            return None
        stop = min(opening_low, price - (1.1 * atr))
        self.last_diagnostics = {}
        return _long_signal(
            self,
            symbol=symbol,
            price=price,
            stop=stop,
            risk_multiple=self.risk_multiple,
            rationale="Opening range breakout/retest reclaimed range high with volume confirmation.",
            confidence=0.64,
            metadata=_metadata(
                row=last,
                style="opening_range",
                setup_type="opening_range_breakout_retest",
                risk_reward=self.risk_multiple,
                extra={"opening_range_high": opening_high, "opening_range_low": opening_low},
            ),
        )


class FailedBreakdownReversalStrategy(BaseStrategy):
    """Long reversal after a support break fails and price reclaims the level."""

    name = "failed_breakdown_reversal"
    required_bars = 55

    def __init__(
        self,
        *,
        timeframe: str = "1h",
        support_window: int = 20,
        minimum_relative_volume: float = 0.90,
        risk_multiple: float = 1.8,
    ):
        self.timeframe = timeframe
        self.support_window = support_window
        self.minimum_relative_volume = minimum_relative_volume
        self.risk_multiple = risk_multiple

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> Signal | None:
        if not self._ensure_length(data):
            _reject(self, rejection_reasons=["insufficient_data"])
            return None
        frame = enrich_technical_indicators(data, timeframe=self.timeframe)
        last = frame.iloc[-1]
        prior = frame.iloc[:-1]
        price = _safe_float(last.get("close"))
        atr = _safe_float(last.get("atr_14"))
        support = _recent_low(prior, self.support_window)
        low = _safe_float(last.get("low"))
        open_price = _safe_float(last.get("open"))
        if price is None or atr is None or support is None or low is None or open_price is None:
            _reject(self, rejection_reasons=["indicator_unavailable"], row=last)
            return None
        broke_support = low < support - (0.15 * atr)
        reclaimed = price > support and price > open_price
        volume_ok = (_safe_float(last.get("relative_volume"), 0.0) or 0.0) >= self.minimum_relative_volume
        regime_ok = price >= (_safe_float(last.get("ema_200"), price) or price) * 0.94
        checks = {
            "support_break_not_observed": broke_support,
            "support_reclaim_not_confirmed": reclaimed,
            "relative_volume_too_low": volume_ok,
            "regime_alignment_too_low": regime_ok,
        }
        if not all(checks.values()):
            rejection_reasons = _condition_rejections(checks)
            weak = _weak_long_signal(
                self,
                symbol=symbol,
                row=last,
                price=price,
                stop=min(low, price - (1.0 * atr)),
                risk_multiple=max(self.risk_multiple, 1.0),
                rationale="Supervised weak-valid failed-breakdown reversal with real support break/reclaim but incomplete confirmation.",
                confidence=0.50,
                style="reversal",
                setup_type="failed_breakdown_reversal",
                rejection_reasons=rejection_reasons,
                setup_anchor=broke_support and reclaimed,
                extra={"support": support, "weak_signal_kind": "support_reclaim_anchor"},
            )
            if weak is not None:
                self.last_diagnostics = {}
                return weak
            _reject(
                self,
                rejection_reasons=rejection_reasons,
                row=last,
                score=55.0 if broke_support or reclaimed else 41.0,
                measurements={"support": support, "low": low},
            )
            return None
        stop = min(low, price - (1.0 * atr))
        self.last_diagnostics = {}
        return _long_signal(
            self,
            symbol=symbol,
            price=price,
            stop=stop,
            risk_multiple=self.risk_multiple,
            rationale="Failed breakdown reclaimed support with reversal confirmation.",
            confidence=0.62,
            metadata=_metadata(
                row=last,
                style="reversal",
                setup_type="failed_breakdown_reversal",
                risk_reward=self.risk_multiple,
                extra={"support": support},
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
