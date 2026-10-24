"""Reconcile the learning account's book with the learning account (and nothing else).

About once a minute while the market is open and something is live, every 15 min while it is
closed and no flatten is pending (never past the next open), every 10 min when idle. Each trade
is handled on its own:

* an order known only by its client id is re-read by its broker id with nested legs first;
* fills are recorded, and exits with their R from the fills (entry fill to the planned stop);
* a 1m-1h position is flattened before the close, and one entered on an earlier New York day
  at the next regular session (``catch_up_flatten``); an unprotected position (every leg ended,
  none filled) is closed at market: the legs are cancelled first and the position is closed only
  once every leg shows ended, by a market sell with a deterministic client order id
  (``la-x-`` + a hash of the trade id and the attempt); with a flatten reason set but no close
  id stored, that id is looked up before anything is sent again (never sell twice);
* a closed trade's record is appended to the closed list FIRST (idempotent per trade id), then
  the book is saved; if the append fails the trade stays ``closed_unreported`` (it counts toward
  its day, is kept however old and is appended next pass); each exit is logged and sent to
  Telegram once, when its record is in both;
* a position gone without a leg fill or our close was closed by hand: ``closed_outside``.

Every leg cancel and close asks the lease first (``Lease.may_act``). The untracked-position
sweep (every 15 min) logs learning-account positions the book does not hold and sends one
Telegram alert per symbol per day; it never closes anything.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.learning_account import store as records

UNKNOWN_GRACE_SECONDS = 600.0  # an unknown send the broker has no order for, after this long
LIVE_SECONDS, CLOSED_SECONDS, IDLE_SECONDS, SWEEP_SECONDS = 60.0, 900.0, 600.0, 900.0
DEFAULT_FLATTEN_MINUTES = 10.0


def flatten_minutes(settings: Any) -> float:
    value = float(getattr(settings, "intraday_flatten_minutes_before_close", 0.0) or 0.0)
    return value if value > 0 else DEFAULT_FLATTEN_MINUTES


def _parse(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else None
    try:
        parsed = datetime.fromisoformat(str(value)) if value else None
    except (TypeError, ValueError):
        return None
    return parsed if parsed is not None and parsed.tzinfo is not None else None


def flatten_due(settings: Any, trade: dict[str, Any], clock: dict[str, Any] | None, now: datetime) -> str | None:
    """Why an intraday position must be closed now (None: it need not)."""

    from app.backtesting.intraday_plan import is_intraday
    from app.learning_account.account import ny_day

    if not is_intraday(trade.get("timeframe")) or not clock or not clock.get("is_open"):
        return None
    entered = _parse(trade.get("filled_at")) or _parse(trade.get("created_at"))
    if entered is not None and ny_day(entered) < ny_day(now):
        return "catch_up_flatten"
    close = _parse(clock.get("next_close"))
    if close is not None and (close - now).total_seconds() <= flatten_minutes(settings) * 60.0:
        return "session_flatten"
    return None


def r_multiple(entry: Any, exit_price: Any, stop: Any) -> float | None:
    try:
        entry, exit_price, stop = float(entry), float(exit_price), float(stop)
    except (TypeError, ValueError):
        return None
    if entry <= stop:
        return None
    return round((exit_price - entry) / (entry - stop), 4)


def closed_record(trade: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "trade_id", "symbol", "strategy_name", "timeframe", "kind", "basis", "evidence_r", "day",
        "created_at", "submitted_at", "filled_at", "entry_fill", "filled_qty", "stop_loss",
        "take_profit", "exit_price", "exit_reason", "exit_at", "pnl_usd", "r",
    )
    return {key: trade.get(key) for key in keys}


def _save(book: records.Book, account: Any, trade: dict[str, Any]) -> records.Book:
    return records.save_book(account.store, book, {**book.trades, trade["trade_id"]: trade})


def announce(account: Any, trade: dict[str, Any]) -> None:
    account.log("learning_account_exit", closed_record(trade) | {"status": trade.get("status")})
    r = trade.get("r")
    account.notify(
        f"Exit {trade.get('symbol')} ({trade.get('strategy_name')}:{trade.get('timeframe')}, {trade.get('kind')}): "
        f"{trade.get('exit_reason')}",
        f"R {r:+.2f}" if isinstance(r, (int, float)) else "R unknown (closed outside the bot)",
    )


def report(account: Any, book: records.Book, trade: dict[str, Any]) -> records.Book:
    """Append the closed record FIRST, then save the book (closed, or still unreported)."""

    added = records.append_closed(account.store, closed_record(trade))
    final = "closed_outside" if trade.get("exit_reason") == "closed_outside" else "closed"
    trade = {**trade, "status": final if added else "closed_unreported"}
    book = _save(book, account, trade)
    if added:
        announce(account, trade)
    return book


def close_trade(
    account: Any, book: records.Book, trade: dict[str, Any], exit_price: Any, reason: str, exit_at: Any, now: datetime
) -> records.Book:
    entry, qty = trade.get("entry_fill"), trade.get("filled_qty")
    pnl = None
    if exit_price is not None and entry is not None and qty:
        pnl = round((float(exit_price) - float(entry)) * float(qty), 2)
    trade = {
        **trade,
        "exit_price": exit_price,
        "exit_reason": reason,
        "exit_at": exit_at or now.isoformat(),
        "pnl_usd": pnl,
        "r": r_multiple(entry, exit_price, trade.get("stop_loss")) if exit_price is not None else None,
    }
    return report(account, book, trade)


def _legs(order: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"id": leg.get("id"), "type": leg.get("type"), "status": leg.get("status")} for leg in order.get("legs") or []]


def _adopt_entry(trade: dict[str, Any], order: dict[str, Any], now: datetime) -> dict[str, Any]:
    from app.learning_account.client import ended

    trade = {**trade, "broker_order_id": order.get("id") or trade.get("broker_order_id")}
    trade["submitted_at"] = trade.get("submitted_at") or now.isoformat()
    filled = float(order.get("filled_qty") or 0.0)
    status = str(order.get("status") or "")
    if filled > 0 and (status == "filled" or ended(status)):
        trade.update(
            status="open",
            entry_fill=order.get("filled_avg_price"),
            filled_qty=filled,
            filled_at=order.get("filled_at") or now.isoformat(),
            legs=_legs(order),
        )
    elif ended(status):
        trade.update(status="rejected", reason=f"entry_{status}")
    else:
        trade.update(status="submitted", order_status=status)
    return trade


def reconcile_trade(
    account: Any, book: records.Book, trade_id: str, clock: dict[str, Any] | None, positions: dict[str, float] | None, now: datetime
) -> records.Book:
    from app.learning_account.account import UNREPORTED

    client = account.client()
    trade = dict(book.trades[trade_id])
    status = trade.get("status")
    if status == UNREPORTED:
        return report(account, book, trade)
    if status in {"submitting", "unknown"}:
        order = client.order_by_client_id(trade["client_order_id"])  # re-read by broker id, nested
        if order is None:
            sent = _parse(trade.get("submitted_at"))
            if status == "submitting" or sent is None or (now - sent).total_seconds() >= UNKNOWN_GRACE_SECONDS:
                reason = "not_sent:lost" if status == "submitting" else "not_found_at_broker"
                return _save(book, account, {**trade, "status": "rejected", "reason": reason})
            return book
        trade = _adopt_entry(trade, order, now)
        book = _save(book, account, trade)
        if trade["status"] != "open":
            return book
        # Filled since this pass read the positions: no position-based decision until the next.
        return manage_open(account, book, trade, order, clock, None, now)
    if status == "submitted":
        order = client.order(trade["broker_order_id"])
        trade = _adopt_entry(trade, order, now)
        book = _save(book, account, trade)
        if trade["status"] != "open":
            return book
        return manage_open(account, book, trade, order, clock, None, now)
    if status == "open":
        return manage_open(account, book, trade, client.order(trade["broker_order_id"]), clock, positions, now)
    return book


def manage_open(
    account: Any,
    book: records.Book,
    trade: dict[str, Any],
    order: dict[str, Any],
    clock: dict[str, Any] | None,
    positions: dict[str, float] | None,
    now: datetime,
) -> records.Book:
    from app.learning_account.client import ended

    client = account.client()
    legs = list(order.get("legs") or [])
    for leg in legs:
        if leg.get("status") == "filled":
            reason = "take_profit" if leg.get("type") == "limit" else "stop_loss"
            return close_trade(account, book, trade, leg.get("filled_avg_price"), reason, leg.get("filled_at"), now)
    if _legs(order) != trade.get("legs"):
        trade = {**trade, "legs": _legs(order)}
        book = _save(book, account, trade)
    if trade.get("close_order_id"):
        close = client.order(trade["close_order_id"])
        if close.get("status") == "filled":
            return close_trade(account, book, trade, close.get("filled_avg_price"), trade.get("flatten_reason") or "flatten", close.get("filled_at"), now)
        if ended(close.get("status")):  # refused or cancelled: the next attempt has its own id
            trade = {**trade, "close_order_id": None, "close_attempt": int(trade.get("close_attempt") or 0) + 1}
            return _save(book, account, trade)
        return book
    held = None if positions is None else float(positions.get(trade["symbol"], 0.0) or 0.0)
    if held is not None and held <= 0:
        if trade.get("flatten_reason"):
            recovered = _recover_close(account, book, trade, now)
            if recovered is not None:
                return recovered
        return close_trade(account, book, trade, None, "closed_outside", None, now)
    reason = trade.get("flatten_reason") or flatten_due(account.settings, trade, clock, now)
    if reason is None and held is not None and legs and all(ended(leg.get("status")) for leg in legs):
        reason = "unprotected"  # a bracket whose every leg ended unfilled (an order read without legs is no proof)
    if reason is None:
        return book
    return flatten(account, book, trade, legs, reason, held, clock, now)


def _recover_close(account: Any, book: records.Book, trade: dict[str, Any], now: datetime) -> records.Book | None:
    """A close this attempt may already have sent (its id lost with a book write): adopt it."""

    from app.learning_account.account import close_client_order_id

    attempt = int(trade.get("close_attempt") or 0)
    found = account.client().order_by_client_id(close_client_order_id(trade["trade_id"], attempt))
    if found is None:
        return None
    trade = {**trade, "close_order_id": found.get("id"), "close_sent_at": trade.get("close_sent_at") or now.isoformat()}
    if found.get("status") == "filled":
        return close_trade(account, book, trade, found.get("filled_avg_price"), trade.get("flatten_reason") or "flatten", found.get("filled_at"), now)
    return _save(book, account, trade)


def flatten(
    account: Any,
    book: records.Book,
    trade: dict[str, Any],
    legs: list[dict[str, Any]],
    reason: str,
    held: float | None,
    clock: dict[str, Any] | None,
    now: datetime,
) -> records.Book:
    from app.learning_account.account import close_client_order_id
    from app.learning_account.client import ended, refused

    client = account.client()
    if not trade.get("flatten_reason"):  # recorded before anything is cancelled or sold
        trade = {**trade, "flatten_reason": reason, "flatten_at": now.isoformat()}
        book = _save(book, account, trade)
    else:
        recovered = _recover_close(account, book, trade, now)
        if recovered is not None:
            return recovered
    if not account.lease.may_act(account.stopping):
        return book
    open_legs = [leg for leg in legs if not ended(leg.get("status"))]
    if open_legs:  # the position is closed only once every leg shows ended
        for leg in open_legs:
            if not account.lease.may_act(account.stopping):
                break
            client.cancel(leg["id"])
        return book
    if held is None or held <= 0 or not clock or not clock.get("is_open"):
        return book  # the close waits for a known position and a regular session
    attempt = int(trade.get("close_attempt") or 0)
    client_order_id = close_client_order_id(trade["trade_id"], attempt)
    qty = min(float(trade.get("filled_qty") or held), held)
    try:
        close = client.submit_close(symbol=trade["symbol"], qty=qty, client_order_id=client_order_id)
    except Exception as exc:  # noqa: BLE001 - refused: next attempt; unknown: looked up next pass
        if refused(exc):
            return _save(book, account, {**trade, "close_attempt": attempt + 1})
        return book
    trade = {**trade, "close_order_id": close.get("id"), "close_sent_at": now.isoformat()}
    book = _save(book, account, trade)
    account.log("learning_account_flatten", {
        "trade_id": trade["trade_id"], "symbol": trade["symbol"], "reason": trade["flatten_reason"],
        "qty": qty, "client_order_id": client_order_id, "attempt": attempt,
    })
    if close.get("status") == "filled":
        return close_trade(account, book, trade, close.get("filled_avg_price"), trade["flatten_reason"], close.get("filled_at"), now)
    return book


def next_reconcile(book: records.Book, clock: dict[str, Any] | None, now: datetime) -> datetime:
    from app.learning_account.account import LIVE

    live = [trade for trade in book.trades.values() if trade.get("status") in LIVE]
    if not live:
        return now + timedelta(seconds=IDLE_SECONDS)
    if clock is None or clock.get("is_open"):
        return now + timedelta(seconds=LIVE_SECONDS)
    if any(trade.get("flatten_reason") for trade in live):
        return now + timedelta(seconds=LIVE_SECONDS)
    later = now + timedelta(seconds=CLOSED_SECONDS)
    opens = _parse(clock.get("next_open"))
    if opens is not None:
        later = min(later, max(opens, now + timedelta(seconds=LIVE_SECONDS / 2)))
    return later


def reconcile(account: Any, book: records.Book, now: datetime) -> records.Book:
    from app.learning_account.account import FINAL, LIVE, UNREPORTED, ny_day

    clock = account.market_clock()
    pending = sorted(tid for tid, t in book.trades.items() if t.get("status") in LIVE | {UNREPORTED})
    positions = None
    if any(book.trades[tid].get("status") != UNREPORTED for tid in pending):
        try:
            positions = account.client().positions()
        except Exception:  # noqa: BLE001 - no position-based decision this pass
            positions = None
    account.positions_seen = (now, positions)
    for trade_id in pending:
        try:
            book = reconcile_trade(account, book, trade_id, clock, positions, now)
        except records.BookConflict:
            raise
        except Exception as exc:  # noqa: BLE001 - each trade on its own
            account.log_once(
                ("reconcile_failed", trade_id, type(exc).__name__),
                "learning_account_status",
                {"route": account.route, "trade_id": trade_id, "error": f"{type(exc).__name__}: {exc}"[:300]},
            )
    today = ny_day(now)
    kept = {tid: t for tid, t in book.trades.items() if not (t.get("status") in FINAL and str(t.get("day") or "") < today)}
    if len(kept) != len(book.trades):
        book = records.save_book(account.store, book, kept)
    account.reconcile_at = next_reconcile(book, clock, now)
    return book


def sweep_untracked(account: Any, book: records.Book, now: datetime) -> None:
    from app.learning_account.account import LIVE

    account.sweep_at = now + timedelta(seconds=SWEEP_SECONDS)
    seen_at, positions = getattr(account, "positions_seen", (None, None))
    if seen_at != now or positions is None:
        try:
            positions = account.client().positions()
        except Exception:  # noqa: BLE001 - next sweep
            return
    tracked = {t.get("symbol") for t in book.trades.values() if t.get("status") in LIVE}
    for symbol in sorted(set(positions) - tracked):
        payload = {"symbol": symbol, "qty": positions[symbol]}
        if account.log_once(("untracked", symbol), "learning_account_untracked_position", payload):
            account.notify(
                f"Untracked position on the learning account: {symbol} x{positions[symbol]:g}.",
                "Not in its book; nothing was closed. Close it on the Alpaca dashboard if it is not wanted.",
            )
