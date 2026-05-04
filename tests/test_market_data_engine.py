from __future__ import annotations

import pandas as pd
import pytest

from app.data.engine import MarketDataEngine
from app.live_signal_schema import MarketQuote
from tests.conftest import make_settings


class FlakyHistoryService:
    def __init__(self) -> None:
        self.calls = 0

    def load_yfinance(self, symbol: str, *, period: str, interval: str, auto_adjust: bool = False):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("temporary provider outage")
        timestamps = pd.date_range("2026-01-01", periods=30, freq="1D", tz="UTC")
        return pd.DataFrame(
            {
                "timestamp": timestamps,
                "open": [100.0] * 30,
                "high": [101.0] * 30,
                "low": [99.0] * 30,
                "close": [100.5] * 30,
                "volume": [1_000_000] * 30,
            }
        )


class FiveMinuteHistoryService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def load_yfinance(self, symbol: str, *, period: str, interval: str, auto_adjust: bool = False):
        self.calls.append((period, interval))
        timestamps = pd.date_range("2026-01-01 14:30", periods=12, freq="5min", tz="UTC")
        return pd.DataFrame(
            {
                "timestamp": timestamps,
                "open": [100.0 + index for index in range(12)],
                "high": [101.0 + index for index in range(12)],
                "low": [99.0 + index for index in range(12)],
                "close": [100.5 + index for index in range(12)],
                "volume": [1_000 + index for index in range(12)],
            }
        )


class FakeEtoroClient:
    def __init__(self, *, fail_history: bool = False) -> None:
        self.fail_history = fail_history
        self.candle_calls: list[tuple[str, str, int]] = []
        self.rate_calls: list[list[str]] = []

    def get_candles(self, symbol: str, *, candles_count: int = 250, direction: str = "desc", interval: str = "OneDay"):
        self.candle_calls.append((symbol, interval, candles_count))
        if self.fail_history:
            raise RuntimeError("eToro candles unavailable")
        timestamps = pd.date_range("2026-01-01 14:30", periods=60, freq="5min", tz="UTC")
        return pd.DataFrame(
            {
                "timestamp": timestamps,
                "open": [100.0] * 60,
                "high": [101.0] * 60,
                "low": [99.0] * 60,
                "close": [100.5] * 60,
                "volume": [1_000_000] * 60,
            }
        )

    def get_rates(self, symbols: list[str]):
        self.rate_calls.append(list(symbols))
        return {
            symbol.upper(): MarketQuote(
                symbol=symbol.upper(),
                bid=100.4,
                ask=100.6,
                last_execution=100.5,
                source="etoro",
            )
            for symbol in symbols
        }


def test_market_data_engine_retries_transient_provider_failure(tmp_path) -> None:
    history = FlakyHistoryService()
    engine = MarketDataEngine(
        make_settings(
            tmp_path,
            primary_market_data_provider="yfinance",
            fallback_market_data_provider="none",
            market_data_retry_attempts=2,
            market_data_retry_backoff_seconds=0,
            market_data_cache_dir=str(tmp_path / "cache"),
        ),
        history_service=history,
    )

    frame = engine.get_history("NVDA", timeframe="1d", bars=20, force_refresh=True)

    assert history.calls == 2
    assert len(frame) == 20
    assert frame.attrs["provider"] == "yfinance"


def test_market_data_engine_resamples_10m_from_5m_yfinance(tmp_path) -> None:
    history = FiveMinuteHistoryService()
    engine = MarketDataEngine(
        make_settings(
            tmp_path,
            primary_market_data_provider="yfinance",
            fallback_market_data_provider="none",
            market_data_cache_dir=str(tmp_path / "cache"),
        ),
        history_service=history,
    )

    frame = engine.get_history("NVDA", timeframe="10m", bars=20, force_refresh=True)

    assert history.calls == [("30d", "5m")]
    assert len(frame) == 7
    assert frame.iloc[0]["open"] == 100.0
    assert frame.iloc[0]["close"] == 100.5
    assert frame.iloc[1]["open"] == 101.0
    assert frame.iloc[1]["close"] == 102.5
    assert frame.iloc[1]["volume"] == 2_003


