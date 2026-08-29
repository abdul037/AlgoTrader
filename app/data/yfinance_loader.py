"""Yahoo Finance market data loading."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any, TypeVar

import pandas as pd
import yfinance as yf

logger = logging.getLogger(__name__)

REQUIRED_COLUMNS = ["timestamp", "open", "high", "low", "close", "volume"]

# Yahoo aggressively rate-limits / stalls requests from cloud-provider IPs, and
# yfinance can then block indefinitely (no reliable internal timeout in 1.6.0).
# A hard wall-clock cap guarantees a stalled fetch fails fast so the scheduler
# tick can fall back to another provider instead of hanging the whole worker.
DEFAULT_YFINANCE_TIMEOUT_SECONDS = 15.0

_T = TypeVar("_T")


def _call_with_timeout(fn: Callable[[], _T], timeout: float, *, what: str) -> _T:
    """Run ``fn`` on a daemon thread, raising ``TimeoutError`` if it overruns.

    The overrunning thread is abandoned (it is a daemon, so it cannot keep the
    process alive); this trades a rare leaked thread for a guarantee that the
    caller — and the scheduler tick above it — never blocks past ``timeout``.
    """

    box: dict[str, Any] = {}

    def _target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller thread
            box["error"] = exc

    worker = threading.Thread(target=_target, name="yfinance-fetch", daemon=True)
    worker.start()
    worker.join(max(float(timeout), 1.0))
    if worker.is_alive():
        logger.warning("yfinance fetch timed out after %.0fs: %s", timeout, what)
        raise TimeoutError(f"yfinance fetch timed out after {timeout:.0f}s: {what}")
    if "error" in box:
        raise box["error"]
    return box["value"]  # type: ignore[return-value]


def _flatten_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Flatten yfinance multi-index columns for single-symbol downloads."""

    if not isinstance(frame.columns, pd.MultiIndex):
        return frame

    flattened: list[str] = []
    for column in frame.columns:
        name_parts = [str(part) for part in column if str(part) != ""]
        flattened.append(name_parts[0])
    frame = frame.copy()
    frame.columns = flattened
    return frame


def _normalize_history(frame: pd.DataFrame) -> pd.DataFrame:
    """Normalize yfinance OHLCV history into the project's standard shape."""

    if frame.empty:
        raise ValueError("No historical data returned by yfinance.")

    frame = _flatten_columns(frame).reset_index()
    frame.columns = [str(column).strip().lower() for column in frame.columns]
    rename_map: dict[str, Any] = {
        "date": "timestamp",
        "datetime": "timestamp",
        "index": "timestamp",
    }
    frame = frame.rename(columns=rename_map)

    missing = [column for column in REQUIRED_COLUMNS if column not in frame.columns]
    if missing:
        raise ValueError(f"Historical data is missing required columns: {', '.join(missing)}")

    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    numeric_columns = ["open", "high", "low", "close", "volume"]
    for column in numeric_columns:
        frame[column] = pd.to_numeric(frame[column], errors="raise")

    normalized = frame.sort_values("timestamp").reset_index(drop=True)
    return normalized[REQUIRED_COLUMNS].copy()


def load_yfinance_history(
    symbol: str,
    *,
    period: str = "5y",
    interval: str = "1d",
    auto_adjust: bool = False,
    timeout: float = DEFAULT_YFINANCE_TIMEOUT_SECONDS,
) -> pd.DataFrame:
    """Load historical OHLCV data from Yahoo Finance (with a hard timeout)."""

    def _fetch() -> pd.DataFrame:
        ticker = yf.Ticker(symbol.upper())
        return ticker.history(period=period, interval=interval, auto_adjust=auto_adjust)

    frame = _call_with_timeout(
        _fetch,
        timeout,
        what=f"symbol={symbol.upper()} period={period} interval={interval}",
    )
    if frame.empty:
        raise ValueError(
            f"yfinance returned no data for symbol={symbol.upper()} period={period} interval={interval}."
        )
    return _normalize_history(frame)
