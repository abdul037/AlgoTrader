"""VWAP reclaim, rejection, and mean-reversion strategy."""

from __future__ import annotations

import pandas as pd

from app.indicators import compute_confluence_score, enrich_technical_indicators, indicator_summary
from app.models.signal import Signal, SignalAction
from app.strategies.base import BaseStrategy
from app.strategies.weak_signals import build_supervised_weak_long_signal


class VWAPReclaimStrategy(BaseStrategy):
    """Trade intraday VWAP reclaims, rejections, and stretch reversions."""

    name = "vwap_reclaim"
    required_bars = 50

    def __init__(self, *, timeframe: str = "5m", relative_volume_floor: float = 1.15):
        self.timeframe = timeframe
        self.relative_volume_floor = relative_volume_floor
        self.last_diagnostics: dict[str, object] | None = None

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> Signal | None:
        self.last_diagnostics = None
        if len(data) < self.required_bars:
            self.last_diagnostics = {"status": "no_signal", "rejection_reasons": ["insufficient_data"]}
            return None

        frame = enrich_technical_indicators(data, timeframe=self.timeframe)
        last = frame.iloc[-1]
        recent = frame.tail(4)
        atr = float(last.get("atr_14") or max(float(last["close"]) * 0.004, 0.01))
        rv = float(last.get("relative_volume") or 0.0)

        bullish_anchor = (
            float(last["close"]) > float(last["vwap"])
            and float(last["ema_9"]) > float(last["ema_20"])
            and recent["low"].min() <= float(last["vwap"])
        )
        volume_ok = rv >= self.relative_volume_floor
        macd_ok = float(last.get("macd_hist") or 0.0) > 0.0
        bullish_reclaim = (
            bullish_anchor
            and volume_ok
            and macd_ok
        )
        if bullish_reclaim:
            entry = float(last["close"])
            stop = float(min(recent["low"].min(), float(last["vwap"]) - atr * 0.35))
            risk = max(entry - stop, atr * 0.75, 0.01)
            target = entry + (risk * 2.1)
            confluence = compute_confluence_score(last, is_short=False)
            return self._build_signal(
                symbol=symbol.upper(),
                strategy_name=self.name,
                action=SignalAction.BUY,
                rationale="Price reclaimed session VWAP with EMA support and expanding volume.",
                confidence=round(min(0.88, 0.58 + confluence * 0.26), 4),
                price=entry,
                stop_loss=stop,
                take_profit=target,
                metadata={
                    "style": "vwap_intraday",
                    "signal_role": "entry_long",
                    "setup_type": "vwap_reclaim",
                    "indicator_confluence_score": round(confluence, 4),
                    "trend_quality": round(min(1.0, confluence + 0.12), 4),
                    "momentum_quality": round(min(1.0, rv / 2.0), 4),
                    "liquidity_quality": round(min(1.0, rv / 2.0), 4),
                    "execution_quality": 0.86,
                    "risk_reward_ratio": round((target - entry) / risk, 2),
                    **indicator_summary(last),
                },
            )

        if bullish_anchor:
            entry = float(last["close"])
            stop = float(min(recent["low"].min(), float(last["vwap"]) - atr * 0.35))
            risk = max(entry - stop, atr * 0.75, 0.01)
            target = entry + (risk * 1.2)
            confluence = compute_confluence_score(last, is_short=False)
            reasons = []
            if not volume_ok:
                reasons.append("relative_volume_too_low")
            if not macd_ok:
                reasons.append("confirmation_too_weak")
            weak = build_supervised_weak_long_signal(
                self,
                symbol=symbol,
                price=entry,
                stop=stop,
                risk_multiple=round((target - entry) / risk, 4),
                rationale="Supervised weak-valid VWAP reclaim with real reclaim anchor but incomplete volume or momentum confirmation.",
                confidence=0.50,
                metadata={
                    "style": "vwap_intraday",
                    "signal_role": "entry_long",
                    "setup_type": "vwap_reclaim",
                    "indicator_confluence_score": round(confluence, 4),
                    "trend_quality": round(min(1.0, confluence + 0.12), 4),
                    "momentum_quality": round(min(1.0, rv / 2.0), 4),
                    "liquidity_quality": round(min(1.0, rv / 2.0), 4),
                    "execution_quality": 0.82,
                    "weak_signal_kind": "vwap_reclaim_anchor",
                    **indicator_summary(last),
                },
                rejection_reasons=reasons or ["confirmation_too_weak"],
                setup_anchor=True,
            )
            if weak is not None:
                return weak

        bearish_rejection = (
            float(last["close"]) < float(last["vwap"])
            and float(last["ema_9"]) < float(last["ema_20"])
            and recent["high"].max() >= float(last["vwap"])
            and rv >= self.relative_volume_floor
            and float(last.get("macd_hist") or 0.0) < 0.0
        )
        if bearish_rejection:
            entry = float(last["close"])
            stop = float(max(recent["high"].max(), float(last["vwap"]) + atr * 0.35))
            risk = max(stop - entry, atr * 0.75, 0.01)
            target = entry - (risk * 2.1)
            confluence = compute_confluence_score(last, is_short=True)
            return self._build_signal(
                symbol=symbol.upper(),
                strategy_name=self.name,
                action=SignalAction.SELL,
                rationale="Price rejected session VWAP with EMA pressure and expanding downside volume.",
                confidence=round(min(0.88, 0.58 + confluence * 0.26), 4),
                price=entry,
                stop_loss=stop,
                take_profit=target,
                metadata={
                    "style": "vwap_intraday",
                    "signal_role": "entry_short",
                    "setup_type": "vwap_rejection",
                    "indicator_confluence_score": round(confluence, 4),
                    "trend_quality": round(min(1.0, confluence + 0.12), 4),
                    "momentum_quality": round(min(1.0, rv / 2.0), 4),
                    "liquidity_quality": round(min(1.0, rv / 2.0), 4),
                    "execution_quality": 0.86,
                    "risk_reward_ratio": round((entry - target) / risk, 2),
                    **indicator_summary(last),
                },
            )

        self.last_diagnostics = {
            "status": "no_signal",
            "rejection_reasons": ["reclaim_not_confirmed"],
            "reason_codes": ["reclaim_not_confirmed"],
            "score": 44.0,
            "measurements": {
                "vwap": float(last["vwap"]),
                "relative_volume": rv,
                "macd_hist": float(last.get("macd_hist") or 0.0),
            },
        }
        return None
