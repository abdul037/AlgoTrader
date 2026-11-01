"""Data for the momentum study: cached S&P 500 member bars, benchmarks and a T-bill rate.

Everything downloaded is untrusted data and lives outside the repository. Nothing here is
imported by ``app/``.

* ``--price-dir`` (read-only): the pullback study's cache. ``prices/<SYM>.parquet`` holds
  yfinance ``auto_adjust=True`` daily bars (timestamp, open, high, low, close, volume) from
  2010-01-04; ``raw/<SYM>.parquet`` holds the same bars unadjusted (used only to find
  corporate-action breaks); ``constituents/members.csv`` lists today's 503 members with GICS
  sectors. SPY and RSP come from the same cache.
* ``--extra-dir``: this study's own downloads (^IRX, the 13-week T-bill discount yield, and BIL
  as a fallback), one parquet per symbol in the same layout.

The cleaning rules are the pullback study's (``research/pullback/data.py`` on
``claude/wip-pullback-study``), copied here so this package stands alone. They were fixed there
before any result was computed and are not changed here.
"""

from __future__ import annotations

import time
from pathlib import Path

import pandas as pd

CASH_SYMBOLS = ["^IRX", "BIL"]
BENCHMARKS = ["SPY", "RSP"]
NON_MEMBER_FILES = {"SPY", "^VIX", "^VIX3M", "HYG", "IEF", "RSP", "QQQ", "IWM"}

STALE_ZERO_VOLUME_BARS = 10
"""More than this many zero-volume bars marks a stale pre-listing history (AMCR, FERG, SW, HWM,
CHTR, VRT); the symbol's history starts after its last zero-volume bar. Isolated zero-volume
bars elsewhere are dropped."""
BREAK_FACTOR_JUMP = 0.15
"""A day on which yfinance's dividend-adjustment factor (adjusted close / split-adjusted close)
moves by more than 15% is a large corporate action (spin-off, special dividend) that yfinance
adjusts unreliably (DHR 2016-07-05 shows a fake +61% day)."""
EXTRA_BREAKS = {"HWM": ["2020-04-01"]}
"""Breaks the factor test misses. Arconic Inc. became Howmet and spun off Arconic Corp. on
2020-04-01; yfinance folds that into its split column, so the factor does not move, and the
day's open (15.40, also its high, against a 12.32 prior close and a 13.20 close) is a
when-issued print. Added after the pre-registration commit from a results-blind check of opens
on month-boundary trade days, before any variant was compared with a control."""


def download_extra(extra_dir: Path, symbols: list[str] = CASH_SYMBOLS, start: str = "2009-06-01") -> dict[str, int]:
    """Download ``symbols`` (adjusted daily bars) into ``extra_dir``; skip files already there."""

    import yfinance as yf

    extra_dir.mkdir(parents=True, exist_ok=True)
    rows: dict[str, int] = {}
    for symbol in symbols:
        path = extra_dir / f"{symbol}.parquet"
        if path.exists():
            rows[symbol] = len(pd.read_parquet(path))
            continue
        frame = None
        for attempt in range(4):
            try:
                frame = yf.download(symbol, start=start, interval="1d", auto_adjust=True,
                                    actions=False, progress=False, threads=False)
                break
            except Exception as exc:  # noqa: BLE001 - network hiccups
                print(f"{symbol} attempt {attempt}: {exc}")
                time.sleep(2 ** (attempt + 1))
        if frame is None or len(frame) == 0:
            print(f"{symbol}: no data")
            continue
        if isinstance(frame.columns, pd.MultiIndex):
            frame.columns = frame.columns.get_level_values(0)
        frame = frame.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
        frame = frame.dropna(subset=["close"])
        frame.index = pd.to_datetime(frame.index).tz_localize(None).normalize()
        frame.index.name = "timestamp"
        frame.reset_index().to_parquet(path, index=False)
        rows[symbol] = len(frame)
    return rows


