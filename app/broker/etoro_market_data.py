"""eToro market-data client for live candles and rates."""

from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

import httpx
import pandas as pd

from app.broker.etoro_rate_limit import (
    EToroRateLimitError,
    compact_http_body,
    mark_etoro_rate_limited,
    wait_for_etoro_slot,
)
from app.runtime_settings import AppSettings
from app.live_signal_schema import MarketQuote

logger = logging.getLogger(__name__)


class EtoroMarketDataClient:
    """Fetch live market data from eToro's official market-data endpoints."""

    def __init__(self, settings: AppSettings):
        self.settings = settings
        self._instrument_cache_by_symbol: dict[str, dict[str, Any]] = {}
        self._instrument_cache_by_id: dict[int, dict[str, Any]] = {}

    def resolve_instrument(self, symbol: str) -> dict[str, Any]:
        """Resolve a symbol to eToro instrument metadata."""

        normalized = symbol.upper().strip()
        cached = self._instrument_cache_by_symbol.get(normalized)
        if cached is not None:
            return cached

        payload = self._request(
            "GET",
            "/market-data/search",
            params={"internalSymbolFull": normalized},
        )
        items = payload.get("items", [])
        exact = next(
            (item for item in items if str(item.get("internalSymbolFull", "")).upper() == normalized),
            None,
        )
        if exact is None:
            raise RuntimeError(f"Instrument lookup failed for {normalized}")

        resolved = {
            "symbol": normalized,
            "instrument_id": int(exact["internalInstrumentId"]),
            "display_name": str(exact.get("title") or exact.get("internalSymbolFull") or normalized),
            "current_rate": float(exact.get("currentRate", 0.0) or 0.0),
            "is_tradable": bool(exact.get("isCurrentlyTradable", False)),
            "is_buy_enabled": bool(exact.get("isBuyEnabled", False)),
        }
        self._instrument_cache_by_symbol[normalized] = resolved
        self._instrument_cache_by_id[resolved["instrument_id"]] = resolved
        return resolved

    def get_rates(self, symbols: list[str]) -> dict[str, MarketQuote]:
        """Fetch live bid/ask quotes for up to 100 symbols."""

        if not symbols:
            return {}

        resolved_items = [self.resolve_instrument(symbol) for symbol in symbols]
        instrument_ids = [item["instrument_id"] for item in resolved_items]
        payload = self._request(
            "GET",
            "/market-data/instruments/rates",
            params={"instrumentIds": ",".join(str(item) for item in instrument_ids)},
        )
        rates_by_id = {
            int(item.get("instrumentID", 0)): item
            for item in payload.get("rates", [])
            if item.get("instrumentID") is not None
        }

        quotes: dict[str, MarketQuote] = {}
        for resolved in resolved_items:
            raw = rates_by_id.get(resolved["instrument_id"], {})
            quotes[resolved["symbol"]] = MarketQuote(
                symbol=resolved["symbol"],
                instrument_id=resolved["instrument_id"],
                bid=self._to_float(raw.get("bid")),
                ask=self._to_float(raw.get("ask")),
                last_execution=self._to_float(raw.get("lastExecution")) or resolved["current_rate"],
                timestamp=raw.get("date"),
            )
        return quotes

    def get_candles(
        self,
        symbol: str,
        *,
        candles_count: int = 250,
        direction: str = "desc",
        interval: str = "OneDay",
    ) -> pd.DataFrame:
        """Fetch normalized OHLCV candles for a symbol and eToro interval."""

        if candles_count < 5:
            raise ValueError("candles_count must be at least 5")
        if candles_count > 1000:
            raise ValueError("candles_count must not exceed 1000")

        resolved = self.resolve_instrument(symbol)
        payload = self._request(
            "GET",
            f"/market-data/instruments/{resolved['instrument_id']}/history/candles/{direction}/{interval}/{candles_count}",
        )
        candle_groups = payload.get("candles", [])
        if not candle_groups:
            raise RuntimeError(f"No candles returned for {symbol.upper()}")

        candles = candle_groups[0].get("candles", [])
        if not candles:
            raise RuntimeError(f"No candle rows returned for {symbol.upper()}")

        frame = pd.DataFrame(candles)
        frame = frame.rename(columns={"fromDate": "timestamp", "instrumentID": "instrument_id"})
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        for column in ["open", "high", "low", "close", "volume"]:
            frame[column] = pd.to_numeric(frame[column], errors="raise")
        frame = frame.sort_values("timestamp").reset_index(drop=True)
        return frame[["timestamp", "open", "high", "low", "close", "volume"]].copy()

    def get_daily_candles(
        self,
        symbol: str,
        *,
        candles_count: int = 250,
        direction: str = "desc",
        interval: str = "OneDay",
    ) -> pd.DataFrame:
        """Backward-compatible candle fetch used by the single-signal path."""

        return self.get_candles(
            symbol,
            candles_count=candles_count,
            direction=direction,
            interval=interval,
        )

    def _headers(self) -> dict[str, str]:
        if self.settings.broker_simulation_enabled:
            raise RuntimeError("eToro market data requires real API credentials in .env or .env.example")
        return {
            "x-api-key": self.settings.etoro_api_key,
            "x-user-key": self.settings.etoro_user_key,
            "x-request-id": str(uuid4()),
            "Content-Type": "application/json",
        }

    def _build_url(self, path: str) -> str:
        root = self.settings.etoro_base_url.rstrip("/")
        if path.startswith("/api/v1/"):
            return f"{root}{path}"
        return f"{root}/api/v1{path}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = self._build_url(path)
        try:
            wait_for_etoro_slot(self.settings)
            with httpx.Client(timeout=20.0) as client:
                response = client.request(
                    method=method,
                    url=url,
                    headers=self._headers(),
                    params=params,
                )
                response.raise_for_status()
                if not response.content:
                    return {}
                return response.json()
        except httpx.HTTPStatusError as exc:
            raw_body = exc.response.text
            body = compact_http_body(raw_body)
            if mark_etoro_rate_limited(
                self.settings,
                status_code=exc.response.status_code,
                body=raw_body,
            ):
                logger.warning("eToro market-data request rate-limited: %s %s", exc, body)
                raise EToroRateLimitError(f"eToro API rate-limited: {body}") from exc
            else:
                logger.error("Market data request failed: %s %s", exc, body)
            raise RuntimeError(
                f"Market data request failed with status {exc.response.status_code}: {body}"
            ) from exc
        except EToroRateLimitError:
            raise
        except httpx.HTTPError as exc:
            logger.exception("Market data request failed: %s", exc)
            raise RuntimeError(f"Market data request failed: {exc}") from exc

    @staticmethod
    def _to_float(value: Any) -> float | None:
        if value in (None, ""):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
