"""High-confluence RSI + VWAP + EMA strategy."""

from __future__ import annotations

from typing import Any

import pandas as pd

from app.indicators import compute_confluence_score, enrich_technical_indicators, indicator_summary
from app.models.signal import Signal, SignalAction
from app.strategies.base import BaseStrategy


class RSIVWAPEMAConfluenceStrategy(BaseStrategy):
    """Only trigger when RSI, VWAP, EMA, and volume conditions align tightly."""

    name = "rsi_vwap_ema_confluence"
    required_bars = 80

    def __init__(
        self,
        *,
        timeframe: str = "5m",
        minimum_relative_volume: float = 1.25,
        minimum_confluence_score: float = 0.84,
        minimum_adx: float = 20.0,
        rsi_long_min: float = 54.0,
        rsi_long_max: float = 66.0,
        rsi_short_min: float = 34.0,
        rsi_short_max: float = 46.0,
        max_extension_atr: float = 1.6,
        minimum_body_to_range: float = 0.32,
        minimum_close_location: float = 0.62,
    ):
        self.timeframe = timeframe
        self.minimum_relative_volume = minimum_relative_volume
        self.minimum_confluence_score = minimum_confluence_score
        self.minimum_adx = minimum_adx
        self.rsi_long_min = rsi_long_min
        self.rsi_long_max = rsi_long_max
        self.rsi_short_min = rsi_short_min
        self.rsi_short_max = rsi_short_max
        self.max_extension_atr = max_extension_atr
        self.minimum_body_to_range = minimum_body_to_range
        self.minimum_close_location = minimum_close_location
        self.last_diagnostics: dict[str, Any] | None = None

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> Signal | None:
        self.last_diagnostics = None
        if len(data) < self.required_bars:
            self.last_diagnostics = {
                "status": "insufficient_bars",
                "symbol": symbol.upper(),
                "strategy_name": self.name,
                "timeframe": self.timeframe,
                "score": None,
                "rejection_reasons": ["insufficient_bars"],
                "measurements": {
                    "bars_available": len(data),
                    "bars_required": self.required_bars,
                },
            }
            return None

        frame = enrich_technical_indicators(data, timeframe=self.timeframe)
        last = frame.iloc[-1]
        prev = frame.iloc[-2]
        atr = float(last.get("atr_14") or max(float(last["close"]) * 0.006, 0.01))
        rv = float(last.get("relative_volume") or 0.0)
        rsi = float(last.get("rsi_14") or 50.0)
        adx = float(last.get("adx_14") or 0.0)
        confluence_long = compute_confluence_score(last, is_short=False)
        confluence_short = compute_confluence_score(last, is_short=True)
        quality = self._quality_metrics(last, atr=atr)
        volume_context = self._volume_context(frame)
        thresholds = self._threshold_profile()

        long_diagnostics = self._side_diagnostics(
            side="long",
            last=last,
            prev=prev,
            rsi=rsi,
            rv=rv,
            adx=adx,
            confluence=confluence_long,
            quality=quality,
            volume_context=volume_context,
            atr=atr,
            thresholds=thresholds,
        )
        short_diagnostics = self._side_diagnostics(
            side="short",
            last=last,
            prev=prev,
            rsi=rsi,
            rv=rv,
            adx=adx,
            confluence=confluence_short,
            quality=quality,
            volume_context=volume_context,
            atr=atr,
            thresholds=thresholds,
        )

        long_conditions = long_diagnostics["passed"]
        if long_conditions:
            entry = float(last["close"])
            stop = float(min(last.get("vwap") or last["low"], last.get("ema_20") or last["low"], entry - atr))
            risk = max(entry - stop, atr * 0.9, 0.01)
            target = entry + (risk * 2.5)
            quality_score = self._quality_score(confluence_long, rv=rv, adx=adx, quality=quality)
            breakout_confirmed = bool(long_diagnostics["measurements"].get("breakout_confirmed"))
            confidence = round(min(0.96, 0.70 + quality_score * 0.24 - (0.03 if not breakout_confirmed else 0.0)), 4)
            return Signal(
                symbol=symbol.upper(),
                strategy_name=self.name,
                action=SignalAction.BUY,
                rationale=(
                    "A+ RSI/VWAP/EMA confluence long: trend stack, VWAP support, controlled RSI, "
                    + ("breakout confirmed" if breakout_confirmed else "near-breakout pressure")
                    + ", RVOL, ADX, candle quality, and entry extension aligned."
                ),
                confidence=confidence,
                price=entry,
                stop_loss=stop,
                take_profit=target,
                metadata={
                    "style": "confluence",
                    "signal_role": "entry_long",
                    "setup_type": "rsi_vwap_ema_confluence",
                    "primary_strategy": True,
                    "a_plus_setup": True,
                    "entry_trigger": "breakout_confirmed" if breakout_confirmed else "near_breakout",
                    "indicator_confluence_score": round(confluence_long, 4),
                    "confluence_quality_score": round(quality_score, 4),
                    "trend_quality": round(min(1.0, confluence_long + 0.18), 4),
                    "momentum_quality": round(min(1.0, (rsi - 50.0) / 18.0), 4),
                    "liquidity_quality": round(min(1.0, rv / 2.0), 4),
                    "execution_quality": 0.9,
                    "strategy_diagnostics": long_diagnostics["measurements"],
                    **quality,
                    "risk_reward_ratio": round((target - entry) / risk, 2),
                    **indicator_summary(last),
                },
            )

        short_conditions = short_diagnostics["passed"]
        if short_conditions:
            entry = float(last["close"])
            stop = float(max(last.get("vwap") or last["high"], last.get("ema_20") or last["high"], entry + atr))
            risk = max(stop - entry, atr * 0.9, 0.01)
            target = entry - (risk * 2.5)
            quality_score = self._quality_score(confluence_short, rv=rv, adx=adx, quality=quality)
            breakdown_confirmed = bool(short_diagnostics["measurements"].get("breakout_confirmed"))
            confidence = round(min(0.96, 0.70 + quality_score * 0.24 - (0.03 if not breakdown_confirmed else 0.0)), 4)
            return Signal(
                symbol=symbol.upper(),
                strategy_name=self.name,
                action=SignalAction.SELL,
                rationale=(
                    "A+ RSI/VWAP/EMA confluence short: trend stack, VWAP rejection, controlled RSI, "
                    + ("breakdown confirmed" if breakdown_confirmed else "near-breakdown pressure")
                    + ", RVOL, ADX, candle quality, and entry extension aligned."
                ),
                confidence=confidence,
                price=entry,
                stop_loss=stop,
                take_profit=target,
                metadata={
                    "style": "confluence",
                    "signal_role": "entry_short",
                    "setup_type": "rsi_vwap_ema_confluence",
                    "primary_strategy": True,
                    "a_plus_setup": True,
                    "entry_trigger": "breakdown_confirmed" if breakdown_confirmed else "near_breakdown",
                    "indicator_confluence_score": round(confluence_short, 4),
                    "confluence_quality_score": round(quality_score, 4),
                    "trend_quality": round(min(1.0, confluence_short + 0.18), 4),
                    "momentum_quality": round(min(1.0, (50.0 - rsi) / 18.0), 4),
                    "liquidity_quality": round(min(1.0, rv / 2.0), 4),
                    "execution_quality": 0.9,
                    "strategy_diagnostics": short_diagnostics["measurements"],
                    **quality,
                    "risk_reward_ratio": round((entry - target) / risk, 2),
                    **indicator_summary(last),
                },
            )

        self.last_diagnostics = self._select_near_miss(symbol, long_diagnostics, short_diagnostics)
        return None

    @staticmethod
    def _quality_metrics(last: pd.Series, *, atr: float) -> dict[str, float]:
        high = float(last.get("high") or last["close"])
        low = float(last.get("low") or last["close"])
        close = float(last["close"])
        open_price = float(last.get("open") or close)
        candle_range = max(high - low, 0.01)
        anchor_levels = [
            float(last.get("ema_9") or close),
            float(last.get("ema_20") or close),
            float(last.get("vwap") or close),
        ]
        extension = min(abs(close - level) for level in anchor_levels) / max(atr, 0.01)
        return {
            "extension_atr": round(extension, 4),
            "body_to_range": round(abs(close - open_price) / candle_range, 4),
            "close_location": round((close - low) / candle_range, 4),
            "close_location_short": round((high - close) / candle_range, 4),
        }

    @staticmethod
    def _volume_context(frame: pd.DataFrame) -> dict[str, float]:
        recent_volume = frame["volume"].tail(20).astype("float64")
        if recent_volume.empty:
            return {
                "last_volume": 0.0,
                "average_volume_20": 0.0,
                "median_volume_20": 0.0,
                "session_volume_ratio": 0.0,
            }
        last_volume = float(recent_volume.iloc[-1] or 0.0)
        average_volume = float(recent_volume.mean() or 0.0)
        median_volume = float(recent_volume.median() or 0.0)
        session_ratio = last_volume / max(median_volume, 0.01)
        return {
            "last_volume": round(last_volume, 2),
            "average_volume_20": round(average_volume, 2),
            "median_volume_20": round(median_volume, 2),
            "session_volume_ratio": round(session_ratio, 4),
        }

    @staticmethod
    def _quality_score(confluence: float, *, rv: float, adx: float, quality: dict[str, float]) -> float:
        extension_score = max(0.0, min(1.0, 1.0 - (quality["extension_atr"] / 2.2)))
        return round(
            min(
                1.0,
                (confluence * 0.38)
                + (min(rv / 2.2, 1.0) * 0.20)
                + (min(adx / 35.0, 1.0) * 0.18)
                + (quality["body_to_range"] * 0.12)
                + (extension_score * 0.12),
            ),
            4,
        )

    def _side_diagnostics(
        self,
        *,
        side: str,
        last: pd.Series,
        prev: pd.Series,
        rsi: float,
        rv: float,
        adx: float,
        confluence: float,
        quality: dict[str, float],
        volume_context: dict[str, float],
        atr: float,
        thresholds: dict[str, float],
    ) -> dict[str, Any]:
        is_long = side == "long"
        current_price = float(last["close"])
        ema20 = float(last.get("ema_20") or current_price)
        vwap = float(last.get("vwap") or current_price)
        breakout_level = float(last.get("range_high_20") or prev["high"]) if is_long else float(last.get("range_low_20") or prev["low"])
        breakout_ready, breakout_gap_atr, breakout_confirmed = self._breakout_ready(
            side=side,
            close=current_price,
            trigger=breakout_level,
            atr=atr,
            tolerance_atr=thresholds["breakout_tolerance_atr"],
        )
        volume_ready, volume_mode = self._volume_ready(
            rv=rv,
            breakout_gap_atr=breakout_gap_atr,
            volume_context=volume_context,
            thresholds=thresholds,
        )
        checks = [
            (
                "price_vs_vwap_ok",
                float(last["close"]) > float(last["vwap"]) if is_long else float(last["close"]) < float(last["vwap"]),
                "close_below_vwap" if is_long else "close_above_vwap",
            ),
            (
                "ema_stack_ok",
                float(last["ema_9"]) > float(last["ema_20"]) > float(last["ema_50"])
                if is_long
                else float(last["ema_9"]) < float(last["ema_20"]) < float(last["ema_50"]),
                "ema_stack_not_bullish" if is_long else "ema_stack_not_bearish",
            ),
            (
                "rsi_band_ok",
                self.rsi_long_min <= rsi <= self.rsi_long_max
                if is_long
                else self.rsi_short_min <= rsi <= self.rsi_short_max,
                "rsi_not_in_long_band" if is_long else "rsi_not_in_short_band",
            ),
            (
                "macd_hist_ok",
                float(last.get("macd_hist") or 0.0) > 0.0 if is_long else float(last.get("macd_hist") or 0.0) < 0.0,
                "macd_hist_not_positive" if is_long else "macd_hist_not_negative",
            ),
            (
                "relative_volume_ok",
                volume_ready,
                "relative_volume_too_low",
            ),
            (
                "breakout_level_ok",
                breakout_ready,
                "breakout_level_not_cleared" if is_long else "breakdown_level_not_cleared",
            ),
            (
                "adx_ok",
                adx >= self.minimum_adx,
                "adx_too_low",
            ),
            (
                "confluence_score_ok",
                confluence >= thresholds["minimum_confluence_score"],
                "confluence_score_too_low",
            ),
            (
                "extension_ok",
                quality["extension_atr"] <= self.max_extension_atr,
                "entry_too_extended",
            ),
            (
                "body_to_range_ok",
                quality["body_to_range"] >= thresholds["minimum_body_to_range"],
                "candle_body_too_small",
            ),
            (
                "close_location_ok",
                quality["close_location"] >= thresholds["minimum_close_location"]
                if is_long
                else quality["close_location_short"] >= thresholds["minimum_close_location"],
                "close_location_too_low" if is_long else "close_location_short_too_low",
            ),
            (
                "ema_9_slope_ok",
                float(last.get("ema_9_slope") or 0.0) > 0.0 if is_long else float(last.get("ema_9_slope") or 0.0) < 0.0,
                "ema_9_slope_not_positive" if is_long else "ema_9_slope_not_negative",
            ),
            (
                "ema_20_slope_ok",
                float(last.get("ema_20_slope") or 0.0) > 0.0 if is_long else float(last.get("ema_20_slope") or 0.0) < 0.0,
                "ema_20_slope_not_positive" if is_long else "ema_20_slope_not_negative",
            ),
        ]
        passed_checks = [name for name, passed, _ in checks if passed]
        rejection_reasons = [fail_code for _, passed, fail_code in checks if not passed]
        total_checks = len(checks)
        pass_ratio = len(passed_checks) / max(total_checks, 1)
        indicative_plan = self._indicative_trade_plan(
            side=side,
            current_price=current_price,
            breakout_level=breakout_level,
            breakout_confirmed=breakout_confirmed,
            atr=atr,
            ema20=ema20,
            vwap=vwap,
        )
        effective_volume_strength = rv / max(thresholds["minimum_relative_volume"], 0.01)
        if thresholds["timeframe_profile"] != "strict_intraday":
            relaxed_strength = min(
                float(volume_context.get("session_volume_ratio") or 0.0)
                / max(thresholds["session_volume_floor"], 0.01),
                1.15,
            ) * 0.95
            if breakout_gap_atr <= thresholds["volume_relaxation_gap_atr"]:
                effective_volume_strength = max(effective_volume_strength, relaxed_strength)
        quality_score = (
            (pass_ratio * 0.65)
            + (min(confluence / max(thresholds["minimum_confluence_score"], 0.01), 1.2) / 1.2 * 0.15)
            + (min(effective_volume_strength, 1.2) / 1.2 * 0.10)
            + (min(adx / max(self.minimum_adx, 0.01), 1.2) / 1.2 * 0.10)
        )
        measurements = {
            "side": side,
            "current_price": round(current_price, 4),
            "atr": round(float(atr), 4),
            "ema20": round(ema20, 4),
            "vwap": round(vwap, 4),
            "rsi": round(rsi, 4),
            "relative_volume": round(rv, 4),
            **volume_context,
            "adx": round(adx, 4),
            "indicator_confluence_score": round(confluence, 4),
            "breakout_level": round(breakout_level, 4),
            "breakout_gap_atr": round(breakout_gap_atr, 4),
            "breakout_tolerance_atr": thresholds["breakout_tolerance_atr"],
            "breakout_confirmed": breakout_confirmed,
            "extension_atr": quality["extension_atr"],
            "body_to_range": quality["body_to_range"],
            "close_location": quality["close_location"],
            "close_location_short": quality["close_location_short"],
            "minimum_relative_volume": thresholds["minimum_relative_volume"],
            "minimum_relative_volume_relaxed": thresholds["minimum_relative_volume_relaxed"],
            "session_volume_floor": thresholds["session_volume_floor"],
            "volume_relaxation_gap_atr": thresholds["volume_relaxation_gap_atr"],
            "volume_check_mode": volume_mode,
            **indicative_plan,
            "minimum_confluence_score": thresholds["minimum_confluence_score"],
            "minimum_adx": self.minimum_adx,
            "minimum_body_to_range": thresholds["minimum_body_to_range"],
            "minimum_close_location": thresholds["minimum_close_location"],
            "max_extension_atr": self.max_extension_atr,
            "timeframe_profile": thresholds["timeframe_profile"],
            "pass_ratio": round(pass_ratio, 4),
            "passed_checks": len(passed_checks),
            "total_checks": total_checks,
        }
        return {
            "side": side,
            "passed": not rejection_reasons,
            "score": round(min(99.0, quality_score * 100.0), 2),
            "rejection_reasons": rejection_reasons,
            "reason_codes": [*passed_checks, *rejection_reasons],
            "measurements": measurements,
        }

    def _threshold_profile(self) -> dict[str, float]:
        timeframe = str(self.timeframe or "").lower()
        profile = {
            "minimum_relative_volume": self.minimum_relative_volume,
            "minimum_relative_volume_relaxed": self.minimum_relative_volume,
            "minimum_confluence_score": self.minimum_confluence_score,
            "minimum_body_to_range": self.minimum_body_to_range,
            "minimum_close_location": self.minimum_close_location,
            "session_volume_floor": self.minimum_relative_volume,
            "volume_relaxation_gap_atr": 0.0,
            "breakout_tolerance_atr": 0.0,
            "timeframe_profile": "strict_intraday",
        }
        if timeframe in {"1h", "60m"}:
            profile.update(
                {
                    "minimum_relative_volume": round(max(1.08, self.minimum_relative_volume - 0.17), 4),
                    "minimum_relative_volume_relaxed": 1.03,
                    "minimum_confluence_score": round(max(0.80, self.minimum_confluence_score - 0.03), 4),
                    "minimum_body_to_range": round(max(0.28, self.minimum_body_to_range - 0.04), 4),
                    "minimum_close_location": round(max(0.58, self.minimum_close_location - 0.04), 4),
                    "session_volume_floor": 0.98,
                    "volume_relaxation_gap_atr": 0.12,
                    "breakout_tolerance_atr": 0.20,
                    "timeframe_profile": "swing_hourly",
                }
            )
        elif timeframe in {"1d", "1day", "day"}:
            profile.update(
                {
                    "minimum_relative_volume": 1.05,
                    "minimum_relative_volume_relaxed": 1.00,
                    "minimum_confluence_score": round(max(0.78, self.minimum_confluence_score - 0.04), 4),
                    "minimum_body_to_range": round(max(0.24, self.minimum_body_to_range - 0.06), 4),
                    "minimum_close_location": round(max(0.56, self.minimum_close_location - 0.05), 4),
                    "session_volume_floor": 0.95,
                    "volume_relaxation_gap_atr": 0.18,
                    "breakout_tolerance_atr": 0.35,
                    "timeframe_profile": "position_daily",
                }
            )
        return profile

    @staticmethod
    def _volume_ready(
        *,
        rv: float,
        breakout_gap_atr: float,
        volume_context: dict[str, float],
        thresholds: dict[str, float],
    ) -> tuple[bool, str]:
        strict_ready = rv >= thresholds["minimum_relative_volume"]
        if strict_ready:
            return True, "strict_relative_volume"
        if thresholds["timeframe_profile"] == "strict_intraday":
            return False, "strict_relative_volume"
        session_ratio = float(volume_context.get("session_volume_ratio") or 0.0)
        session_aware_ready = (
            rv >= thresholds["minimum_relative_volume_relaxed"]
            and session_ratio >= thresholds["session_volume_floor"]
            and breakout_gap_atr <= thresholds["volume_relaxation_gap_atr"]
        )
        if session_aware_ready:
            return True, "session_aware_relaxed"
        return False, "strict_relative_volume"

    @staticmethod
    def _indicative_trade_plan(
        *,
        side: str,
        current_price: float,
        breakout_level: float,
        breakout_confirmed: bool,
        atr: float,
        ema20: float,
        vwap: float,
    ) -> dict[str, float | str]:
        is_long = side == "long"
        entry = current_price if breakout_confirmed else breakout_level
        if is_long:
            stop = min(vwap, ema20, entry - atr)
            risk = max(entry - stop, atr * 0.9, 0.01)
            target = entry + (risk * 2.5)
        else:
            stop = max(vwap, ema20, entry + atr)
            risk = max(stop - entry, atr * 0.9, 0.01)
            target = entry - (risk * 2.5)
        target_move_pct = abs(target - entry) / max(entry, 0.01) * 100.0
        return {
            "watchlist_trigger": "breakout_confirmed" if breakout_confirmed and is_long else
            "breakdown_confirmed" if breakout_confirmed else
            "breakout_above" if is_long else "breakdown_below",
            "indicative_entry": round(entry, 4),
            "indicative_stop": round(stop, 4),
            "indicative_target": round(target, 4),
            "indicative_rr": round(abs(target - entry) / max(abs(entry - stop), 0.01), 2),
            "indicative_target_move_pct": round(target_move_pct, 2),
        }

    @staticmethod
    def _breakout_ready(
        *,
        side: str,
        close: float,
        trigger: float,
        atr: float,
        tolerance_atr: float,
    ) -> tuple[bool, float, bool]:
        normalized_atr = max(float(atr), 0.01)
        if side == "long":
            if close > trigger:
                return True, 0.0, True
            gap_atr = max(trigger - close, 0.0) / normalized_atr
            return gap_atr <= tolerance_atr, gap_atr, False
        if close < trigger:
            return True, 0.0, True
        gap_atr = max(close - trigger, 0.0) / normalized_atr
        return gap_atr <= tolerance_atr, gap_atr, False

    def _select_near_miss(
        self,
        symbol: str,
        long_diagnostics: dict[str, Any],
        short_diagnostics: dict[str, Any],
    ) -> dict[str, Any]:
        best = long_diagnostics if float(long_diagnostics["score"]) >= float(short_diagnostics["score"]) else short_diagnostics
        alternate = short_diagnostics if best is long_diagnostics else long_diagnostics
        return {
            "status": "no_signal",
            "symbol": symbol.upper(),
            "strategy_name": self.name,
            "timeframe": self.timeframe,
            "score": best["score"],
            "rejection_reasons": list(best["rejection_reasons"]),
            "reason_codes": list(best["reason_codes"])[:12],
            "measurements": {
                **best["measurements"],
                "near_miss_side": best["side"],
                "alternate_side": alternate["side"],
                "alternate_side_score": alternate["score"],
            },
        }
