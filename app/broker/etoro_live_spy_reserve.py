"""AlgoBot's idle cash held in SPY (operator 2026-11-04: "SPY A, and buy SPY today").

The live copies used about a third of the AlgoBot account and the rest sat in cash earning
nothing; the research (docs/research/defensive_trend_study.md) found plain buy-and-hold SPY
beat every rule tested. Version A, the operator's choice: keep the idle cash in one SPY
position, always held, and sell just enough of it whenever a copy needs the cash.

* Top-up: once per New York trading day, between 09:45 and 15:45 New York while the market is
  open (Alpaca's clock, so holidays buy nothing), buy SPY with the cash above
  ``CASH_BUFFER_USD`` (kept free so the day's first copies need no sale) and the mirror's
  ``CASH_RESERVE_USD``. The holding never goes above ``HARD_MAX_RESERVE_PCT`` of equity at
  cost, and a top-up under ``MIN_TOPUP_USD`` is skipped. The day is written before the order
  (write-ahead), so a timeout can never buy twice in one day.
* Release: a copy that needs more than the free cash first sells SPY units worth the shortfall
  plus ``RELEASE_MARGIN_PCT`` (a partial close; the whole lot when under ``MIN_LEFT_USD`` would
  be left), then waits up to ``RELEASE_WAIT_SECONDS`` for eToro's cash to show it. A failed sale
  refuses that copy only (no halt); the paper trade is never touched.
* The holding has no stop or target (version A: always held), so reconcile's close of a
  position without a stop skips SPY. It counts toward neither the 9-position cap nor the group
  cap, but it does count toward the 100%-of-equity exposure cap, where a copy's room is credited
  with ``SELLABLE_HAIRCUT`` of its cost (what the bot can count on selling it for).
* Your copy follows: eToro copies AlgoBot's SPY buys and sales in proportion (5% of AlgoBot).

``etoro_live_spy_reserve_enabled`` off stops top-ups and sales and gives SPY back to reconcile's
unprotected-position close, which then sells it: off means out of SPY.
"""

from __future__ import annotations

import json
import math
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.utils.time import utc_now

RESERVE_SYMBOL = "SPY"
RESERVE_KEY = "etoro_live:spy_reserve"
CASH_BUFFER_USD = 2_500.0
HARD_MAX_RESERVE_PCT = 60.0
MIN_TOPUP_USD = 50.0
SELLABLE_HAIRCUT = 0.9
RELEASE_MARGIN_PCT = 1.0
MIN_LEFT_USD = 20.0
RELEASE_WAIT_SECONDS = 15.0
RELEASE_POLL_SECONDS = 3.0
_NEW_YORK = ZoneInfo("America/New_York")
WINDOW_START = (9, 45)
WINDOW_END = (15, 45)


class ReserveError(RuntimeError):
    """The SPY sale a copy needed could not be made; that copy is refused."""


def enabled(settings: Any) -> bool:
    return bool(getattr(settings, "etoro_live_spy_reserve_enabled", False))


def is_reserve_symbol(settings: Any, symbol: Any) -> bool:
    return enabled(settings) and str(symbol or "").upper() == RESERVE_SYMBOL


def reserve_instrument_id(settings: Any, runtime_state: Any) -> int | None:
    """SPY's eToro instrument id once the reserve has looked it up (None when off or unknown).
    Reconcile names positions by it, so a failed symbol lookup (which leaves the raw id as the
    name) can never make the stop-less SPY look like an unprotected trade to be closed."""

    if not enabled(settings):
        return None
    known = load(runtime_state).get("instrument_id")
    try:
        return int(known) if known else None
    except (TypeError, ValueError):
        return None


def position_symbol(position: Any, spy_id: int | None) -> str:
    """A portfolio position's symbol, with the reserve's instrument id always named SPY."""

    if spy_id is not None and getattr(position, "instrument_id", None) == spy_id:
        return RESERVE_SYMBOL
    return str(getattr(position, "symbol", "") or "").upper()


