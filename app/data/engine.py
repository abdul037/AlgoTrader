"""Reusable market data engine with provider fallback and lightweight caching."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from time import sleep, time
from typing import Any

import pandas as pd

from app.broker.etoro_rate_limit import EToroRateLimitError
from app.live_signal_schema import MarketQuote
from app.runtime_settings import AppSettings
from app.data.market_data import MarketDataService

logger = logging.getLogger(__name__)


TIMEFRAME_CONFIG: dict[str, dict[str, Any]] = {
    "1m": {"provider_interval": "1m", "etoro_interval": "OneMinute", "period": "7d", "cache_ttl": 60},
    "5m": {"provider_interval": "5m", "etoro_interval": "FiveMinutes", "period": "30d", "cache_ttl": 120},
    "10m": {"provider_interval": "5m", "etoro_interval": "TenMinutes", "period": "30d", "cache_ttl": 180, "resample_rule": "10min"},
    "15m": {"provider_interval": "15m", "etoro_interval": "FifteenMinutes", "period": "60d", "cache_ttl": 300},
    "1h": {"provider_interval": "60m", "etoro_interval": "OneHour", "period": "730d", "cache_ttl": 900},
    "1d": {"provider_interval": "1d", "etoro_interval": "OneDay", "period": "5y", "cache_ttl": 3600},
    "1w": {"provider_interval": "1wk", "etoro_interval": "OneWeek", "period": "10y", "cache_ttl": 21600},
}


class MarketDataEngine:
    """Fetch normalized OHLCV data across providers with file-based caching."""

    def __init__(self, settings: AppSettings, *, etoro_client: Any | None = None, history_service: MarketDataService | None = None):
        self.settings = settings
        self.etoro_client = etoro_client
        self.history_service = history_service or MarketDataService()
        self.cache_dir = Path(settings.market_data_cache_dir).expanduser().resolve()

    def get_history(
        self,
        symbol: str,
        *,
        timeframe: str = "1d",
        bars: int = 250,
        provider: str | None = None,
        force_refresh: bool = False,
    ) -> pd.DataFrame:
        """Return normalized OHLCV history for a symbol and timeframe."""

        normalized_symbol = symbol.upper().strip()
        normalized_timeframe = self._normalize_timeframe(timeframe)
        provider_name = self._resolve_provider(normalized_timeframe, provider)
        cache_path = self._cache_path(normalized_symbol, normalized_timeframe, provider_name)

        if not force_refresh:
            cached = self._load_cached_frame(
                cache_path,
                normalized_timeframe,
                bars,
                provider=provider_name,
                requested_provider=provider_name,
            )
            if cached is not None:
                return cached.tail(bars).reset_index(drop=True)

        fetch_errors: list[str] = []
        for candidate_provider in self._provider_order(normalized_timeframe, provider_name):
            frame = None
            provider_errors: list[str] = []
            for attempt in range(self._retry_attempts()):
                try:
                    frame = self._fetch_history_from_provider(
                        normalized_symbol,
                        timeframe=normalized_timeframe,
                        bars=bars,
                        provider=candidate_provider,
                    )
                    break
                except EToroRateLimitError:
                    raise
                except Exception as exc:
                    provider_errors.append(str(exc))
                    if attempt < self._retry_attempts() - 1:
                        sleep(self._retry_backoff_seconds(attempt))
            if frame is None:
                fetch_errors.append(f"{candidate_provider}: {' | '.join(provider_errors)}")
                continue

            frame = self._annotate_frame(
                frame,
                provider=candidate_provider,
                requested_provider=provider_name,
                used_fallback=candidate_provider != provider_name,
                from_cache=False,
                data_age_seconds=0.0,
            )
            self._write_cached_frame(
                self._cache_path(normalized_symbol, normalized_timeframe, candidate_provider),
                frame,
            )
            return frame.tail(bars).reset_index(drop=True)

        raise RuntimeError(
            f"Failed to load history for {normalized_symbol} timeframe={normalized_timeframe}. "
            f"Errors: {' | '.join(fetch_errors)}"
        )

    def get_quote(
        self,
        symbol: str,
        *,
        timeframe: str = "1d",
        provider: str | None = None,
        force_refresh: bool = False,
    ) -> MarketQuote:
        """Return the best available quote for a symbol."""

        normalized_symbol = symbol.upper().strip()
        normalized_timeframe = self._normalize_timeframe(timeframe)
        provider_name = self._resolve_provider(normalized_timeframe, provider)
        quote_provider = self._resolve_quote_provider(provider)

        if quote_provider == "etoro" and self.etoro_client is not None:
            for attempt in range(self._retry_attempts()):
                try:
                    quote = self.etoro_client.get_rates([normalized_symbol]).get(normalized_symbol)
                    if quote is not None:
                        return quote.model_copy(
                            update={
                                "source": "etoro",
                                "is_primary": True,
                                "used_fallback": False,
                                "from_cache": False,
                                "quote_derived_from_history": False,
                                "data_age_seconds": 0.0,
                            }
                        )
                except EToroRateLimitError:
                    raise
                except Exception as exc:
                    logger.warning("eToro quote fallback for %s failed: %s", normalized_symbol, exc)
                    if attempt < self._retry_attempts() - 1:
                        sleep(self._retry_backoff_seconds(attempt))

        frame = self.get_history(
            normalized_symbol,
            timeframe=normalized_timeframe,
            bars=2,
            provider="yfinance" if provider_name != "etoro" else None,
            force_refresh=force_refresh,
        )
        last = frame.iloc[-1]
        attrs = dict(frame.attrs)
        provider_used = str(attrs.get("provider") or provider_name)
        return MarketQuote(
            symbol=normalized_symbol,
            bid=float(last["close"]),
            ask=float(last["close"]),
            last_execution=float(last["close"]),
            timestamp=last["timestamp"].isoformat() if hasattr(last["timestamp"], "isoformat") else str(last["timestamp"]),
            source=provider_used,
            is_primary=not bool(attrs.get("used_fallback", False)),
            used_fallback=bool(attrs.get("used_fallback", False)),
            from_cache=bool(attrs.get("from_cache", False)),
            quote_derived_from_history=True,
            data_age_seconds=float(attrs.get("data_age_seconds", 0.0) or 0.0),
        )

    def _fetch_history_from_provider(
        self,
        symbol: str,
        *,
        timeframe: str,
        bars: int,
        provider: str,
    ) -> pd.DataFrame:
        if provider == "etoro":
            if self.etoro_client is None:
                raise RuntimeError("eToro client is not configured")
            config = TIMEFRAME_CONFIG[timeframe]
            interval = str(config["etoro_interval"])
            candle_count = min(max(bars, 50), 1000)
            if hasattr(self.etoro_client, "get_candles"):
                return self.etoro_client.get_candles(symbol, candles_count=candle_count, interval=interval)
            return self.etoro_client.get_daily_candles(symbol, candles_count=candle_count, interval=interval)

        if provider == "yfinance":
            config = TIMEFRAME_CONFIG[timeframe]
            yf_symbol = symbol.replace(".", "-")
            frame = self.history_service.load_yfinance(
                yf_symbol,
                period=config["period"],
                interval=config["provider_interval"],
                auto_adjust=False,
            )
            if config.get("resample_rule"):
                return self._resample_ohlcv(frame, str(config["resample_rule"]))
            return frame

        raise ValueError(f"Unsupported provider: {provider}")

    def _load_cached_frame(
        self,
        path: Path,
        timeframe: str,
        bars: int,
        *,
        provider: str,
        requested_provider: str,
    ) -> pd.DataFrame | None:
        if not path.exists():
            return None
        ttl_candidates = [
            float(self.settings.market_data_cache_ttl_seconds),
            float(TIMEFRAME_CONFIG[timeframe]["cache_ttl"]),
        ]
        if self.settings.require_verified_market_data_for_alerts:
            ttl_candidates.append(float(self.settings.max_market_data_age_seconds))
        ttl = min(ttl_candidates)
        age_seconds = max(time() - path.stat().st_mtime, 0)
        if age_seconds > ttl:
            return None
        try:
            frame = pd.read_csv(path)
            frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
            if len(frame) < min(bars, 20):
                return None
            return self._annotate_frame(
                frame[["timestamp", "open", "high", "low", "close", "volume"]].copy(),
                provider=provider,
                requested_provider=requested_provider,
                used_fallback=provider != requested_provider,
                from_cache=True,
                data_age_seconds=age_seconds,
            )
        except Exception as exc:
            logger.warning("Failed to read market data cache %s: %s", path, exc)
            return None

    def _write_cached_frame(self, path: Path, frame: pd.DataFrame) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            cached = frame.copy()
            cached["timestamp"] = pd.to_datetime(cached["timestamp"], utc=True)
            cached.to_csv(path, index=False)
            meta_path = path.with_suffix(".meta.json")
            meta_path.write_text(json.dumps({"rows": len(cached), "updated_at": int(time())}), encoding="utf-8")
        except Exception as exc:
            logger.warning("Failed to write market data cache %s: %s", path, exc)

    def _cache_path(self, symbol: str, timeframe: str, provider: str) -> Path:
        safe_symbol = symbol.replace("/", "_").replace(":", "_")
        return self.cache_dir / provider / timeframe / f"{safe_symbol}.csv"

    @staticmethod
    def _annotate_frame(
        frame: pd.DataFrame,
        *,
        provider: str,
        requested_provider: str,
        used_fallback: bool,
        from_cache: bool,
        data_age_seconds: float,
    ) -> pd.DataFrame:
        annotated = frame.copy()
        annotated.attrs.update(
            {
                "provider": provider,
                "requested_provider": requested_provider,
                "used_fallback": used_fallback,
                "from_cache": from_cache,
                "data_age_seconds": round(float(data_age_seconds or 0.0), 3),
            }
        )
        return annotated

    @staticmethod
    def _resample_ohlcv(frame: pd.DataFrame, rule: str) -> pd.DataFrame:
        if frame.empty:
            return frame
        working = frame.copy()
        working["timestamp"] = pd.to_datetime(working["timestamp"], utc=True)
        resampled = (
            working.set_index("timestamp")
            .resample(rule, label="right", closed="right")
            .agg(
                open=("open", "first"),
                high=("high", "max"),
                low=("low", "min"),
                close=("close", "last"),
                volume=("volume", "sum"),
            )
            .dropna(subset=["open", "high", "low", "close"])
            .reset_index()
        )
        return resampled[["timestamp", "open", "high", "low", "close", "volume"]]

    def _resolve_provider(self, timeframe: str, provider: str | None) -> str:
        requested = (provider or self.settings.primary_market_data_provider or "auto").strip().lower()
        if requested == "auto":
            if self.etoro_client is not None:
                return "etoro"
            fallback = (self.settings.fallback_market_data_provider or "yfinance").strip().lower()
            return "yfinance" if fallback in {"", "none"} else fallback
        return requested

    def _resolve_quote_provider(self, provider: str | None) -> str:
        requested = (provider or self.settings.primary_market_data_provider or "auto").strip().lower()
        if requested in {"auto", "etoro"} and self.etoro_client is not None:
            return "etoro"
        fallback = (self.settings.fallback_market_data_provider or "yfinance").strip().lower()
        return fallback if fallback not in {"", "none"} else "yfinance"

    def _provider_order(self, timeframe: str, primary_provider: str) -> list[str]:
        providers = [primary_provider]
        fallback = (self.settings.fallback_market_data_provider or "").strip().lower()
        if fallback and fallback != "none" and fallback not in providers:
            providers.append(fallback)
        return providers

    def _retry_attempts(self) -> int:
        return max(1, int(getattr(self.settings, "market_data_retry_attempts", 1) or 1))

    def _retry_backoff_seconds(self, attempt: int) -> float:
        base = max(0.0, float(getattr(self.settings, "market_data_retry_backoff_seconds", 0.0) or 0.0))
        return base * float(attempt + 1)

    @staticmethod
    def _normalize_timeframe(timeframe: str) -> str:
        normalized = timeframe.strip().lower()
        mapping = {
            "oneday": "1d",
            "1day": "1d",
            "day": "1d",
            "week": "1w",
            "weekly": "1w",
            "1week": "1w",
            "1wk": "1w",
            "60m": "1h",
            "60min": "1h",
            "hour": "1h",
            "5min": "5m",
            "10min": "10m",
            "10minute": "10m",
            "10minutes": "10m",
            "15min": "15m",
            "1min": "1m",
        }
        normalized = mapping.get(normalized, normalized)
        if normalized not in TIMEFRAME_CONFIG:
            raise ValueError(f"Unsupported timeframe: {timeframe}")
        return normalized
