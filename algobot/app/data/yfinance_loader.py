"""Yahoo Finance market data loading."""

from __future__ import annotations

from typing import Any

import pandas as pd
import yfinance as yf


REQUIRED_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


def _flatten_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Flatten yfinance multi-index columns for single-symbol downloads."""

    if not isinstance(frame.columns, pd.MultiIndex):
        return frame

    flattened: list[str] = []
    for column in frame.columns:
        name_parts = [str(part) for part in column if str(part) != ""]
        flattened.append(name_parts[0])
    frame = frame.copy()
    frame.columns = flattened
    return frame


def _normalize_history(frame: pd.DataFrame) -> pd.DataFrame:
    """Normalize yfinance OHLCV history into the project's standard shape."""

    if frame.empty:
        raise ValueError("No historical data returned by yfinance.")

    frame = _flatten_columns(frame).reset_index()
    frame.columns = [str(column).strip().lower() for column in frame.columns]
    rename_map: dict[str, Any] = {
        "date": "timestamp",
        "datetime": "timestamp",
    }
    frame = frame.rename(columns=rename_map)

    missing = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"Historical data is missing required columns: {', '.join(missing)}")

    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    numeric_columns = ["open", "high", "low", "close", "volume"]
    for column in numeric_columns:
        frame[column] = pd.to_numeric(frame[column], errors="raise")

    normalized = frame.sort_values("timestamp").reset_index(drop=True)
    return normalized[REQUIRED_COLUMNS].copy()


def load_yfinance_history(
    symbol: str,
    *,
    period: str = "5y",
    interval: str = "1d",
    auto_adjust: bool = False,
) -> pd.DataFrame:
    """Load historical OHLCV data from Yahoo Finance."""

    ticker = yf.Ticker(symbol.upper())
    frame = ticker.history(period=period, interval=interval, auto_adjust=auto_adjust)
    if frame.empty:
        raise ValueError(
            f"yfinance returned no data for symbol={symbol.upper()} period={period} interval={interval}."
        )
    return _normalize_history(frame)
