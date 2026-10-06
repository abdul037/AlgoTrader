"""eToro's own limits decide portfolio room (operator 2026-10-06: "go with option 3").

On 10-06 the swing scan found MSFT, NVDA and AAPL, but every entry was refused at the
proposal step by the PAPER portfolio's room limits (gross, correlated and symbol
exposure on ~$100k, with $28.8k of paper-only positions from before the mirror). The real
eToro account had 3 of 6 positions open and $6,990 free, so it never got a trade to copy.

While the live mirror would copy an order, this module lets eToro's account decide the
room instead of paper's:

* ``room_verdict`` asks the mirror's own ``blockers()`` about the order (state reads only:
  no eToro call and no lock). If the mirror would refuse for any reason other than room
  (disabled, halted, evidence, the 2-a-day cap, the loss stop, not a long equity ...) it
  returns None and paper keeps all its own room checks -- exactly the old behaviour.
  Otherwise it returns eToro's room reasons: [] means paper skips its room checks.
* eToro's room is: one position per symbol, 6 open positions, free cash for the trade
  plus the $100 reserve, and at most ``HARD_MAX_OPEN_PER_BUCKET`` open positions in one
  correlation group (code constant, no setting can raise it).
* ``effective_open_symbols`` is the open set those caps count: reconcile's list, with raw
  instrument ids mapped back to symbols, plus scorecard trades not yet filled or still
  open (reconcile drops orders that have not filled yet).

Every per-trade rule (stop, risk %, loss limits, kill switch, cooldowns, daily bucket
entries, drawdown halts, one paper position per symbol) still applies on paper, and
``mirror()`` re-checks every hard cap under its lock before any real order.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

HARD_MAX_OPEN_PER_BUCKET = 3  # e.g. 3 tech names of 6, each ~10% of the account

ROOM_REASONS = frozenset(
    {
        "etoro_live_symbol_already_open",
        "etoro_live_open_position_cap",
        "etoro_live_insufficient_cash",
        "etoro_live_bucket_cap",
    }
)
_LIVE_TRADE_STATUSES = {"pending_fill", "open"}


def effective_open_symbols(runtime_state: Any, state: dict[str, Any]) -> list[str]:
    """Symbols the eToro caps count: reconcile's list plus mirrored trades in flight."""

    from app.broker.etoro_live_scorecard import SCORECARD_KEY

    cards = _load(runtime_state, SCORECARD_KEY)
    test_order = _load(runtime_state, "etoro_live:test_order")
    by_id: dict[str, str] = {}
    for trade in [*cards.values(), test_order]:
        if isinstance(trade, dict) and trade.get("instrument_id") and trade.get("symbol"):
            by_id[str(trade["instrument_id"])] = str(trade["symbol"]).upper()
    symbols = {by_id.get(str(s), str(s).upper()) for s in state.get("open_symbols") or []}
    for trade in cards.values():
        if isinstance(trade, dict) and trade.get("status") in _LIVE_TRADE_STATUSES:
            symbols.add(str(trade.get("symbol") or "").upper())
    symbols.discard("")
    return sorted(symbols)


def bucket_blockers(symbol: str, open_symbols: list[str]) -> list[str]:
    """At most HARD_MAX_OPEN_PER_BUCKET open eToro positions in one correlation group."""

    from app.risk.sectors import UNKNOWN_BUCKET, correlation_bucket_for_symbol

    bucket = correlation_bucket_for_symbol(symbol)
    if bucket == UNKNOWN_BUCKET:
        return []
    held = sum(1 for s in open_symbols if correlation_bucket_for_symbol(s) == bucket)
    return ["etoro_live_bucket_cap"] if held >= HARD_MAX_OPEN_PER_BUCKET else []


def room_verdict(
    mirror: Any, order: Any, *, signal: Any = None, primary_broker: str = "alpaca"
) -> list[str] | None:
    """eToro's room reasons for this order, or None when paper's room rules apply."""

    if mirror is None or not bool(
        getattr(mirror.settings, "etoro_live_room_authority_enabled", True)
    ):
        return None
    try:
        reasons = mirror.blockers(
            SimpleNamespace(id=None, order=order, signal=signal), primary_broker
        )
    except Exception:  # noqa: BLE001 - any doubt keeps paper's own room checks
        return None
    if any(reason not in ROOM_REASONS for reason in reasons):
        return None  # the mirror would not copy it, so paper's room decides as before
    return list(reasons)


def _load(runtime_state: Any, key: str) -> dict[str, Any]:
    try:
        value = json.loads(runtime_state.get(key) or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}
