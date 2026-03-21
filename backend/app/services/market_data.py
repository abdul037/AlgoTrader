from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import httpx

from app.config import get_settings
from app.schemas import PriceBar, ProviderStatus, SupportedInterval

ALPHA_VANTAGE_URL = "https://www.alphavantage.co/query"
ALPACA_STOCK_BARS_URL = "https://data.alpaca.markets/v2/stocks"


def _interval_to_minutes(interval: SupportedInterval) -> int:
    mapping = {
        "1min": 1,
        "5min": 5,
        "15min": 15,
        "30min": 30,
        "60min": 60,
        "daily": 24 * 60,
    }
    return mapping[interval]


def _symbol_seed(symbol: str) -> int:
    return sum(ord(character) for character in symbol.upper())


def _build_demo_series(symbol: str, interval: SupportedInterval, lookback: int) -> list[PriceBar]:
    seed = _symbol_seed(symbol)
    now = datetime.now(UTC)
    previous_close = 90 + seed % 120
    step = timedelta(minutes=_interval_to_minutes(interval))
    bars: list[PriceBar] = []

    for index in range(lookback - 1, -1, -1):
        timestamp = now - step * index
        wave = math.sin((index + seed) / 8) * 2.8
        drift = 0.09 if index > lookback / 2 else -0.03
        noise = ((seed * (index + 11)) % 7) / 10 - 0.3
        close = max(5, previous_close + drift + wave * 0.12 + noise)
        open_price = previous_close
        price_range = max(0.5, abs(close - open_price) + 1.15)

        bars.append(
            PriceBar(
                time=timestamp.isoformat(),
                open=round(open_price, 2),
                high=round(max(open_price, close) + price_range * 0.6, 2),
                low=round(min(open_price, close) - price_range * 0.4, 2),
                close=round(close, 2),
                volume=round(800000 + ((seed + index * 17) % 2000000)),
            )
        )
        previous_close = close

    return bars


def get_market_data_status() -> ProviderStatus:
    settings = get_settings()
    provider = settings.market_data_provider.lower()
    if provider == "alpha_vantage":
        ready = bool(settings.alpha_vantage_api_key)
        return ProviderStatus(
            provider="alpha_vantage",
            ready=ready,
            mode="delayed" if ready else "demo",
            note=(
                "Alpha Vantage is configured."
                if ready
                else "Set ALPHA_VANTAGE_API_KEY to switch from demo candles to live stock data."
            ),
        )
    if provider == "alpaca":
        feed = _normalize_alpaca_feed(settings.alpaca_data_feed)
        ready = bool(settings.alpaca_api_key and settings.alpaca_api_secret)
        feed_note = (
            "IEX is available on Alpaca's free live market data offering."
            if feed == "iex"
            else "SIP feed may require the appropriate Alpaca market data subscription."
        )
        return ProviderStatus(
            provider="alpaca",
            ready=ready,
            mode="live" if ready else "demo",
            feed=feed,
            note=(
                f"Alpaca market data is configured on the {feed.upper()} feed. {feed_note}"
                if ready
                else "Set both ALPACA_API_KEY and ALPACA_API_SECRET to switch from demo candles to Alpaca market data."
            ),
        )

    return ProviderStatus(
        provider="demo",
        ready=True,
        mode="demo",
        note="Using synthetic market data until a live provider is configured.",
    )


def _alpha_config(interval: SupportedInterval) -> tuple[str, str, dict[str, str]]:
    if interval == "daily":
        return "TIME_SERIES_DAILY", "Time Series (Daily)", {}
    return (
        "TIME_SERIES_INTRADAY",
        f"Time Series ({interval})",
        {"interval": interval, "outputsize": "compact", "adjusted": "true"},
    )


def _alpaca_timeframe(interval: SupportedInterval) -> str:
    mapping = {
        "1min": "1Min",
        "5min": "5Min",
        "15min": "15Min",
        "30min": "30Min",
        "60min": "1Hour",
        "daily": "1Day",
    }
    return mapping[interval]


def _normalize_alpaca_feed(feed: str) -> str:
    normalized = feed.strip().lower()
    return normalized if normalized in {"iex", "sip"} else "iex"


