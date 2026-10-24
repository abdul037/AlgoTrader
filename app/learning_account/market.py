"""Market data for learning entries: read-only, from the MAIN account's data client.

Stated plainly: the learning account has no data client of its own. Its quotes and the
market-direction bars are read from the Alpaca provider of the app's shared market-data engine
(``MarketDataEngine.alpaca_provider``), which wraps the main account's read-only data client.
Those reads never trade, never touch the main account's orders or positions, and never write
the shared bar cache (``cache=False``), but they do count against the same data rate limit.

* A quote is read ONCE per entry; any failure refuses the entry (no yfinance, eToro or
  history-derived fallback).
* SPY/QQQ 5m bars for the market-direction check come from the same provider (never "auto"
  or eToro data), each benchmark at most once per ``BENCHMARK_TTL_SECONDS`` (failures
  included); the check itself is the main path's (``market_direction_reasons``).
* Every read is capped at ``READ_TIMEOUT_SECONDS`` (the main data client has no timeout of its
  own); a read still running then is "hung", and while more than ``MAX_HUNG_READS`` are hung
  no new read starts (``ReadsHung``: the entry is refused ``market_data_reads_hung``).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from datetime import timedelta
from typing import Any

READ_TIMEOUT_SECONDS = 10.0
MAX_HUNG_READS = 2
BENCHMARK_TTL_SECONDS = 300.0
BENCHMARK_BARS = 200
BENCHMARK_LOOKBACK_DAYS = 7  # the engine's own live window for 200 5m bars


class ReadsHung(RuntimeError):
    """More than ``MAX_HUNG_READS`` reads on the main data client have not returned."""


class ReadTimeout(RuntimeError):
    """A read on the main data client took longer than ``READ_TIMEOUT_SECONDS``."""


class MarketDataUnavailable(RuntimeError):
    """The shared engine has no Alpaca provider (main Alpaca data is not configured)."""


def _spawn(target: Callable[[], None]) -> None:
    threading.Thread(target=target, name="learning-account-read", daemon=True).start()


class BoundedReads:
    """Runs each read on its own daemon thread and waits at most ``timeout`` for it."""

    def __init__(self, *, timeout: float = READ_TIMEOUT_SECONDS, spawn: Callable[..., None] = _spawn, wait: Any = None):
        self.timeout = timeout
        self._spawn = spawn
        self._wait = wait or (lambda event, seconds: event.wait(seconds))
        self._lock = threading.Lock()
        self.hung = 0

    def too_many_hung(self) -> bool:
        with self._lock:
            return self.hung > MAX_HUNG_READS

    def call(self, read: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if self.too_many_hung():
            raise ReadsHung("market_data_reads_hung")
        box: dict[str, Any] = {"finished": False, "hung": False}
        done = threading.Event()

        def run() -> None:
            try:
                box["value"] = read(*args, **kwargs)
            except BaseException as exc:  # noqa: BLE001 - handed to the caller
                box["error"] = exc
            finally:
                with self._lock:
                    box["finished"] = True
                    if box["hung"]:
                        self.hung -= 1
                done.set()

        self._spawn(run)
        if not self._wait(done, self.timeout):
            with self._lock:
                if not box["finished"]:
                    box["hung"] = True
                    self.hung += 1
            if box["hung"]:
                raise ReadTimeout("market_data_read_timeout")
        if "error" in box:
            raise box["error"]
        return box.get("value")


class MarketReads:
    """Quotes and benchmark bars for the learning thread (``now`` gives aware UTC datetimes)."""

    def __init__(self, engine: Any, settings: Any, *, now: Any, reads: BoundedReads | None = None):
        self.engine = engine
        self.settings = settings
        self.now = now
        self.reads = reads or BoundedReads()
        self._benchmarks: dict[str, tuple[Any, Any, Exception | None]] = {}  # symbol -> (at, frame, error)

    def _provider(self) -> Any:
        provider = getattr(self.engine, "alpaca_provider", None)
        if provider is None:
            raise MarketDataUnavailable("alpaca_data_not_configured")
        return provider

    def quote(self, symbol: str) -> Any:
        """One quote from the main account's data client; raises on any failure."""

        return self.reads.call(self._provider().get_quote, symbol.upper())

    def get_history(self, symbol: str, *, timeframe: str = "5m", bars: int = BENCHMARK_BARS, **_: Any) -> Any:
        """A benchmark's 5m bars for ``market_direction_reasons`` (at most one read per symbol
        per ``BENCHMARK_TTL_SECONDS``, failures included; nothing is cached on disk)."""

        symbol = symbol.upper()
        now = self.now()
        cached = self._benchmarks.get(symbol)
        if cached is not None and (now - cached[0]).total_seconds() < BENCHMARK_TTL_SECONDS:
            if cached[2] is not None:
                raise cached[2]
            return cached[1]
        try:
            frame = self.reads.call(
                self._provider().get_bars,
                symbol,
                timeframe=timeframe,
                start=now - timedelta(days=BENCHMARK_LOOKBACK_DAYS),
                end=now,
                cache=False,
            )
            frame = frame.tail(bars).reset_index(drop=True) if frame is not None else frame
        except Exception as exc:  # noqa: BLE001 - remembered for the TTL, like a success
            self._benchmarks[symbol] = (now, None, exc)
            raise
        self._benchmarks[symbol] = (now, frame, None)
        return frame

    def direction_reasons(self, symbol: str, timeframe: str) -> list[str]:
        """The main path's market-direction check, on these reads."""

        from app.risk.market_direction import market_direction_reasons

        if self.reads.too_many_hung():
            return ["market_data_reads_hung"]
        return market_direction_reasons(
            self.settings, self, symbol=symbol, timeframe=timeframe, side="buy", logs=None, now=self.now()
        )
