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

import json
from datetime import timedelta
from typing import Any

from app.broker.etoro_rate_limit import EToroRateLimitError
from app.utils.time import utc_now

FLAT_READS_TO_CLOSE = 2
_GONE_MARKERS = ("not found", "does not exist", "already closed")
# Paper safety flattens are NOT copied (operator 2026-10-06, "build the exception"): a
# breaker trip / emergency stop or an unprotected-position close on paper is plumbing, not
# a strategy exit, and paper alarms have been false before. The paper side writes a marker
# before it closes anything; a live trade whose paper position vanished after such a
# marker is detached (left on eToro's own stop/target and the backup stop), never closed.
SAFETY_KEY = "paper_safety_flatten"
KILL_SWITCH_KEY = "automation:kill_switch"
SAFETY_WINDOW_HOURS = 24  # a flatten queued after hours fills at the next open


def mark_paper_safety_flatten(runtime_state: Any, *, symbol: str | None, reason: str) -> None:
    """Record a paper safety flatten (all positions when ``symbol`` is None). Never raises."""

    try:
        try:
            data = json.loads(runtime_state.get(SAFETY_KEY) or "{}")
        except (TypeError, ValueError):
            data = {}
        data = data if isinstance(data, dict) else {}
        now = utc_now().isoformat()
        if symbol is None:
            data["all_at"] = now
        else:
            data.setdefault("symbols", {})[str(symbol).upper()] = now
        data["last_reason"] = reason
        runtime_state.set(SAFETY_KEY, json.dumps(data))
    except Exception:  # noqa: BLE001 - a marker failure must never block a safety flatten
        pass


def _safety_flatten(runtime_state: Any, trade: dict[str, Any], symbol: str) -> str | None:
    """Why this trade's paper exit must not be copied, or None for a strategy exit."""

    if str(runtime_state.get(KILL_SWITCH_KEY) or "").strip().lower() in {"1", "true", "yes"}:
        return "paper_kill_switch_on"
    try:
        data = json.loads(runtime_state.get(SAFETY_KEY) or "{}")
    except (TypeError, ValueError):
        return "paper_safety_marker_unreadable"  # fail closed: detach, don't close
    if not isinstance(data, dict):
        return "paper_safety_marker_unreadable"
    cutoff = (utc_now() - timedelta(hours=SAFETY_WINDOW_HOURS)).isoformat()
    opened = str(trade.get("opened_at") or "")
    symbols = data.get("symbols") if isinstance(data.get("symbols"), dict) else {}
    for at, why in (
        (data.get("all_at"), "paper_emergency_flatten"),
        (symbols.get(symbol), "paper_unprotected_flatten"),
    ):
        if at and str(at) > cutoff and str(at) >= opened:
            return why
    return None


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
            if trade.get("exit_copy") == "detached":
                continue  # rides eToro's own stop/target from here
            if symbol in held:
                trade["paper_seen"] = True
                trade["paper_flat_reads"] = 0
                continue
            if not trade.get("paper_seen") or symbol in skip:
                continue
            trade["paper_flat_reads"] = int(trade.get("paper_flat_reads") or 0) + 1
            if trade["paper_flat_reads"] < FLAT_READS_TO_CLOSE:
                continue
            reason = _safety_flatten(mirror.state, trade, symbol)
            if reason is not None:
                trade.update(exit_copy="detached", detached_reason=reason)
                trade["detached_at"] = utc_now().isoformat()
                mirror.logs.log(
                    "etoro_live_exit_copy_detached", {"symbol": symbol, "reason": reason}
                )
                mirror._notify(
                    f"eToro LIVE: the paper bot closed {symbol} for safety ({reason}). The eToro "
                    "trade stays open on its own stop and target; close it in eToro if you want out."
                )
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
