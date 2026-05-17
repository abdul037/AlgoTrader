"""Alpaca data-provider cache tests."""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from app.data.providers.alpaca_data import AlpacaDataProvider
from app.live_signal_schema import MarketQuote


class FakeAlpacaClient:
    def __init__(self) -> None:
        self.bar_calls = 0
        self.quote_calls = 0

    def get_bars(self, symbol: str, *, timeframe: str, start: datetime, end: datetime) -> pd.DataFrame:
        self.bar_calls += 1
        return pd.DataFrame(
            {
                "timestamp": pd.date_range(start, periods=2, freq="1D", tz="UTC"),
                "open": [100.0, 101.0],
                "high": [102.0, 103.0],
                "low": [99.0, 100.0],
                "close": [101.0, 102.0],
                "volume": [1_000_000, 1_100_000],
                "vwap": [100.5, 101.5],
            }
        )

    def get_quote(self, symbol: str, *, force_refresh: bool = False, timeframe: str = "1d") -> MarketQuote:
        self.quote_calls += 1
        return MarketQuote(
            symbol=symbol.upper(),
            bid=100.0,
            ask=100.1,
            last_execution=100.05,
            timestamp="2026-05-17T00:00:00+00:00",
            source="alpaca",
        )


def test_get_bars_caches_to_parquet_on_first_call(tmp_path) -> None:
    client = FakeAlpacaClient()
    provider = AlpacaDataProvider(client, cache_dir=tmp_path)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 31, tzinfo=timezone.utc)

    frame = provider.get_bars("AAPL", timeframe="1d", start=start, end=end)

    assert client.bar_calls == 1
    assert len(frame) == 2
    assert list(tmp_path.glob("AAPL_1d_*.parquet"))


def test_get_bars_reads_from_cache_on_second_call_no_api_hit(tmp_path) -> None:
    client = FakeAlpacaClient()
    provider = AlpacaDataProvider(client, cache_dir=tmp_path)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 31, tzinfo=timezone.utc)

    first = provider.get_bars("AAPL", timeframe="1d", start=start, end=end)
    second = provider.get_bars("AAPL", timeframe="1d", start=start, end=end)

    assert client.bar_calls == 1
    pd.testing.assert_frame_equal(first, second)


def test_get_quote_always_calls_alpaca_no_cache(tmp_path) -> None:
    client = FakeAlpacaClient()
    provider = AlpacaDataProvider(client, cache_dir=tmp_path)

    provider.get_quote("AAPL")
    provider.get_quote("AAPL")

    assert client.quote_calls == 2


def test_cache_path_includes_symbol_timeframe_dates(tmp_path) -> None:
    provider = AlpacaDataProvider(FakeAlpacaClient(), cache_dir=tmp_path)
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 31, tzinfo=timezone.utc)

    path = provider._cache_path("BRK.B", "1h", start, end)

    assert path.name == "BRK_B_1h_20260101T000000_20260131T000000.parquet"
