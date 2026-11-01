"""Download and load the study's daily data (yfinance). Nothing here is imported by ``app/``.

Adjusted bars come from ``auto_adjust=True`` (split- and dividend-adjusted OHLC). Unadjusted closes
(``auto_adjust=False``) are stored separately and used only for the price-chart signal diagnostic.
The cash proxy is BIL from its first full day, and the 13-week T-bill yield (^IRX) before that.
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import pandas as pd

FUNDS = ["SPY", "QQQ", "IWM", "EFA", "IEF", "TLT", "GLD", "DBC", "VNQ", "BIL"]
YIELD_INDEX = "^IRX"
START = "2003-01-01"
END = "2026-11-01"  # exclusive in yfinance: the last bar is 2026-10-30 (a Friday)


def _fetch(symbol: str, auto_adjust: bool) -> pd.DataFrame:
    import yfinance as yf

    last_error: Exception | None = None
    for wait in (0, 2, 4, 8, 16):
        if wait:
            time.sleep(wait)
        try:
            frame = yf.Ticker(symbol).history(
                start=START, end=END, auto_adjust=auto_adjust, actions=False, raise_errors=True
            )
            if len(frame):
                break
        except Exception as exc:  # network errors: retry with backoff
            last_error = exc
    else:
        raise RuntimeError(f"download failed for {symbol}: {last_error}")
    frame.index = pd.DatetimeIndex(frame.index.tz_localize(None).normalize(), name="date")
    return frame


def download(data_dir: Path) -> None:
    (data_dir / "adjusted").mkdir(parents=True, exist_ok=True)
    (data_dir / "raw").mkdir(parents=True, exist_ok=True)
    for symbol in FUNDS + [YIELD_INDEX]:
        adjusted = _fetch(symbol, auto_adjust=True)
        cols = ["Open", "High", "Low", "Close", "Volume"]
        adjusted[cols].to_csv(data_dir / "adjusted" / f"{_name(symbol)}.csv")
        if symbol != YIELD_INDEX:
            raw = _fetch(symbol, auto_adjust=False)
            raw[["Open", "Close", "Adj Close"]].to_csv(data_dir / "raw" / f"{_name(symbol)}.csv")
        first, last = adjusted.index[0].date(), adjusted.index[-1].date()
        print(f"{symbol}: {len(adjusted)} bars {first} .. {last}")


def _name(symbol: str) -> str:
    return symbol.replace("^", "")


def load_adjusted(data_dir: Path, symbol: str) -> pd.DataFrame:
    frame = pd.read_csv(
        data_dir / "adjusted" / f"{_name(symbol)}.csv", index_col="date", parse_dates=["date"]
    )
    return frame.astype(float)


def load_raw(data_dir: Path, symbol: str) -> pd.DataFrame:
    frame = pd.read_csv(
        data_dir / "raw" / f"{_name(symbol)}.csv", index_col="date", parse_dates=["date"]
    )
    return frame.astype(float)


def cash_daily_returns(data_dir: Path, calendar: pd.DatetimeIndex) -> tuple[pd.Series, str]:
    """Daily cash return on ``calendar``: ^IRX up to BIL's first day, BIL's adjusted close after.

    ^IRX is the 13-week bill discount yield in percent. Each day earns the previous close's yield
    times the calendar days elapsed / 360 (the bill market's actual/360 convention). A missing
    ^IRX value is filled forward from the last one. BIL's first bar has no prior close, so BIL's
    returns start the day after it.
    """
    irx = load_adjusted(data_dir, YIELD_INDEX)["Close"].reindex(calendar).ffill()
    days = pd.Series(calendar, index=calendar).diff().dt.days
    irx_ret = (irx.shift(1) / 100.0) * days / 360.0
    bil = load_adjusted(data_dir, "BIL")["Close"].reindex(calendar)
    bil_ret = bil / bil.shift(1) - 1.0
    first_bil = bil.first_valid_index()
    use_bil = (calendar > first_bil) & bil_ret.notna().to_numpy()
    out = pd.Series(np.where(use_bil, bil_ret, irx_ret), index=calendar).fillna(0.0)
    note = (
        f"^IRX (actual/360) through {first_bil.date()}, BIL adjusted close from the next "
        f"trading day"
    )
    return out, note
