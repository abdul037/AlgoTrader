"""eToro's own limits decide portfolio room (operator 2026-10-06: "go with option 3").

On 10-06 the swing scan found MSFT, NVDA and AAPL, but every entry was refused at the
proposal step by the PAPER portfolio's room limits (gross, correlated and symbol
exposure on ~$100k, with $28.8k of paper-only positions from before the mirror). The real
eToro account had 3 of 6 positions open and $6,990 free, so it never got a trade to copy.

While the live mirror would copy an order, this module lets eToro's account decide the
room instead of paper's:

* ``room_verdict`` asks the mirror's own ``blockers()`` about the order (state reads only:
  no eToro call and no lock). If the mirror would refuse for any reason other than room
  (disabled, halted, evidence, the 3-a-day cap, the loss stop, not a long equity ...) it
  returns None and paper keeps all its own room checks -- exactly the old behaviour.
  Otherwise it returns eToro's room reasons: [] means paper skips its room checks.
* eToro's room is: one position per symbol, 9 open positions (6 until 10-07), free cash for the trade
  plus the $100 reserve, the exposure cap below, and at most ``HARD_MAX_OPEN_PER_BUCKET`` open positions in one
  correlation group (code constant, no setting can raise it).
* ``effective_open_symbols`` is the open set those caps count: reconcile's list, with raw
  instrument ids mapped back to symbols, plus scorecard trades not yet filled or still
  open (reconcile drops orders that have not filled yet).

* the exposure cap (operator 2026-10-26): eToro's open notional plus the copy within 100% of
  equity (``open_exposure_usd``; the mirror cuts a stake that does not fit, or refuses it as
  ``etoro_live_exposure_cap``). It is a room reason on purpose: it is the free-cash check
  counted in notional (with 1x positions at cost the two rooms differ only by the $100
  reserve, and they are refused together), so like the cash, position and group caps it
  keeps eToro's room in charge: the entry is refused on paper too ("eToro live room: ...").
  Were it not a room reason, an order refused for cash AND exposure would hand the decision
  back to paper's room and paper would take, uncopied, what today it refuses.
  Unreadable exposure (``etoro_live_exposure_unknown``) is not a room reason: it returns None
  and paper's own room rules decide, as for an unknown equity.

Every per-trade rule (stop, risk %, loss limits, kill switch, cooldowns, daily bucket
entries, drawdown halts, one paper position per symbol) still applies on paper, and
``mirror()`` re-checks every hard cap under its lock before any real order.
"""

from __future__ import annotations

import json
import math
from contextlib import suppress
from types import SimpleNamespace
from typing import Any

HARD_MAX_OPEN_PER_BUCKET = 4  # operator 2026-10-07: 3 -> 4 with 9 positions (e.g. 4 tech of 9)
COPY_FAILED_KEY = "etoro_live:copy_failed"
COPY_FAILED_HOLD_MINUTES = 30

ROOM_REASONS = frozenset(
    {
        "etoro_live_symbol_already_open",
        "etoro_live_open_position_cap",
        "etoro_live_insufficient_cash",
        "etoro_live_bucket_cap",
        "etoro_live_exposure_cap",  # operator 2026-10-26 (see the module docstring)
    }
)
_LIVE_TRADE_STATUSES = {"pending_fill", "open"}
MARK_MAX_AGE_MINUTES = 15  # an eToro mark older than this is not a current value (as the page)


def open_exposure_usd(runtime_state: Any, state: dict[str, Any]) -> float | None:
    """eToro's open notional for the exposure cap, or None when it cannot be read.

    The positions are the room check's own (``effective_open_symbols``: reconcile's read with
    the mirror's write-ahead, plus the scorecard's trades in flight or open). Each counts once,
    at the LARGEST of what is known about it: reconcile's notional at cost (eToro ``amount`` x
    ``leverage``, ``open_notional`` in the mirror state), its live scorecard trades' notional,
    and its current value (units x the backup stop's eToro mark, at most
    ``MARK_MAX_AGE_MINUTES`` old). The larger is the conservative choice against an equity
    read at cost: an open gain never adds room and an open loss never frees any. A held
    position with no notional at all, an unreadable record or a malformed row is unknown."""

    from datetime import datetime, timedelta

    from app.broker.etoro_live_backup_stop import MARKS_KEY
    from app.broker.etoro_live_scorecard import SCORECARD_KEY
    from app.utils.time import utc_now

    try:
        cards, marks = (json.loads(runtime_state.get(k) or "{}") for k in (SCORECARD_KEY, MARKS_KEY))
    except (TypeError, ValueError):
        return None
    notional = state.get("open_notional", {})
    if not all(isinstance(record, dict) for record in (cards, marks, notional)):
        return None
    by_id = _ids_to_symbols(runtime_state, cards)
    costs: dict[str, list[float]] = {}
    units: dict[str, float] = {}
    for key, row in notional.items():
        symbol = by_id.get(str(key), str(key).upper())
        if not isinstance(row, dict) or row.get("unreadable"):
            return None
        if _positive(row.get("cost_usd")):
            costs.setdefault(symbol, []).append(float(row["cost_usd"]))
        units[symbol] = max(units.get(symbol, 0.0), _positive(row.get("units")) or 0.0)
    live: dict[str, float] = {}
    for trade in cards.values():
        if isinstance(trade, dict) and trade.get("status") in _LIVE_TRADE_STATUSES:
            symbol = str(trade.get("symbol") or "").upper()
            amount = _positive(trade.get("notional_usd")) or _positive(trade.get("amount_usd"))
            live[symbol] = live.get(symbol, 0.0) + (amount or 0.0)
            units[symbol] = max(units.get(symbol, 0.0), _positive(trade.get("units")) or 0.0)
    fresh = utc_now() - timedelta(minutes=MARK_MAX_AGE_MINUTES)
    total = 0.0
    for symbol in effective_open_symbols(runtime_state, state):
        readings = [*costs.get(symbol, []), live.get(symbol, 0.0)]
        if not max(readings):
            return None  # held, but no notional is known
        mark = marks.get(symbol) if isinstance(marks.get(symbol), dict) else {}
        try:
            seen = datetime.fromisoformat(str(mark.get("at")))
        except (TypeError, ValueError):
            seen = None
        rate = _positive(mark.get("rate"))
        if rate and seen is not None and seen.tzinfo is not None and seen >= fresh:
            readings.append(units.get(symbol, 0.0) * rate)
        total += max(readings)
    return round(total, 2)