def _alpaca_start_time(interval: SupportedInterval, lookback: int) -> datetime:
    now = datetime.now(UTC)
    if interval == "daily":
        return now - timedelta(days=max(lookback * 3, 365))
    return now - timedelta(minutes=_interval_to_minutes(interval) * lookback * 8)


def _parse_alpha_payload(payload: dict, series_key: str, lookback: int) -> list[PriceBar]:
    raw_series = payload.get(series_key)
    if not isinstance(raw_series, dict):
        note = payload.get("Note")
        error_message = payload.get("Error Message")
        raise RuntimeError(note or error_message or "Could not parse Alpha Vantage response.")

    bars: list[PriceBar] = []
    for time, candle in raw_series.items():
        if not isinstance(candle, dict):
            continue
        try:
            bars.append(
                PriceBar(
                    time=datetime.fromisoformat(time).replace(tzinfo=UTC).isoformat(),
                    open=float(candle["1. open"]),
                    high=float(candle["2. high"]),
                    low=float(candle["3. low"]),
                    close=float(candle["4. close"]),
                    volume=int(float(candle.get("5. volume", 0))),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue

    bars.sort(key=lambda bar: bar.time)
    return bars[-lookback:]


def _parse_alpaca_payload(payload: dict, lookback: int) -> list[PriceBar]:
    raw_bars = payload.get("bars")
    if not isinstance(raw_bars, list):
        message = payload.get("message")
        code = payload.get("code")
        raise RuntimeError(message or code or "Could not parse Alpaca market data response.")

    bars: list[PriceBar] = []
    for candle in raw_bars:
        if not isinstance(candle, dict):
            continue
        try:
            timestamp = str(candle["t"]).replace("Z", "+00:00")
            bars.append(
                PriceBar(
                    time=datetime.fromisoformat(timestamp).astimezone(UTC).isoformat(),
                    open=float(candle["o"]),
                    high=float(candle["h"]),
                    low=float(candle["l"]),
                    close=float(candle["c"]),
                    volume=int(float(candle.get("v", 0))),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue

    bars.sort(key=lambda bar: bar.time)
    return bars[-lookback:]


async def fetch_market_data(
    symbol: str, interval: SupportedInterval = "15min", lookback: int = 220
) -> tuple[str, str, list[PriceBar]]:
    settings = get_settings()
    provider = settings.market_data_provider.lower()

    if provider == "alpaca" and settings.alpaca_api_key and settings.alpaca_api_secret:
        feed = _normalize_alpaca_feed(settings.alpaca_data_feed)
        params = {
            "timeframe": _alpaca_timeframe(interval),
            "start": _alpaca_start_time(interval, lookback).isoformat().replace("+00:00", "Z"),
            "end": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "limit": str(lookback),
            "adjustment": "raw",
            "feed": feed,
            "sort": "asc",
        }

        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                f"{ALPACA_STOCK_BARS_URL}/{symbol.upper()}/bars",
                params=params,
                headers={
                    "APCA-API-KEY-ID": settings.alpaca_api_key,
                    "APCA-API-SECRET-KEY": settings.alpaca_api_secret,
                },
            )
            payload = response.json()

        if response.status_code >= 400:
            message = payload.get("message") if isinstance(payload, dict) else None
            raise RuntimeError(message or f"Alpaca market data request failed with {response.status_code}.")

        bars = _parse_alpaca_payload(payload, lookback)
        if not bars:
            raise RuntimeError(f"No Alpaca market data returned for {symbol.upper()}.")
        return "alpaca", "live", bars

    if provider == "alpha_vantage" and settings.alpha_vantage_api_key:
        function_name, series_key, extra_params = _alpha_config(interval)
        params = {
            "function": function_name,
            "symbol": symbol.upper(),
            "apikey": settings.alpha_vantage_api_key,
            **extra_params,
        }

        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(ALPHA_VANTAGE_URL, params=params)
            response.raise_for_status()
            payload = response.json()

        bars = _parse_alpha_payload(payload, series_key, lookback)
        if not bars:
            raise RuntimeError(f"No market data returned for {symbol.upper()}.")
        return "alpha_vantage", "delayed", bars

    if provider not in {"demo", "alpha_vantage", "alpaca"}:
        raise RuntimeError(f"Unsupported market data provider: {provider}")

    return "demo", "demo", _build_demo_series(symbol, interval, lookback)