def counted_symbols(settings: Any, held: list[str]) -> list[str]:
    """The open symbols the position and group caps count: the SPY reserve is not a trade."""

    return [s for s in held if not is_reserve_symbol(settings, s)]


def sellable_usd(settings: Any, mirror_state: dict[str, Any]) -> float:
    """What a copy's room may count on from the holding: its cost at the last reconcile or
    top-up (``open_notional``) less the haircut; 0 when off or unknown."""

    if not enabled(settings):
        return 0.0
    notional = mirror_state.get("open_notional")
    row = notional.get(RESERVE_SYMBOL) if isinstance(notional, dict) else None
    cost = _num(row.get("cost_usd")) if isinstance(row, dict) else None
    return round(cost * SELLABLE_HAIRCUT, 2) if cost is not None and cost > 0 else 0.0


def in_window(now: datetime) -> tuple[bool, str]:
    """(inside the top-up window on a weekday, the New York date)."""

    ny = now.astimezone(_NEW_YORK)
    start = ny.replace(hour=WINDOW_START[0], minute=WINDOW_START[1], second=0, microsecond=0)
    end = ny.replace(hour=WINDOW_END[0], minute=WINDOW_END[1], second=0, microsecond=0)
    return ny.weekday() < 5 and start <= ny < end, ny.date().isoformat()


def load(runtime_state: Any) -> dict[str, Any]:
    try:
        value = json.loads(runtime_state.get(RESERVE_KEY) or "{}")
    except (TypeError, ValueError):
        value = {}
    return value if isinstance(value, dict) else {}


def _save(runtime_state: Any, value: dict[str, Any]) -> None:
    runtime_state.set(RESERVE_KEY, json.dumps(value))


