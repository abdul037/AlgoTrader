"""Backup stop for eToro LIVE positions (operator decision 2026-10-04).

eToro filled the ETH test with the requested target but set its own stop-loss at
exactly -10% of the fill ($2,428.34) instead of the strategy's $2,581.80 (-4.31%).
The operator chose a backup stop: on every maintenance tick the bot reads a fresh
eToro price and closes a live position itself once the price is at or below the
stop the strategy asked for. eToro's own stop stays as the outer safety net.
Ticks are a few minutes apart, so a fast move can fill below the level.

Mirrored entries record their intended stop under ``STOPS_KEY``; the one-off
crypto test keeps its stop in its own state and calls ``fresh_rate`` directly.
"""

from __future__ import annotations

import json
import time
from contextlib import nullcontext
from datetime import timedelta
from typing import Any

from app.broker.etoro_rate_limit import EToroRateLimitError
from app.utils.time import utc_now

STOPS_KEY = "etoro_live:intended_stops"
# eToro's own price per open live position, saved each tick for the scorecard page's live P/L
# (operator 2026-10-07: "it should be based on eToro"). Display only; nothing trades on it.
MARKS_KEY = "etoro_live:marks"
# The live account's positions exactly as eToro reports them (operator 2026-10-07: "show the
# actual eToro trade and P/L"). Display only; nothing trades on it.
POSITIONS_KEY = "etoro_live:positions"
_POSITION_FIELDS = (
    "positionID",
    "instrumentID",
    "isBuy",
    "leverage",
    "units",
    "amount",
    "openRate",
    "openDateTime",
    "stopLossRate",
    "takeProfitRate",
    "totalFees",
    "unrealizedPnL",
    "netProfit",
    "pnL",
    "pnl",
)
UNSEEN_GRACE_MINUTES = 24 * 60  # never-seen entry (e.g. an order queued before the open) ages out
CLOSE_RECHECK_SECONDS = 3.0


def fresh_rate(client: Any, symbol: str) -> float | None:
    """Current eToro rate for ``symbol`` from a fresh search (the client caches its own)."""

    normalized = symbol.upper().strip()
    payload = client._request(
        "GET", "/market-data/search", params={"internalSymbolFull": normalized}
    )
    items = (payload or {}).get("items", []) or []
    exact = next(
        (i for i in items if str(i.get("internalSymbolFull", "")).upper() == normalized), None
    )
    rate = float((exact or {}).get("currentRate") or 0.0)
    return rate if rate > 0 else None


def remember_stop(mirror: Any, symbol: str, stop: float | None, instrument_id: int | None) -> None:
    """Record the strategy's stop for a freshly mirrored entry."""

    if stop is None:
        return
    with _lock(mirror):
        _remember(mirror, symbol, stop, instrument_id)


def forget_stop(mirror: Any, symbol: str) -> None:
    """Drop a recorded stop (its order was refused, so no position exists)."""

    with _lock(mirror):
        stops = _load(mirror)
        if stops.pop(symbol.upper(), None) is not None:
            _save(mirror, stops)


def check_backup_stops(mirror: Any) -> list[dict[str, Any]] | None:
    """Close any mirrored position whose fresh price is at or below its intended stop."""

    with _lock(mirror):
        return _check(mirror)


def _remember(mirror: Any, symbol: str, stop: float, instrument_id: int | None) -> None:
    stops = _load(mirror)
    stops[symbol.upper()] = {
        "stop": float(stop),
        "instrument_id": instrument_id,
        "recorded_at": utc_now().isoformat(),
    }
    _save(mirror, stops)


def portfolio_positions(raw: Any) -> list[dict[str, Any]] | None:
    """Positions from a raw eToro portfolio read, or None when the read is not a
    well-formed portfolio (empty body, no ``clientPortfolio``, no ``credit``, positions
    not a list). Callers must change nothing on None (review 2026-10-05)."""

    portfolio = raw.get("clientPortfolio") if isinstance(raw, dict) else None
    if not isinstance(portfolio, dict) or portfolio.get("credit") is None:
        return None
    positions = portfolio.get("positions")
    return positions if isinstance(positions, list) else None


