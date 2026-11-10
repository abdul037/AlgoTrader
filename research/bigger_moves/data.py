"""Bars for the bigger-moves study: cleaned daily segments, weekly bars, index membership.

The price cache is the pullback study's (``research/pullback/cli.py data``), read-only:
``prices/`` (yfinance ``auto_adjust=True`` daily bars), ``raw/`` (unadjusted, used only to find
corporate-action breaks) and ``constituents/members.csv`` (today's members, sector, "date
added"). Everything in it is untrusted data; run with ``python -I``.

Cleaning is the pullback study's ``clean_symbol``, unchanged, plus the momentum study's extra
HWM break. A break splits a symbol's history into segments: indicators restart after it, and a
trade cannot cross it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from research.momentum.data import EXTRA_BREAKS
from research.pullback import data as pullback_data

EVAL_START = pd.Timestamp("2011-01-03")
MAIN_END = pd.Timestamp("2025-10-31")
HOLDOUT_START = pd.Timestamp("2025-11-03")
LIVE24 = ["AAPL", "ADBE", "AMD", "AMZN", "AVGO", "BAC", "COST", "CRM", "CSCO", "DIS", "GOOGL",
          "INTC", "IWM", "JPM", "META", "MSFT", "NFLX", "NVDA", "PYPL", "QQQ", "SPY", "TSLA",
          "WMT", "XOM"]
LIVE24_ETFS = ("IWM", "QQQ", "SPY")
SUB_PERIODS = (
    ("2011-2015", pd.Timestamp("2011-01-01"), pd.Timestamp("2015-12-31")),
    ("2016-2020", pd.Timestamp("2016-01-01"), pd.Timestamp("2020-12-31")),
    ("2021-2025", pd.Timestamp("2021-01-01"), MAIN_END),
)


@dataclass(frozen=True)
class Bars:
    """One segment of one timeframe. ``date`` is the bar's label (the engine's timestamp:
    the session for daily bars, the week's first session for weekly bars); ``close_date`` is
    the session whose close the bar ends on (the signal day)."""

    timeframe: str
    frame: pd.DataFrame  # timestamp, open, high, low, close, volume
    date: np.ndarray  # datetime64[D]
    close_date: np.ndarray  # datetime64[D]


def members(data_dir: Path) -> pd.DataFrame:
    return pullback_data.load_members(data_dir)


def daily_segments(data_dir: Path, symbol: str) -> list[pd.DataFrame]:
    """Cleaned adjusted daily bars split at corporate-action breaks (a break day starts a new
    segment, as a new listing would)."""

    cleaned = pullback_data.clean_symbol(data_dir, symbol)
    if cleaned is None:
        return []
    frame, breaks = cleaned
    frame = frame[["timestamp", "open", "high", "low", "close", "volume"]].copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"]).dt.tz_localize(None).dt.normalize()
    cuts = sorted({pd.Timestamp(b) for b in breaks} | {pd.Timestamp(b) for b in EXTRA_BREAKS.get(symbol, [])})
    segments = []
    start = 0
    stamps = frame["timestamp"]
    for cut in cuts:
        position = int(np.searchsorted(stamps.to_numpy(), np.datetime64(cut), side="left"))
        if position > start:
            segments.append(frame.iloc[start:position].reset_index(drop=True))
        start = max(start, position)
    if start < len(frame):
        segments.append(frame.iloc[start:].reset_index(drop=True))
    return [segment for segment in segments if len(segment) > 0]


def weekly(daily: pd.DataFrame) -> pd.DataFrame:
    """Weekly bars from one daily segment: first open, max high, min low, last close, summed
    volume, labelled with the week's first session; ``close_date`` is its last session."""

    week = daily["timestamp"].dt.to_period("W-SUN")
    grouped = daily.groupby(week, sort=True)
    out = pd.DataFrame({
        "timestamp": grouped["timestamp"].first().to_numpy(),
        "open": grouped["open"].first().to_numpy(),
        "high": grouped["high"].max().to_numpy(),
        "low": grouped["low"].min().to_numpy(),
        "close": grouped["close"].last().to_numpy(),
        "volume": grouped["volume"].sum().to_numpy(),
        "close_date": grouped["timestamp"].last().to_numpy(),
    })
    return out.reset_index(drop=True)


def as_bars(frame: pd.DataFrame, timeframe: str) -> Bars:
    stamps = pd.to_datetime(frame["timestamp"]).to_numpy().astype("datetime64[D]")
    if "close_date" in frame:
        close_date = pd.to_datetime(frame["close_date"]).to_numpy().astype("datetime64[D]")
    else:
        close_date = stamps
    ohlcv = frame[["timestamp", "open", "high", "low", "close", "volume"]].reset_index(drop=True)
    return Bars(timeframe=timeframe, frame=ohlcv, date=stamps, close_date=close_date)


def symbol_bars(data_dir: Path, symbol: str) -> dict[str, list[Bars]]:
    """Daily and weekly bars per segment."""

    segments = daily_segments(data_dir, symbol)
    return {
        "1d": [as_bars(segment, "1d") for segment in segments],
        "1w": [as_bars(weekly(segment), "1w") for segment in segments],
    }


def engine_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """OHLCV with UTC timestamps, as the bot and the engine see bars."""

    out = frame[["timestamp", "open", "high", "low", "close", "volume"]].copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"]).dt.tz_localize("UTC")
    return out.reset_index(drop=True)