def _num(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _positions(raw: Any) -> list[dict[str, Any]]:
    portfolio = (raw or {}).get("clientPortfolio") or {} if isinstance(raw, dict) else {}
    return [p for p in portfolio.get("positions") or [] if isinstance(p, dict)]


def _credit(raw: Any) -> float | None:
    portfolio = (raw or {}).get("clientPortfolio") or {} if isinstance(raw, dict) else {}
    return _num(portfolio.get("credit"))


def _instrument_id(mirror: Any, reserve: dict[str, Any]) -> int:
    known = reserve.get("instrument_id")
    return int(known) if known else int(mirror.client._search_instrument(RESERVE_SYMBOL)["instrument_id"])


def _held(raw: Any, instrument_id: int) -> list[dict[str, Any]]:
    return [
        p for p in _positions(raw)
        if int(p.get("instrumentID") or 0) == instrument_id and p.get("isBuy", True) is not False
    ]


# -- top-up (guard tick) -------------------------------------------------------
def top_up(mirror: Any) -> dict[str, Any] | None:
    """Buy SPY with the idle cash, at most once a New York trading day; None when not due."""

    settings = mirror.settings
    if not enabled(settings) or not bool(getattr(settings, "etoro_live_mirror_enabled", False)):
        return None
    now = utc_now()
    inside, day = in_window(now)
    if not inside or load(mirror.state).get("last_topup_day") == day:
        return None
    from app.broker.etoro_live_mirror import HALTED_KEY, client_problem

    if mirror.state.get(HALTED_KEY) or client_problem(mirror.client):
        return None
    market_open = getattr(mirror, "market_open", None)
    if market_open is None or not bool(market_open()):  # no clock, a holiday or an early close
        return None
    with mirror.lock:
        reserve = load(mirror.state)
        if reserve.get("last_topup_day") == day:  # another tick or container got here first
            return None
        return _top_up(mirror, reserve, day, now)


def _top_up(mirror: Any, reserve: dict[str, Any], day: str, now: datetime) -> dict[str, Any] | None:
    from app.broker.etoro_live_backup_stop import fresh_rate
    from app.broker.etoro_live_mirror import CASH_RESERVE_USD, account_value_at_cost
    from app.broker.etoro_rate_limit import EToroRateLimitError
    from app.models.trade import TradeOrder

    client = mirror.client
    raw = client.fetch_raw_portfolio()
    cash = _credit(raw)
    equity = account_value_at_cost(raw) if cash is not None else None
    if cash is None or equity is None or equity <= 0:
        mirror.logs.log("etoro_live_spy_reserve_skipped", {"reason": "account_unreadable"})
        return None  # nothing written: the next tick tries again
    instrument_id = _instrument_id(mirror, reserve)
    resolver = getattr(client, "resolver", None)
    if resolver is not None:
        try:
            resolver.resolve(RESERVE_SYMBOL)
        except ValueError as exc:  # not on the allowed list: the order would be refused
            _save(mirror.state, {**reserve, "instrument_id": instrument_id, "last_topup_day": day})
            mirror.logs.log("etoro_live_spy_reserve_skipped", {"reason": "spy_not_allowed", "error": str(exc)[:160]})
            return None
    held_cost = round(sum(_num(p.get("amount")) or 0.0 for p in _held(raw, instrument_id)), 2)
    cap_room = equity * HARD_MAX_RESERVE_PCT / 100.0 - held_cost
    cash_room = cash - CASH_BUFFER_USD - CASH_RESERVE_USD
    amount = round(min(cap_room, cash_room), 2)
    summary = {"day": day, "equity": equity, "cash": cash, "held_cost": held_cost, "amount": amount}
    reserve.update(instrument_id=instrument_id, last_topup_day=day, checked_at=now.isoformat())
    if amount < MIN_TOPUP_USD:
        _save(mirror.state, reserve)
        mirror.logs.log("etoro_live_spy_reserve_checked", {**summary, "bought": 0.0})
        return {**summary, "bought": 0.0}
    rate = fresh_rate(client, RESERVE_SYMBOL)
    if not rate or rate <= 0:
        mirror.logs.log("etoro_live_spy_reserve_skipped", {**summary, "reason": "rate_unknown"})
        return None  # nothing written: the next tick tries again
    # Write-ahead: the day, the spend and the holding are saved BEFORE the POST, so a timeout
    # whose order did fill is never bought again today and the copies see the cash as spent.
    _save(mirror.state, {**reserve, "pending": {"amount_usd": amount, "at": now.isoformat()}})
    state = mirror._state()
    before = json.loads(json.dumps(state))
    state["open_symbols"] = sorted(set(state.get("open_symbols") or []) | {RESERVE_SYMBOL})
    notional = state.get("open_notional")
    notional = dict(notional) if isinstance(notional, dict) else {}
    row = dict(notional.get(RESERVE_SYMBOL) or {})
    row["cost_usd"] = round((_num(row.get("cost_usd")) or held_cost) + amount, 2)
    notional[RESERVE_SYMBOL] = row
    state["open_notional"] = notional
    if state.get("last_cash") is not None:
        state["last_cash"] = round(float(state["last_cash"]) - amount, 2)
    mirror._save(state)
    order = TradeOrder(
        symbol=RESERVE_SYMBOL, amount_usd=amount, leverage=1, proposed_price=rate,
        strategy_name="spy_reserve", rationale="idle cash held in SPY (operator 2026-11-04)",
    )
    try:
        response = client.open_market_order_by_amount(order, client_order_id=f"etoro-live:spy-reserve:{day}")
    except Exception as exc:  # noqa: BLE001 - never halts the copies
        refused = isinstance(exc, EToroRateLimitError) or "with status 4" in str(exc)
        if refused:  # never sent or refused: nothing was bought
            mirror._save(before)
        if isinstance(exc, EToroRateLimitError):
            reserve.pop("last_topup_day", None)  # try again on a later tick
        _save(mirror.state, {**reserve, "last_error": str(exc)[:200]})
        mirror.logs.log("etoro_live_spy_reserve_failed", {**summary, "error": str(exc)[:300], "refused": refused})
        return None
    status = str(getattr(response, "status", "") or "").lower()
    if status in {"rejected", "cancelled"}:
        mirror._save(before)
        _save(mirror.state, {**reserve, "last_error": f"order {status}"})
        mirror.logs.log("etoro_live_spy_reserve_failed", {**summary, "status": status})
        return None
    bought = {"amount_usd": amount, "rate": rate, "order_id": getattr(response, "order_id", None), "status": status, "at": now.isoformat()}
    _save(mirror.state, {**reserve, "last_buy": bought})
    mirror.logs.log("etoro_live_spy_reserve_bought", {**summary, **bought})
    mirror._notify(f"eToro LIVE: bought ${amount:,.0f} of SPY with idle cash (held: ${held_cost + amount:,.0f}).")
    return {**summary, "bought": amount}


# -- release (inside the mirror's copy, under its lock) -----------------------
def release_for_copy(mirror: Any, state: dict[str, Any], amount: float) -> dict[str, Any]:
    """Sell SPY worth the copy's cash shortfall before it is placed and return the mirror state
    to carry on with. No-op when off, when the free cash suffices or was never read."""

    from app.broker.etoro_live_backup_stop import fresh_rate
    from app.broker.etoro_live_mirror import CASH_RESERVE_USD

    if not enabled(mirror.settings):
        return state
    cash = _num(state.get("last_cash"))
    if cash is None:
        return state
    short = round(float(amount) - (cash - CASH_RESERVE_USD), 2)
    if short <= 0:
        return state
    if sellable_usd(mirror.settings, state) <= 0:
        raise ReserveError("no_spy_to_sell")
    client = mirror.client
    reserve = load(mirror.state)
    raw = client.fetch_raw_portfolio()
    instrument_id = _instrument_id(mirror, reserve)
    held = sorted(_held(raw, instrument_id), key=lambda p: -(_num(p.get("units")) or 0.0))
    rate = fresh_rate(client, RESERVE_SYMBOL)
    if not held or not rate or rate <= 0:
        raise ReserveError("spy_not_held" if not held else "spy_rate_unknown")
    need_units = short * (1 + RELEASE_MARGIN_PCT / 100.0) / rate
    sold: list[dict[str, Any]] = []
    for position in held:
        units = _num(position.get("units")) or 0.0
        if need_units <= 0 or units <= 0:
            break
        take = min(units, need_units)
        whole = (units - take) * rate < MIN_LEFT_USD
        client.close_position_by_id(
            int(position["positionID"]), instrument_id, units_to_deduct=None if whole else round(take, 6)
        )
        sold.append({"position_id": position["positionID"], "units": units if whole else round(take, 6), "whole": whole})
        need_units -= units if whole else take
    if need_units > 1e-9:
        raise ReserveError("spy_holding_too_small")
    before_credit = _credit(raw)
    confirmed = None
    deadline = time.monotonic() + RELEASE_WAIT_SECONDS
    while time.monotonic() < deadline:
        time.sleep(RELEASE_POLL_SECONDS)
        credit = _credit(client.fetch_raw_portfolio())
        if credit is not None and before_credit is not None and credit >= before_credit + short * 0.95:
            confirmed = credit
            break
    value = round(sum(s["units"] for s in sold) * rate, 2)
    if confirmed is not None:
        state["last_cash"] = round(confirmed, 2)
    notional = state.get("open_notional")
    if isinstance(notional, dict) and isinstance(notional.get(RESERVE_SYMBOL), dict):
        row = dict(notional[RESERVE_SYMBOL])
        row["cost_usd"] = round(max((_num(row.get("cost_usd")) or 0.0) - value, 0.0), 2)
        state["open_notional"] = {**notional, RESERVE_SYMBOL: row}
    mirror._save(state)
    event = {"short_usd": short, "sold": sold, "rate": rate, "value_usd": value, "cash_confirmed": confirmed is not None}
    _save(mirror.state, {**reserve, "instrument_id": instrument_id, "last_release": {**event, "at": utc_now().isoformat()}})
    mirror.logs.log("etoro_live_spy_reserve_released", event)
    return state
