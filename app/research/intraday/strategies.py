"""Five intraday-native research candidates (RESEARCH ONLY, never registered or scanned).

Each fixes its parameters a priori (no fitting to the study's data) and trades at most one
setup per session, entering on a bracketed signal at a bar's close. Every signal carries
``max_hold_bars`` so the engine's time stop exits at the open of the 15:50 ET bar (15:45 on
15m bars), the live close-before-the-bell rule, whatever bars follow. Fills are the engine's:
one bar late (``entry_delay_bars``), bar opens, costs applied.

* ``OpeningRangeBreakoutStrategy`` -- first close beyond the opening range (15/30 min) on an
  active open (opening-range relative volume >= 1), stop at the far side of the range, 2R.
* ``GapOpenStrategy`` -- an overnight gap of at least 1% decided by the first regular-hours
  bar: ``go`` buys a gap up that opens green (stop: that bar's low, 2R); ``fade`` buys a gap
  down that opens green (stop: that bar's low, target: the prior close, at least 1R).
* ``RelativeVolumeMomentumStrategy`` -- 10:00-14:00 ET, the first new session high on a stock
  in play (time-of-day relative volume >= 1.5, up >= 1% on the day, above VWAP), stop at
  VWAP, 2R.
* ``VWAPDeviationReversionStrategy`` -- 10:00-15:00 ET, a green bar after a probe below VWAP
  minus 2 session sigma, target VWAP, stop as far below as the target is above (1:1).
* ``LateDayMomentumStrategy`` -- fills at 15:00 ET on a day up >= 0.5% and above VWAP; stop at
  VWAP, out by the 15:50 time stop (the 3R target rarely binds).

``direction="short"`` evaluates the mirror image (prices negated) and emits bracketed
``entry_short`` SELLs, which only the harness's short engine config opens (harness.py);
the long evidence path only ever opens BUYs. Shorts are a diagnostic: the bot does not short.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import numpy as np
import pandas as pd

from app.models.signal import Signal, SignalAction
from app.research.intraday.session import (
    RTH_OPEN_MINUTE,
    PriorSession,
    SessionView,
    bar_minutes_for,
    hold_bars_until,
    session_view,
)
from app.strategies.base import BaseStrategy

DIRECTIONS = ("long", "short")


def mirror_view(view: SessionView) -> SessionView:
    """The same session with prices negated: a long rule on it is the short rule on ``view``."""

    prior = tuple(
        replace(s, high=-s.low, low=-s.high, close=-s.close) for s in view.prior
    )
    return replace(
        view, open=-view.open, high=-view.low, low=-view.high, close=-view.close, prior=prior
    )


def _change(value: float, reference: float) -> float:
    """Relative change that keeps its sign meaning on a mirrored (negated) view."""

    return (value - reference) / abs(reference) if reference else 0.0


class IntradayCandidate(BaseStrategy):
    """Shared plumbing: build the session view, mirror for shorts, emit a bracket signal."""

    name = "intraday_candidate"
    required_bars = 2
    setup_type = "intraday_candidate"

    def __init__(
        self,
        *,
        timeframe: str = "5m",
        direction: str = "long",
        name: str | None = None,
        entry_delay_bars: int = 1,
        prior_sessions: int = 10,
    ):
        if direction not in DIRECTIONS:
            raise ValueError(f"direction must be one of {DIRECTIONS}, got {direction!r}")
        self.timeframe = timeframe
        self.bar_minutes = bar_minutes_for(timeframe)
        self.direction = direction
        self.entry_delay_bars = entry_delay_bars
        self.prior_sessions = prior_sessions
        if name:
            self.name = name

    def generate_signal(self, data: pd.DataFrame, symbol: str) -> Signal | None:
        view = session_view(data, bar_minutes=self.bar_minutes, prior_sessions=self.prior_sessions)
        if view is None:
            return None
        short = self.direction == "short"
        plan = self._long_plan(mirror_view(view) if short else view)
        if plan is None:
            return None
        entry, stop, target, extra = plan
        if short:
            entry, stop, target = -entry, -stop, -target
        hold = hold_bars_until(
            view.last_minute, bar_minutes=self.bar_minutes, entry_delay_bars=self.entry_delay_bars
        )
        if hold <= 0:
            return None
        risk = abs(entry - stop)
        return self._build_signal(
            symbol=symbol.upper(),
            strategy_name=self.name,
            action=SignalAction.SELL if short else SignalAction.BUY,
            rationale=f"Research candidate {self.name} ({self.direction}): {self.setup_type}.",
            confidence=0.55,
            price=float(entry),
            stop_loss=float(stop),
            take_profit=float(target),
            metadata={
                "style": "intraday_research",
                "research_only": True,
                "signal_role": "entry_short" if short else "entry_long",
                "setup_type": self.setup_type,
                "timeframe": self.timeframe,
                "max_hold_bars": int(hold),
                "risk_reward_ratio": round(abs(target - entry) / risk, 2) if risk > 0 else None,
                # A short's feature values are read off the mirrored view (prices negated).
                "features_mirrored": short,
                **{key: round(float(value), 6) for key, value in extra.items()},
            },
        )

    def _long_plan(self, view: SessionView) -> tuple[float, float, float, dict[str, Any]] | None:
        """(entry, stop, target, extra) for a long on ``view``, or None."""

        raise NotImplementedError


class OpeningRangeBreakoutStrategy(IntradayCandidate):
    name = "research_orb"
    setup_type = "opening_range_breakout"

    def __init__(
        self,
        *,
        range_minutes: int = 30,
        min_range_rvol: float = 1.0,
        reward_risk: float = 2.0,
        last_entry_minute: int = 12 * 60,
        **kwargs: Any,
    ):
        super().__init__(**kwargs)
        self.range_minutes = range_minutes
        self.min_range_rvol = min_range_rvol
        self.reward_risk = reward_risk
        self.last_entry_minute = last_entry_minute

    def _long_plan(self, view: SessionView):
        range_end = RTH_OPEN_MINUTE + self.range_minutes
        in_range = view.minute < range_end
        if not in_range.any() or in_range[-1] or view.last_minute >= self.last_entry_minute:
            return None
        range_high = float(view.high[in_range].max())
        range_low = float(view.low[in_range].min())
        breaks = view.close[~in_range] > range_high
        if not breaks[-1] or breaks[:-1].any():  # the session's first close beyond the range
            return None
        range_rvol = view.cumulative_relative_volume()[in_range][-1]
        if not range_rvol >= self.min_range_rvol:  # NaN (too little history) fails too
            return None
        entry = float(view.close[-1])
        risk = entry - range_low
        if risk <= 0:
            return None
        extra = {"range_high": range_high, "range_low": range_low, "range_rvol": range_rvol}
        return entry, range_low, entry + self.reward_risk * risk, extra


class GapOpenStrategy(IntradayCandidate):
    name = "research_gap"
    setup_type = "overnight_gap"

    def __init__(
        self,
        *,
        mode: str = "go",
        min_gap_pct: float = 1.0,
        reward_risk: float = 2.0,
        min_fade_reward_risk: float = 1.0,
        **kwargs: Any,
    ):
        super().__init__(**kwargs)
        if mode not in ("go", "fade"):
            raise ValueError(f"mode must be 'go' or 'fade', got {mode!r}")
        self.mode = mode
        self.setup_type = f"gap_{mode}"
        self.min_gap = min_gap_pct / 100.0
        self.reward_risk = reward_risk
        self.min_fade_reward_risk = min_fade_reward_risk

    def _long_plan(self, view: SessionView):
        prior_close = view.adjacent_prior_close
        if not view.is_first_bar or prior_close is None:
            return None
        gap = _change(float(view.open[0]), prior_close)
        entry, stop = float(view.close[0]), float(view.low[0])
        risk = entry - stop
        if not view.close[0] > view.open[0] or risk <= 0:
            return None
        if self.mode == "go":
            if gap < self.min_gap:
                return None
            return entry, stop, entry + self.reward_risk * risk, {"gap": gap}
        if gap > -self.min_gap or (prior_close - entry) < self.min_fade_reward_risk * risk:
            return None
        return entry, stop, float(prior_close), {"gap": gap}


class RelativeVolumeMomentumStrategy(IntradayCandidate):
    name = "research_rvol_momentum"
    setup_type = "relative_volume_momentum"

    def __init__(
        self,
        *,
        min_rvol: float = 1.5,
        min_day_change_pct: float = 1.0,
        first_minute: int = 10 * 60,
        last_entry_minute: int = 14 * 60,
        min_stop_pct: float = 0.25,
        reward_risk: float = 2.0,
        **kwargs: Any,
    ):
        super().__init__(**kwargs)
        self.min_rvol = min_rvol
        self.min_day_change = min_day_change_pct / 100.0
        self.first_minute = first_minute
        self.last_entry_minute = last_entry_minute
        self.min_stop = min_stop_pct / 100.0
        self.reward_risk = reward_risk

    def _long_plan(self, view: SessionView):
        prior_close = view.adjacent_prior_close
        if prior_close is None or not self.first_minute <= view.last_minute < self.last_entry_minute:
            return None
        vwap, _ = view.vwap_and_sigma()
        rvol = view.cumulative_relative_volume()
        high_before = np.concatenate(([np.inf], np.maximum.accumulate(view.high)[:-1]))
        day_change = (view.close - prior_close) / abs(prior_close)
        qualifies = (
            (view.minute >= self.first_minute)
            & (rvol >= self.min_rvol)
            & (view.close > vwap)
            & (day_change >= self.min_day_change)
            & (view.close > high_before)
        )
        if not qualifies[-1] or qualifies[:-1].any():  # the session's first qualifying bar
            return None
        entry = float(view.close[-1])
        stop = min(float(vwap[-1]), entry - self.min_stop * abs(entry))
        extra = {"rvol": rvol[-1], "day_change": day_change[-1], "vwap": vwap[-1]}
        return entry, stop, entry + self.reward_risk * (entry - stop), extra


class VWAPDeviationReversionStrategy(IntradayCandidate):
    name = "research_vwap_reversion"
    setup_type = "vwap_deviation_reversion"

    def __init__(
        self,
        *,
        band_sigma: float = 2.0,
        first_minute: int = 10 * 60,
        last_entry_minute: int = 15 * 60,
        min_reward_pct: float = 0.2,
        **kwargs: Any,
    ):
        super().__init__(**kwargs)
        self.band_sigma = band_sigma
        self.first_minute = first_minute
        self.last_entry_minute = last_entry_minute
        self.min_reward = min_reward_pct / 100.0

    def _long_plan(self, view: SessionView):
        if not self.first_minute <= view.last_minute < self.last_entry_minute:
            return None
        vwap, sigma = view.vwap_and_sigma()
        lower = float(vwap[-1] - self.band_sigma * sigma[-1])
        entry, target = float(view.close[-1]), float(vwap[-1])
        reward = target - entry
        if not (view.low[-1] <= lower and view.close[-1] > view.open[-1]):
            return None
        if reward < self.min_reward * abs(entry):
            return None
        return entry, entry - reward, target, {"vwap": target, "sigma": sigma[-1]}


class LateDayMomentumStrategy(IntradayCandidate):
    name = "research_late_day_momentum"
    setup_type = "late_day_momentum"

    def __init__(
        self,
        *,
        fill_minute: int = 15 * 60,
        min_day_change_pct: float = 0.5,
        min_stop_pct: float = 0.25,
        reward_risk: float = 3.0,
        **kwargs: Any,
    ):
        super().__init__(**kwargs)
        self.fill_minute = fill_minute
        self.min_day_change = min_day_change_pct / 100.0
        self.min_stop = min_stop_pct / 100.0
        self.reward_risk = reward_risk

    def _long_plan(self, view: SessionView):
        signal_minute = self.fill_minute - (1 + max(self.entry_delay_bars, 0)) * self.bar_minutes
        prior_close = view.adjacent_prior_close
        if prior_close is None or view.last_minute != signal_minute:
            return None
        vwap, _ = view.vwap_and_sigma()
        entry = float(view.close[-1])
        day_change = _change(entry, prior_close)
        if day_change < self.min_day_change or not entry > vwap[-1]:
            return None
        stop = min(float(vwap[-1]), entry - self.min_stop * abs(entry))
        extra = {"day_change": day_change, "vwap": vwap[-1]}
        return entry, stop, entry + self.reward_risk * (entry - stop), extra


# The study's variants, fixed before any backtest: (class, constructor kwargs).
CANDIDATE_VARIANTS: dict[str, tuple[type[IntradayCandidate], dict[str, Any]]] = {
    "research_orb30": (OpeningRangeBreakoutStrategy, {"range_minutes": 30}),
    "research_orb15": (OpeningRangeBreakoutStrategy, {"range_minutes": 15}),
    "research_gap_go": (GapOpenStrategy, {"mode": "go"}),
    "research_gap_fade": (GapOpenStrategy, {"mode": "fade"}),
    "research_rvol_momentum": (RelativeVolumeMomentumStrategy, {}),
    "research_vwap_reversion": (VWAPDeviationReversionStrategy, {}),
    "research_late_day_momentum": (LateDayMomentumStrategy, {}),
}


def build_candidate(
    variant: str, *, timeframe: str, direction: str = "long", entry_delay_bars: int = 1
) -> IntradayCandidate:
    cls, kwargs = CANDIDATE_VARIANTS[variant]
    return cls(
        timeframe=timeframe,
        direction=direction,
        name=variant,
        entry_delay_bars=entry_delay_bars,
        **kwargs,
    )


__all__ = [
    "CANDIDATE_VARIANTS",
    "GapOpenStrategy",
    "IntradayCandidate",
    "LateDayMomentumStrategy",
    "OpeningRangeBreakoutStrategy",
    "PriorSession",
    "RelativeVolumeMomentumStrategy",
    "VWAPDeviationReversionStrategy",
    "build_candidate",
    "mirror_view",
]
