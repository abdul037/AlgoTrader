from __future__ import annotations

import time

import pandas as pd
import pytest

from app.data.yfinance_loader import load_yfinance_history


class _FakeTicker:
    def __init__(self, symbol: str):
        self.symbol = symbol

    def history(self, period: str, interval: str, auto_adjust: bool) -> pd.DataFrame:
        index = pd.to_datetime(["2026-04-07", "2026-04-08"], utc=True)
        return pd.DataFrame(
            {
                "Open": [180.0, 182.0],
                "High": [184.0, 185.0],
                "Low": [179.0, 181.0],
                "Close": [183.0, 184.5],
                "Volume": [10_000_000, 12_500_000],
            },
            index=index,
        )


class _HangingTicker:
    def __init__(self, symbol: str):
        self.symbol = symbol

    def history(self, period: str, interval: str, auto_adjust: bool) -> pd.DataFrame:
        time.sleep(5)  # simulate Yahoo stalling; abandoned once the caller times out
        return pd.DataFrame()


def test_load_yfinance_history_normalizes_columns(monkeypatch):
    monkeypatch.setattr("app.data.yfinance_loader.yf.Ticker", _FakeTicker)
    frame = load_yfinance_history("NVDA", period="1mo", interval="1d")

    assert list(frame.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert len(frame) == 2
    assert frame["timestamp"].dt.tz is not None
    assert frame.iloc[0]["close"] == 183.0


def test_load_yfinance_history_times_out_instead_of_hanging(monkeypatch):
    monkeypatch.setattr("app.data.yfinance_loader.yf.Ticker", _HangingTicker)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        load_yfinance_history("NVDA", period="1mo", interval="1d", timeout=0.5)
    # It returns promptly on timeout rather than blocking for the full stall.
    assert time.monotonic() - started < 3.0
