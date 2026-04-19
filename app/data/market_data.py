"""Market data service."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from app.data.csv_loader import load_ohlcv_csv
from app.data.yfinance_loader import load_yfinance_history


class MarketDataService:
    """Provide normalized historical market data."""

    def load_csv(self, path: str | Path) -> pd.DataFrame:
        """Load historical data from CSV."""

        return load_ohlcv_csv(path)

    def load_yfinance(
        self,
        symbol: str,
        *,
        period: str = "5y",
        interval: str = "1d",
        auto_adjust: bool = False,
    ) -> pd.DataFrame:
        """Load normalized historical data from Yahoo Finance."""

        return load_yfinance_history(
            symbol,
            period=period,
            interval=interval,
            auto_adjust=auto_adjust,
        )
