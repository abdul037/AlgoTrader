"""Copy paper exits to eToro LIVE (operator request 2026-10-06: "start the exit copying build").

The mirror copies entries only. eToro then exits a mirrored trade solely at its stop or
target, while the paper bot can also close early (time limit, signal exit, end-of-day
flatten, an operator or safety flatten). Live results then drift from what the backtest
models, which defeats the live test. This step follows the paper position: once the
paper account is flat in a mirrored symbol, the eToro position is closed at market and
booked in the scorecard with exit reason ``paper_exit``.

A wrong "flat" would close a real position, so it fails closed:

* acts only while the US market is open (Alpaca's clock), when paper exits happen;
* the paper position must have been seen open first (``paper_seen``);
* the symbol must read flat on ``FLAT_READS_TO_CLOSE`` consecutive guard ticks;
* any failed or malformed paper read changes nothing and resets nothing.

eToro's own stop and target stay on the position throughout, so a missed or failed copy
leaves the trade protected, never naked.
"""

from __future__ import annotations

from typing import Any

from app.broker.etoro_rate_limit import EToroRateLimitError
from app.utils.time import utc_now

FLAT_READS_TO_CLOSE = 2
_GONE_MARKERS = ("not found", "does not exist", "already closed")


def paper_reader(alpaca: Any) -> Any:
    """A reader for the guard: () -> (market_open, symbols held on paper)."""

    def read() -> tuple[bool, set[str]]:
        is_open = bool(alpaca.is_regular_market_open())
        if not is_open:
            return False, set()
        portfolio = alpaca.get_portfolio()
        positions = getattr(portfolio, "positions", None)
        if not isinstance(positions, list):
            raise ValueError("malformed paper portfolio")
        held = {str(p.symbol).upper() for p in positions if abs(float(p.quantity or 0.0)) > 0}
        return True, held

    return read


def copy_paper_exits(mirror: Any, skip_symbols: set[str] | None = None) -> list[dict[str, Any]]:
    """Close eToro positions whose paper position has gone flat. Returns the closes."""

    from app.broker.etoro_live_backup_stop import _lock, forget_stop, fresh_rate
    from app.broker.etoro_live_mirror import client_problem
    from app.broker.etoro_live_scorecard import _load, _mark_closed, _save

    read = getattr(mirror, "paper_reader", None)
    if read is None:
        return []
    with _lock(mirror):
        cards = _load(mirror)
        live = {
            k: t for k, t in cards.items() if t.get("status") == "open" and t.get("position_ids")
        }
        if not live or client_problem(mirror.client):
            return []
        try:
            market_open, held = read()
        except Exception as exc:  # noqa: BLE001 - unknown paper state: change nothing
            mirror.logs.log("etoro_live_exit_copy_read_failed", {"error": str(exc)})
            return []
        if not market_open:
            return []
        skip = {s.upper() for s in skip_symbols or set()}
        closes: list[dict[str, Any]] = []
        for trade in live.values():
            symbol = str(trade.get("symbol") or "").upper()
            if symbol in held:
                trade["paper_seen"] = True
                trade["paper_flat_reads"] = 0
                continue
            if not trade.get("paper_seen") or symbol in skip:
                continue
            trade["paper_flat_reads"] = int(trade.get("paper_flat_reads") or 0) + 1
            if trade["paper_flat_reads"] < FLAT_READS_TO_CLOSE:
                continue
            record = _close(mirror, trade, symbol)
            if record is None:
                continue
            try:
                rate = fresh_rate(mirror.client, symbol)
            except Exception:  # noqa: BLE001 - book it without a price; the open rate stays
                rate = None
            _mark_closed(trade, rate or float(trade.get("open_rate") or 0.0), "paper_exit")
            if rate is None:  # closed, but the exit price is unknown: keep it out of the R stats
                trade.update(
                    close_rate=None, realized_r=None, pnl_usd=None, close_rate_missing=True
                )
            trade["exit_copied_at"] = utc_now().isoformat()
            forget_stop(mirror, symbol)
            record.update(rate=rate, realized_r=trade.get("realized_r"))
            mirror.logs.log("etoro_live_exit_copied", record)
            mirror._notify(
                f"eToro LIVE: closed {symbol} because the paper bot exited it"
                + (f" (about {rate:,.2f})." if rate else ".")
            )
            closes.append(record)
        current = _load(mirror)
        current.update({k: t for k, t in live.items() if k in current})
        _save(mirror, current)
        return closes


def _close(mirror: Any, trade: dict[str, Any], symbol: str) -> dict[str, Any] | None:
    """Close every eToro position of the trade; None when it should be retried next tick."""

    instrument_id = int(trade.get("instrument_id") or 0)
    record: dict[str, Any] = {
        "symbol": symbol,
        "position_ids": [],
        "strategy": trade.get("strategy_name"),
    }
    for position_id in trade.get("position_ids") or []:
        try:
            mirror.client.close_position_by_id(int(position_id), instrument_id)
            record["position_ids"].append(position_id)
        except Exception as exc:  # noqa: BLE001
            message = str(exc).lower()
            if any(marker in message for marker in _GONE_MARKERS):
                record["position_ids"].append(position_id)  # already closed at eToro
                record["already_closed"] = True
                continue
            event = "etoro_live_exit_copy_rate_limited"
            if not isinstance(exc, EToroRateLimitError):
                event = "etoro_live_exit_copy_failed"
                mirror._notify(
                    f"eToro LIVE: couldn't close {symbol} after the paper exit ({exc}). "
                    "Its eToro stop and target still protect it; retrying each minute."
                )
            mirror.logs.log(event, {"symbol": symbol, "error": str(exc)})
            return None
    return record
