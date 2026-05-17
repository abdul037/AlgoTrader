"""Alpaca historical/quote data provider with local parquet caching."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd

from app.live_signal_schema import MarketQuote

if TYPE_CHECKING:
    from app.broker.alpaca_client import AlpacaClient


class AlpacaDataProvider:
    """Small research-data wrapper around the Phase A Alpaca client."""

    def __init__(self, alpaca_client: "AlpacaClient", cache_dir: Path = Path(".cache/alpaca")):
        self._client = alpaca_client
        self._cache_dir = cache_dir
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    def get_bars(self, symbol: str, *, timeframe: str, start: datetime, end: datetime) -> pd.DataFrame:
        """Return UTC-indexed OHLCV bars. Cache to parquet by symbol/timeframe/date range."""

        cache_path = self._cache_path(symbol, timeframe, start, end)
        if cache_path.exists():
            return _normalize_frame(pd.read_parquet(cache_path))
        bars = self._client.get_bars(symbol, timeframe=timeframe, start=start, end=end)
        bars = _normalize_frame(bars)
        if not bars.empty:
            bars.to_parquet(cache_path, index=False)
        return bars

    def get_quote(self, symbol: str) -> MarketQuote:
        """Live quote, no caching because quotes should always be fresh."""

        return self._client.get_quote(symbol, force_refresh=True, timeframe="1d")

    def _cache_path(self, symbol: str, timeframe: str, start, end) -> Path:
        safe_symbol = "".join(char if char.isalnum() else "_" for char in symbol.upper().strip())
        safe_timeframe = "".join(char if char.isalnum() else "_" for char in timeframe.lower().strip())
        start_token = _utc_token(start)
        end_token = _utc_token(end)
        return self._cache_dir / f"{safe_symbol}_{safe_timeframe}_{start_token}_{end_token}.parquet"


def _normalize_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame.copy()
    normalized = frame.copy()
    normalized["timestamp"] = pd.to_datetime(normalized["timestamp"], utc=True)
    keep = ["timestamp", "open", "high", "low", "close", "volume"]
    if "vwap" in normalized.columns:
        keep.append("vwap")
    return normalized[keep].sort_values("timestamp").reset_index(drop=True)


def _utc_token(value) -> str:
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.strftime("%Y%m%dT%H%M%S")
