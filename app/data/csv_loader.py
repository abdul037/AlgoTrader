"""CSV market data loading."""

from __future__ import annotations

from pathlib import Path

import pandas as pd


REQUIRED_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]


def load_ohlcv_csv(path: str | Path) -> pd.DataFrame:
    """Load and normalize OHLCV data from CSV."""

    frame = pd.read_csv(path)
    frame.columns = [column.strip().lower() for column in frame.columns]

    missing = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"CSV is missing required columns: {', '.join(missing)}")

    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    numeric_columns = ["open", "high", "low", "close", "volume"]
    for column in numeric_columns:
        frame[column] = pd.to_numeric(frame[column], errors="raise")

    frame = frame.sort_values("timestamp").reset_index(drop=True)
    return frame[REQUIRED_COLUMNS].copy()