def test_market_data_engine_auto_prefers_etoro_intraday_history(tmp_path) -> None:
    etoro = FakeEtoroClient()
    history = FiveMinuteHistoryService()
    engine = MarketDataEngine(
        make_settings(
            tmp_path,
            primary_market_data_provider="auto",
            fallback_market_data_provider="yfinance",
            market_data_retry_attempts=1,
            market_data_cache_dir=str(tmp_path / "cache"),
        ),
        etoro_client=etoro,
        history_service=history,
    )

    frame = engine.get_history("NVDA", timeframe="5m", bars=20, force_refresh=True)

    assert etoro.candle_calls == [("NVDA", "FiveMinutes", 50)]
    assert history.calls == []
    assert frame.attrs["provider"] == "etoro"
    assert frame.attrs["used_fallback"] is False


def test_market_data_engine_supports_weekly_etoro_history(tmp_path) -> None:
    etoro = FakeEtoroClient()
    engine = MarketDataEngine(
        make_settings(
            tmp_path,
            primary_market_data_provider="auto",
            fallback_market_data_provider="yfinance",
            market_data_retry_attempts=1,
            market_data_cache_dir=str(tmp_path / "cache"),
        ),
        etoro_client=etoro,
        history_service=FiveMinuteHistoryService(),
    )

    frame = engine.get_history("NVDA", timeframe="weekly", bars=20, force_refresh=True)

    assert etoro.candle_calls == [("NVDA", "OneWeek", 50)]
    assert frame.attrs["provider"] == "etoro"


def test_market_data_engine_weekly_yfinance_fallback(tmp_path) -> None:
    etoro = FakeEtoroClient(fail_history=True)
    history = FiveMinuteHistoryService()
    engine = MarketDataEngine(
        make_settings(
            tmp_path,
            primary_market_data_provider="auto",
            fallback_market_data_provider="yfinance",
            market_data_retry_attempts=1,
            market_data_cache_dir=str(tmp_path / "cache"),
        ),
        etoro_client=etoro,
        history_service=history,
    )

    frame = engine.get_history("NVDA", timeframe="1w", bars=20, force_refresh=True)

    assert etoro.candle_calls == [("NVDA", "OneWeek", 50)]
    assert history.calls == [("10y", "1wk")]
    assert frame.attrs["provider"] == "yfinance"
    assert frame.attrs["used_fallback"] is True


def test_market_data_engine_falls_back_to_yfinance_when_etoro_history_fails(tmp_path) -> None:
    etoro = FakeEtoroClient(fail_history=True)
    history = FiveMinuteHistoryService()
    engine = MarketDataEngine(
        make_settings(
            tmp_path,
            primary_market_data_provider="auto",
            fallback_market_data_provider="yfinance",
            market_data_retry_attempts=1,
            market_data_cache_dir=str(tmp_path / "cache"),
        ),
        etoro_client=etoro,
        history_service=history,
    )

    frame = engine.get_history("NVDA", timeframe="5m", bars=20, force_refresh=True)

    assert etoro.candle_calls == [("NVDA", "FiveMinutes", 50)]
    assert history.calls == [("30d", "5m")]
    assert frame.attrs["provider"] == "yfinance"
    assert frame.attrs["used_fallback"] is True


def test_market_data_engine_auto_uses_etoro_live_quote_for_intraday(tmp_path) -> None:
    etoro = FakeEtoroClient()
    engine = MarketDataEngine(
        make_settings(
            tmp_path,
            primary_market_data_provider="auto",
            fallback_market_data_provider="yfinance",
            market_data_cache_dir=str(tmp_path / "cache"),
        ),
        etoro_client=etoro,
        history_service=FiveMinuteHistoryService(),
    )

    quote = engine.get_quote("NVDA", timeframe="1m", force_refresh=True)

    assert etoro.rate_calls == [["NVDA"]]
    assert quote.source == "etoro"
    assert quote.quote_derived_from_history is False


def test_market_data_engine_rejects_unsupported_timeframe(tmp_path) -> None:
    engine = MarketDataEngine(
        make_settings(tmp_path, market_data_cache_dir=str(tmp_path / "cache")),
        etoro_client=FakeEtoroClient(),
        history_service=FiveMinuteHistoryService(),
    )

    with pytest.raises(ValueError, match="Unsupported timeframe"):
        engine.get_history("NVDA", timeframe="2m", bars=20, force_refresh=True)