def _check(mirror: Any) -> list[dict[str, Any]] | None:
    from app.broker.etoro_live_mirror import client_problem

    stops = _load(mirror)
    if not stops or client_problem(mirror.client):
        return None
    client = mirror.client
    raw = client.fetch_raw_portfolio()
    positions = portfolio_positions(raw)
    if positions is None:
        return None  # malformed/empty read: keep every stop, try again next tick
    _save_positions(mirror, raw, positions, stops)
    grace = (utc_now() - timedelta(minutes=UNSEEN_GRACE_MINUTES)).isoformat()
    closed: list[dict[str, Any]] = []
    remaining: dict[str, Any] = {}
    marks: dict[str, Any] = {}
    for symbol, info in stops.items():
        instrument_id = info.get("instrument_id")
        if not instrument_id:
            try:
                instrument_id = int(client._search_instrument(symbol)["instrument_id"])
                info["instrument_id"] = instrument_id
            except Exception:  # noqa: BLE001 - try again next tick
                remaining[symbol] = info
                continue
        held = [p for p in positions if int(p.get("instrumentID") or 0) == int(instrument_id)]
        if not held:
            # Seen before and now gone -> closed at eToro: drop. Never seen -> it may still be
            # a pending order (queued before the open): keep it until it ages out.
            if not info.get("seen") and str(info.get("recorded_at") or "") > grace:
                remaining[symbol] = info
            continue
        info["seen"] = True
        try:
            rate = fresh_rate(client, symbol)
        except Exception:  # noqa: BLE001 - eToro's own stop still protects it
            rate = None
        if rate is not None:
            marks[symbol] = {"rate": rate, "at": utc_now().isoformat()}
        if rate is None or rate > float(info["stop"]):
            remaining[symbol] = info
            continue
        record = {"symbol": symbol, "rate": rate, "stop": info["stop"], "position_ids": []}
        try:
            for position in held:
                client.close_position_by_id(int(position["positionID"]), int(instrument_id))
                record["position_ids"].append(position["positionID"])
        except Exception as exc:  # noqa: BLE001 - stop new risk, ask for a manual close
            if isinstance(exc, EToroRateLimitError):  # nothing was sent; retry next tick
                remaining[symbol] = info
                mirror.logs.log(
                    "etoro_live_backup_stop_rate_limited", {**record, "error": str(exc)}
                )
                continue
            if not _still_open(client, int(instrument_id)):  # e.g. the old container closed it
                record["closed_elsewhere"] = True
            else:
                remaining[symbol] = info
                mirror.logs.log(
                    "etoro_live_backup_stop_close_failed", {**record, "error": str(exc)}
                )
                mirror._halt(f"backup_stop_close_failed:{symbol}:{exc}")
                continue
        mirror.logs.log("etoro_live_backup_stop_closed", record)
        mirror._notify(
            f"eToro LIVE backup stop: closed {symbol} at about {rate:,.2f} (stop {info['stop']:,.2f})."
        )
        closed.append(record)
    # Merge, don't overwrite: another container (deploy overlap) or the execution thread may
    # have recorded a stop since the snapshot. Drop only what this pass closed or aged out.
    current = _load(mirror)
    for symbol in set(stops) - set(remaining):
        current.pop(symbol, None)
    for symbol, info in remaining.items():
        if symbol in current:
            for field in ("instrument_id", "seen"):
                if info.get(field):
                    current[symbol][field] = info[field]
    _save(mirror, current)
    _save_marks(mirror, marks, held_symbols=set(remaining))
    return closed


def _save_positions(
    mirror: Any, raw: dict[str, Any], positions: list[Any], stops: dict[str, Any]
) -> None:
    """Snapshot eToro's own view of the live positions; never raises into the guard."""

    try:
        by_id = {
            str(info.get("instrument_id")): symbol
            for symbol, info in stops.items()
            if info.get("instrument_id")
        }
        rows = []
        for position in positions:
            if not isinstance(position, dict):
                continue
            row = {k: position[k] for k in _POSITION_FIELDS if k in position}
            row["symbol"] = by_id.get(str(position.get("instrumentID")), "")
            rows.append(row)
        portfolio = raw.get("clientPortfolio") or {}
        snapshot = {
            "at": utc_now().isoformat(),
            "credit": portfolio.get("credit"),
            "positions": rows,
        }
        mirror.state.set(POSITIONS_KEY, json.dumps(snapshot))
    except Exception:  # noqa: BLE001 - display data only
        return


def _save_marks(mirror: Any, marks: dict[str, Any], *, held_symbols: set[str]) -> None:
    """Keep the latest eToro price per symbol still held; never raises into the guard."""

    try:
        current = json.loads(mirror.state.get(MARKS_KEY) or "{}")
        current = current if isinstance(current, dict) else {}
        current = {k: v for k, v in current.items() if k in held_symbols or k in marks}
        current.update(marks)
        mirror.state.set(MARKS_KEY, json.dumps(current))
    except Exception:  # noqa: BLE001 - display data only
        return


def _still_open(client: Any, instrument_id: int) -> bool:
    """After a failed close: is a position in ``instrument_id`` still open?

    Fails closed: an error or a malformed/partial portfolio read counts as "still open",
    so the stop is kept and the operator is alerted. If it still looks open, re-read once
    after ``CLOSE_RECHECK_SECONDS`` -- during a deploy overlap the other container's close
    may still be pending.
    """

    for attempt in range(2):
        if attempt:
            time.sleep(CLOSE_RECHECK_SECONDS)
        try:
            raw = client.fetch_raw_portfolio()
            portfolio = raw.get("clientPortfolio") if isinstance(raw, dict) else None
            positions = portfolio.get("positions") if isinstance(portfolio, dict) else None
            if not isinstance(positions, list):
                continue  # unknown: treat as open unless the re-read is clean
            if not any(int(p.get("instrumentID") or 0) == instrument_id for p in positions):
                return False
        except Exception:  # noqa: BLE001 - unknown: assume it is still open
            continue
    return True


def _lock(mirror: Any) -> Any:
    return getattr(mirror, "lock", None) or nullcontext()


def _load(mirror: Any) -> dict[str, Any]:
    try:
        value = json.loads(mirror.state.get(STOPS_KEY) or "{}")
    except (TypeError, ValueError):
        value = {}
    return value if isinstance(value, dict) else {}


def _save(mirror: Any, stops: dict[str, Any]) -> None:
    mirror.state.set(STOPS_KEY, json.dumps(stops))
