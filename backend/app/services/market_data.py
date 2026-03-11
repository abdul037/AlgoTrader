from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import httpx

from app.config import get_settings
from app.schemas import PriceBar, ProviderStatus, SupportedInterval

ALPHA_VANTAGE_URL = "https://www.alphavantage.co/query"


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


async def fetch_market_data(
    symbol: str, interval: SupportedInterval = "15min", lookback: int = 220
) -> tuple[str, str, list[PriceBar]]:
    settings = get_settings()
    provider = settings.market_data_provider.lower()

    if provider != "alpha_vantage" or not settings.alpha_vantage_api_key:
        return "demo", "demo", _build_demo_series(symbol, interval, lookback)

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