def _read(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        return None
    frame = pd.read_parquet(path)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"])
    return frame.sort_values("timestamp").drop_duplicates("timestamp").reset_index(drop=True)


def load_members(price_dir: Path) -> pd.DataFrame:
    return pd.read_csv(price_dir / "constituents" / "members.csv")


def clean_symbol(price_dir: Path, symbol: str) -> tuple[pd.DataFrame, list[pd.Timestamp]] | None:
    """Cleaned adjusted bars for ``symbol`` and its corporate-action break dates."""

    frame = _read(price_dir / "prices" / f"{symbol}.parquet")
    if frame is None or frame.empty:
        return None
    zero = frame["volume"] <= 0
    if int(zero.sum()) > STALE_ZERO_VOLUME_BARS:
        frame = frame.loc[frame.index[zero].max() + 1 :]
    frame = frame[frame["volume"] > 0].reset_index(drop=True)
    breaks: list[pd.Timestamp] = []
    raw = _read(price_dir / "raw" / f"{symbol}.parquet")
    if raw is not None:
        merged = frame[["timestamp", "close"]].merge(raw[["timestamp", "Close"]], on="timestamp", how="inner")
        factor = merged["close"] / merged["Close"]
        jump = (factor / factor.shift(1) - 1.0).abs()
        breaks = [pd.Timestamp(ts) for ts in merged.loc[jump > BREAK_FACTOR_JUMP, "timestamp"]]
    breaks += [pd.Timestamp(day) for day in EXTRA_BREAKS.get(symbol, [])]
    return frame, sorted(breaks)


class Panel:
    """Open and close panels on SPY's trading calendar (dates x symbols), plus breaks."""

    def __init__(self, price_dir: Path, extra_dir: Path):
        spy = _read(price_dir / "prices" / "SPY.parquet")
        assert spy is not None
        self.dates = pd.DatetimeIndex(spy["timestamp"])
        members = load_members(price_dir)
        self.members = members
        opens, closes, self.breaks, self.first_bar = {}, {}, {}, {}
        self.sector = dict(zip(members["yf_symbol"], members["sector"], strict=True))
        for symbol in members["yf_symbol"]:
            cleaned = clean_symbol(price_dir, symbol)
            if cleaned is None:
                continue
            frame, breaks = cleaned
            frame = frame.set_index("timestamp")
            opens[symbol] = frame["open"]
            closes[symbol] = frame["close"]
            self.breaks[symbol] = breaks
            self.first_bar[symbol] = frame.index.min()
        self.symbols = sorted(opens)
        self.open = pd.DataFrame(opens).reindex(self.dates)[self.symbols]
        self.close = pd.DataFrame(closes).reindex(self.dates)[self.symbols]
        bench = {}
        for symbol in BENCHMARKS:
            frame = _read(price_dir / "prices" / f"{symbol}.parquet").set_index("timestamp")
            bench[symbol] = frame
        self.bench_open = pd.DataFrame({s: f["open"] for s, f in bench.items()}).reindex(self.dates)
        self.bench_close = pd.DataFrame({s: f["close"] for s, f in bench.items()}).reindex(self.dates)
        self.cash_source = "none (0%)"
        self.irx = pd.Series(0.0, index=self.dates)
        irx = _read(extra_dir / "^IRX.parquet")
        if irx is not None and len(irx) > 1000:
            series = irx.set_index("timestamp")["close"].reindex(self.dates.union(irx["timestamp"])).ffill()
            self.irx = series.reindex(self.dates).ffill().fillna(0.0) / 100.0
            self.cash_source = "^IRX"
        bil = _read(extra_dir / "BIL.parquet")
        self.bil_close = None if bil is None else bil.set_index("timestamp")["close"].reindex(self.dates).ffill()

    def break_matrix(self) -> pd.DataFrame:
        """Boolean dates x symbols: True on a corporate-action break day."""

        out = pd.DataFrame(False, index=self.dates, columns=self.symbols)
        for symbol, days in self.breaks.items():
            for day in days:
                if day in out.index:
                    out.loc[day, symbol] = True
        return out


def month_calendar(dates: pd.DatetimeIndex) -> pd.DataFrame:
    """One row per calendar month: its first and last trading day (SPY calendar)."""

    frame = pd.DataFrame({"date": dates})
    frame["month"] = frame["date"].dt.to_period("M")
    grouped = frame.groupby("month")["date"]
    return pd.DataFrame({"first": grouped.min(), "last": grouped.max()})
