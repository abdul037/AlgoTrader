"""Session-native features for the intraday research candidates (RESEARCH ONLY).

Every feature reads only bars at or before the latest bar, so a strategy called on a prefix
of a frame sees exactly what the live scanner would have seen at that bar's close.

* Sessions are New York calendar dates; only regular-hours bars (start 09:30 to before 16:00
  ET) count. Alpaca minute frames can carry extended-hours bars: those never form an opening
  range, a VWAP or a volume baseline here, and no signal fires on one.
* Relative volume is time-of-day aware: today's cumulative volume through the current bar
  against the mean cumulative volume through the same minute over the prior sessions.
* VWAP is session-anchored on the typical price (high + low + close) / 3, with the usual
  volume-weighted standard-deviation band.
* ``adjacent_prior_close`` is the previous session's close only when that session is the
  previous weekday. Walk-forward folds drop the embargo day between warm-up and test, so on a
  fold's first test day the frame's "previous session" can be two sessions back; a gap or
  day change measured from it would be fiction. After an exchange holiday it is None too.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

NEW_YORK = "America/New_York"
RTH_OPEN_MINUTE = 9 * 60 + 30
RTH_CLOSE_MINUTE = 16 * 60
# The live bot closes intraday positions this many minutes before the bell
# (``intraday_flatten_minutes_before_close``, default 10): the candidates exit by then.
LIVE_FLATTEN_MINUTE = RTH_CLOSE_MINUTE - 10
BAR_MINUTES = {"5m": 5, "10m": 10, "15m": 15, "30m": 30, "1h": 60}
# Walk-forward warm-up is 5 calendar days (3-5 sessions), so a 10-session baseline is not
# always there; 3 is the least a relative-volume reading is taken from.
MIN_PRIOR_SESSIONS = 3
_NS_PER_MINUTE = 60_000_000_000


def bar_minutes_for(timeframe: str) -> int:
    try:
        return BAR_MINUTES[str(timeframe).strip().lower()]
    except KeyError as exc:
        raise ValueError(f"unsupported intraday timeframe {timeframe!r}") from exc


@dataclass(frozen=True)
class PriorSession:
    day: int  # New York day number (days since 1970-01-01)
    minute: np.ndarray
    cum_volume: np.ndarray
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class SessionView:
    """Today's regular-hours bars through the latest bar, plus the prior sessions."""

    day: int
    minute: np.ndarray  # bar start, minutes after New York midnight
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    prior: tuple[PriorSession, ...]  # oldest first

    @property
    def last_minute(self) -> int:
        return int(self.minute[-1])

    @property
    def adjacent_prior_close(self) -> float | None:
        """The previous session's close when it is the previous weekday, else None."""

        if not self.prior:
            return None
        weekday = (self.day + 3) % 7  # 1970-01-01 was a Thursday; Monday = 0
        expected_gap = 3 if weekday == 0 else 1
        last = self.prior[-1]
        return last.close if self.day - last.day == expected_gap else None

    @property
    def is_first_bar(self) -> bool:
        return len(self.minute) == 1 and self.last_minute == RTH_OPEN_MINUTE

    def vwap_and_sigma(self) -> tuple[np.ndarray, np.ndarray]:
        """Session VWAP per bar and its volume-weighted standard deviation band width."""

        price = (self.high + self.low + self.close) / 3.0
        volume = np.clip(self.volume, 0.0, None)
        cum_volume = np.cumsum(volume)
        cum_pv = np.cumsum(price * volume)
        cum_p2v = np.cumsum(price * price * volume)
        with np.errstate(divide="ignore", invalid="ignore"):
            # Before any volume prints (a zero-volume first bar) the bar's own price stands in.
            vwap = np.where(cum_volume > 0, cum_pv / cum_volume, price)
            variance = np.where(cum_volume > 0, cum_p2v / cum_volume - vwap * vwap, 0.0)
        sigma = np.sqrt(np.clip(np.nan_to_num(variance, nan=0.0), 0.0, None))
        return vwap, sigma

    def cumulative_relative_volume(self, *, min_prior: int = MIN_PRIOR_SESSIONS) -> np.ndarray:
        """Per bar: today's volume so far over the prior sessions' mean through that minute."""

        today = np.cumsum(np.clip(self.volume, 0.0, None))
        if len(self.prior) < min_prior:
            return np.full(len(today), np.nan)
        baselines = np.zeros(len(today))
        for session in self.prior:
            index = np.searchsorted(session.minute, self.minute, side="right") - 1
            baselines += np.where(index >= 0, session.cum_volume[np.clip(index, 0, None)], 0.0)
        baselines /= len(self.prior)
        with np.errstate(divide="ignore", invalid="ignore"):
            return np.where(baselines > 0, today / baselines, np.nan)