def effective_open_symbols(runtime_state: Any, state: dict[str, Any]) -> list[str]:
    """Symbols the eToro caps count: reconcile's list plus mirrored trades in flight."""

    from app.broker.etoro_live_scorecard import SCORECARD_KEY

    cards = _load(runtime_state, SCORECARD_KEY)
    by_id = _ids_to_symbols(runtime_state, cards)
    symbols = {by_id.get(str(s), str(s).upper()) for s in state.get("open_symbols") or []}
    for trade in cards.values():
        if isinstance(trade, dict) and trade.get("status") in _LIVE_TRADE_STATUSES:
            symbols.add(str(trade.get("symbol") or "").upper())
    symbols.discard("")
    return sorted(symbols)


def held_symbols(runtime_state: Any) -> set[str]:
    """``effective_open_symbols`` from the mirror's three records, read in one round trip;
    raises when one is unreadable (the room check itself would read it as empty). Option B
    (2026-10-21): a paper-only entry never opens in these symbols (app/workflow/paper_only.py)."""

    from app.broker.etoro_live_mirror import STATE_KEY
    from app.broker.etoro_live_scorecard import SCORECARD_KEY
    from app.broker.etoro_live_test_order import TEST_STATE_KEY

    keys = [STATE_KEY, SCORECARD_KEY, TEST_STATE_KEY]
    getter = getattr(runtime_state, "get_many", None)
    values = (
        dict(getter(keys)) if callable(getter) else {key: runtime_state.get(key) for key in keys}
    )
    records = {key: json.loads(values.get(key) or "{}") for key in keys}
    if not all(isinstance(record, dict) for record in records.values()):
        raise ValueError("an eToro live record is not a JSON object")
    return set(effective_open_symbols(SimpleNamespace(get=values.get), records[STATE_KEY]))


def paper_only_held_reasons(mirror: Any, order: Any) -> list[str]:
    """For an order tagged paper-only (option B), the execution-time re-check of its symbol:
    refused while the real eToro account holds or has it in flight, or when that cannot be
    read. [] for any other order, and without a mirror (nothing is copied or exit-copied)."""

    from app.performance.paper_only import is_paper_only

    if mirror is None or not is_paper_only(getattr(order, "metadata", None)):
        return []
    try:
        held = held_symbols(mirror.state)
    except Exception:  # noqa: BLE001 - unknown is refused
        return ["paper_only_etoro_live_state_unreadable"]
    return ["paper_only_etoro_live_symbol_held"] if order.symbol.upper() in held else []


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
    # Review 2026-10-06: only grant eToro room when the copy can actually happen. The
    # self-simulated paper broker never calls mirror(); a rate-limit cooldown or a fresh
    # rejection makes mirror() roll back without using any room, so every entry would skip
    # paper's room checks and none would be copied.
    settings = mirror.settings
    for name in ("paper_broker", "broker_for_equities"):  # the order must reach Alpaca paper
        if str(getattr(settings, name, "alpaca") or "alpaca") != "alpaca":
            return None
    if _cooling_down(mirror) or _recent_copy_failure(mirror.state):
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


def note_copy_failed(mirror: Any, symbol: str, reason: str) -> None:
    """Called by mirror() when an order was rate-limited or rejected: for the next
    COPY_FAILED_HOLD_MINUTES paper's own room rules apply again."""

    from app.utils.time import utc_now

    with suppress(Exception):
        mirror.state.set(
            COPY_FAILED_KEY,
            json.dumps({"at": utc_now().isoformat(), "symbol": symbol, "reason": reason}),
        )


def _recent_copy_failure(runtime_state: Any) -> bool:
    from datetime import datetime, timedelta

    from app.utils.time import utc_now

    try:
        at = datetime.fromisoformat(str(_load(runtime_state, COPY_FAILED_KEY).get("at")))
    except (TypeError, ValueError):
        return False
    return utc_now() - at < timedelta(minutes=COPY_FAILED_HOLD_MINUTES)


def _cooling_down(mirror: Any) -> bool:
    from app.broker.etoro_rate_limit import etoro_cooldown_remaining

    client_settings = getattr(getattr(mirror, "client", None), "settings", None)
    try:
        return client_settings is not None and etoro_cooldown_remaining(client_settings) > 0
    except Exception:  # noqa: BLE001 - unknown means do not grant eToro room
        return True


def _ids_to_symbols(runtime_state: Any, cards: dict[str, Any]) -> dict[str, str]:
    """Raw eToro instrument ids (reconcile keeps unresolved ones) mapped back to symbols."""

    by_id: dict[str, str] = {}
    for trade in [*cards.values(), _load(runtime_state, "etoro_live:test_order")]:
        if isinstance(trade, dict) and trade.get("instrument_id") and trade.get("symbol"):
            by_id[str(trade["instrument_id"])] = str(trade["symbol"]).upper()
    return by_id


def _positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _load(runtime_state: Any, key: str) -> dict[str, Any]:
    try:
        value = json.loads(runtime_state.get(key) or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}
