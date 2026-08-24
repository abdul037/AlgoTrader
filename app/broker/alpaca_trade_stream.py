"""Real-time Alpaca order fill/exit stream (``trade_updates`` websocket).

The periodic reconciliation sweep already ingests fills, but only on its cadence
-- a fill or a bracket exit is not recorded until the next sweep. This stream
subscribes to Alpaca ``trade_updates`` and, on each terminal/fill event for an
order we placed, calls back into the reconciliation service's
``ingest_order_update`` so the execution record and realized PnL update
immediately. The sweep stays as the backstop for anything the stream drops.

The websocket runs on a dedicated daemon thread with its own asyncio loop and a
reconnect loop, and is gated behind ``alpaca_trade_stream_enabled`` (off by
default). The event-parsing logic is factored into the pure
:func:`extract_order_update` so it can be unit-tested without a live socket.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Events that change an order's terminal/fill state and are worth ingesting.
RELEVANT_EVENTS = frozenset(
    {
        "fill",
        "partial_fill",
        "canceled",
        "cancelled",
        "expired",
        "rejected",
        "replaced",
        "done_for_day",
    }
)


@dataclass(frozen=True)
class TradeUpdateInfo:
    """Normalized view of one trade update."""

    order_id: str | None
    event: str | None
    status: str | None

    @property
    def is_relevant(self) -> bool:
        return bool(self.order_id) and (self.event or "") in RELEVANT_EVENTS


def _read(source: Any, key: str) -> Any:
    """Read ``key`` from either a mapping or an attribute holder."""

    if source is None:
        return None
    if isinstance(source, dict):
        return source.get(key)
    return getattr(source, key, None)


def extract_order_update(update: Any) -> TradeUpdateInfo:
    """Pull the order id, event, and status out of a trade update, defensively.

    Accepts an alpaca-py ``TradeUpdate`` object, a raw dict, or anything with
    the same shape, so it is safe against SDK version differences.
    """

    event = _read(update, "event")
    order = _read(update, "order")
    order_id = _read(order, "id")
    status = _read(order, "status")
    return TradeUpdateInfo(
        order_id=str(order_id) if order_id is not None else None,
        event=str(event).lower() if event is not None else None,
        status=str(status).lower() if status is not None else None,
    )


class AlpacaTradeStream:
    """Run the Alpaca trade-update websocket on a supervised background thread."""

    def __init__(
        self,
        *,
        api_key: str,
        secret_key: str,
        paper: bool,
        on_order_update: Callable[[str], Any],
        run_logs: Any | None = None,
        reconnect_seconds: float = 5.0,
        stream_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._api_key = api_key
        self._secret_key = secret_key
        self._paper = paper
        self._on_order_update = on_order_update
        self._run_logs = run_logs
        self._reconnect_seconds = max(float(reconnect_seconds), 1.0)
        self._stream_factory = stream_factory
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._stream: Any = None
        self._ingested = 0

    def status(self) -> dict[str, Any]:
        """Lightweight liveness snapshot for the readiness probe."""

        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "connected": self._stream is not None,
            "ingested": self._ingested,
        }

    # -- event handling (pure-ish, unit tested) ------------------------------

    def handle_update(self, update: Any) -> bool:
        """Dispatch one trade update. Returns True if it was ingested."""

        info = extract_order_update(update)
        if not info.is_relevant:
            return False
        try:
            self._on_order_update(info.order_id)
            self._ingested += 1
            return True
        except Exception as exc:  # noqa: BLE001 - a bad event must not kill the socket
            logger.exception("trade stream ingest failed for %s: %s", info.order_id, exc)
            self._log("alpaca_trade_stream_error", {"order_id": info.order_id, "error": str(exc)})
            return False

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="alpaca-trade-stream", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        stream = self._stream
        if stream is not None:
            try:
                stream.stop()
            except Exception:  # noqa: BLE001 - best-effort shutdown
                logger.debug("trade stream stop() raised", exc_info=True)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=self._reconnect_seconds + 2)

    def _build_stream(self) -> Any:
        if self._stream_factory is not None:
            return self._stream_factory()
        # Imported lazily so the app does not hard-depend on the streaming extra.
        from alpaca.trading.stream import TradingStream

        return TradingStream(self._api_key, self._secret_key, paper=self._paper)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                stream = self._build_stream()
                self._stream = stream

                async def _handler(update: Any) -> None:
                    self.handle_update(update)

                stream.subscribe_trade_updates(_handler)
                self._log("alpaca_trade_stream_connected", {})
                stream.run()  # blocks until the socket closes or stop() is called
            except Exception as exc:  # noqa: BLE001 - reconnect on any transport error
                logger.exception("alpaca trade stream error: %s", exc)
                self._log("alpaca_trade_stream_disconnected", {"error": str(exc)})
            finally:
                self._stream = None
            if self._stop.is_set():
                break
            self._stop.wait(self._reconnect_seconds)

    def _log(self, event: str, payload: dict[str, Any]) -> None:
        if self._run_logs is None:
            return
        try:
            self._run_logs.log(event, payload)
        except Exception:  # noqa: BLE001 - logging must never break the stream
            logger.debug("trade stream could not record run log", exc_info=True)
