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
from datetime import timedelta
from typing import Any

from app.utils.time import utc_now

STOPS_KEY = "etoro_live:intended_stops"
UNSEEN_GRACE_MINUTES = 30  # an entry with no matching position after this is dropped


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
    stops = _load(mirror)
    stops[symbol.upper()] = {
        "stop": float(stop),
        "instrument_id": instrument_id,
        "recorded_at": utc_now().isoformat(),
    }
    _save(mirror, stops)


def check_backup_stops(mirror: Any) -> list[dict[str, Any]] | None:
    """Close any mirrored position whose fresh price is at or below its intended stop."""

    from app.broker.etoro_live_mirror import client_problem

    stops = _load(mirror)
    if not stops or client_problem(mirror.client):
        return None
    client = mirror.client
    positions = (client.fetch_raw_portfolio().get("clientPortfolio", {}) or {}).get(
        "positions", []
    ) or []
    grace = (utc_now() - timedelta(minutes=UNSEEN_GRACE_MINUTES)).isoformat()
    closed: list[dict[str, Any]] = []
    remaining: dict[str, Any] = {}
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
            if str(info.get("recorded_at") or "") > grace:
                remaining[symbol] = info  # not visible yet; closed positions age out
            continue
        try:
            rate = fresh_rate(client, symbol)
        except Exception:  # noqa: BLE001 - eToro's own stop still protects it
            rate = None
        if rate is None or rate > float(info["stop"]):
            remaining[symbol] = info
            continue
        record = {"symbol": symbol, "rate": rate, "stop": info["stop"], "position_ids": []}
        try:
            for position in held:
                client.close_position_by_id(int(position["positionID"]), int(instrument_id))
                record["position_ids"].append(position["positionID"])
        except Exception as exc:  # noqa: BLE001 - stop new risk, ask for a manual close
            remaining[symbol] = info
            mirror.logs.log("etoro_live_backup_stop_close_failed", {**record, "error": str(exc)})
            mirror._halt(f"backup_stop_close_failed:{symbol}:{exc}")
            continue
        mirror.logs.log("etoro_live_backup_stop_closed", record)
        mirror._notify(
            f"eToro LIVE backup stop: closed {symbol} at about {rate:,.2f} (stop {info['stop']:,.2f})."
        )
        closed.append(record)
    _save(mirror, remaining)
    return closed


def _load(mirror: Any) -> dict[str, Any]:
    try:
        value = json.loads(mirror.state.get(STOPS_KEY) or "{}")
    except (TypeError, ValueError):
        value = {}
    return value if isinstance(value, dict) else {}


def _save(mirror: Any, stops: dict[str, Any]) -> None:
    mirror.state.set(STOPS_KEY, json.dumps(stops))