def session_view(
    data: pd.DataFrame, *, bar_minutes: int, prior_sessions: int = 10
) -> SessionView | None:
    """Build the view for the latest bar, or None when that bar is not a regular-hours bar."""

    if data is None or len(data) == 0:
        return None
    # A regular-hours-only frame needs ~prior_sessions + 2 days of bars; a frame with
    # extended-hours bars (Alpaca) needs up to a whole day's worth of bars per session.
    for per_day in (int(math.ceil(390 / bar_minutes)), int(math.ceil(1440 / bar_minutes))):
        rows = (prior_sessions + 2) * per_day
        tail = data.iloc[-rows:]
        minute, day = _local_minute_and_day(tail["timestamp"])
        regular = (minute >= RTH_OPEN_MINUTE) & (minute < RTH_CLOSE_MINUTE)
        if not regular[-1]:
            return None
        keep = np.flatnonzero(regular)
        regular_day = day[keep]
        starts = np.flatnonzero(np.r_[True, regular_day[1:] != regular_day[:-1]])
        if len(starts) >= prior_sessions + 1 or rows >= len(data):
            break
    columns = {
        name: tail[name].to_numpy(dtype=float)[keep] for name in ("open", "high", "low", "close")
    }
    volume = tail["volume"].to_numpy(dtype=float)[keep] if "volume" in tail.columns else None
    columns["volume"] = np.zeros(len(keep)) if volume is None else np.nan_to_num(volume, nan=0.0)
    minute = minute[keep]
    bounds = [*starts.tolist(), len(keep)]
    segments = list(zip(bounds[:-1], bounds[1:], strict=True))  # one (first, stop) per session, oldest first
    prior = tuple(
        PriorSession(
            day=int(regular_day[first]),
            minute=minute[first:stop],
            cum_volume=np.cumsum(np.clip(columns["volume"][first:stop], 0.0, None)),
            high=float(np.max(columns["high"][first:stop])),
            low=float(np.min(columns["low"][first:stop])),
            close=float(columns["close"][stop - 1]),
        )
        for first, stop in segments[:-1][-prior_sessions:]
    )
    today = slice(*segments[-1])
    return SessionView(
        day=int(regular_day[-1]),
        minute=minute[today],
        open=columns["open"][today],
        high=columns["high"][today],
        low=columns["low"][today],
        close=columns["close"][today],
        volume=columns["volume"][today],
        prior=prior,
    )


def _local_minute_and_day(timestamps: pd.Series) -> tuple[np.ndarray, np.ndarray]:
    """New York minute-of-day and day number per timestamp (one tz conversion, then ints)."""

    if isinstance(timestamps.dtype, pd.DatetimeTZDtype):
        index = pd.DatetimeIndex(timestamps)
    else:
        index = pd.DatetimeIndex(pd.to_datetime(timestamps, utc=True))
    local = index.tz_convert(NEW_YORK).tz_localize(None).as_unit("ns").asi8 // _NS_PER_MINUTE
    return local % 1440, local // 1440


def hold_bars_until(
    signal_minute: int,
    *,
    bar_minutes: int,
    exit_minute: int = LIVE_FLATTEN_MINUTE,
    entry_delay_bars: int = 1,
) -> int:
    """Bars from the fill to the time stop for a signal on the bar starting at ``signal_minute``.

    The production minute-bar evidence fills a signal at the open of bar N + 1 + delay; the
    engine's time stop exits at the open of the bar ``max_hold_bars`` bars after the fill, here
    the last bar starting at or before ``exit_minute``. 0 or less: too late to enter.
    """

    fill_minute = signal_minute + (1 + max(entry_delay_bars, 0)) * bar_minutes
    return (exit_minute - fill_minute) // bar_minutes
