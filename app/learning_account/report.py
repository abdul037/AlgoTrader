"""The ``learning_account`` section of ``GET /performance/live-scorecard`` (one ``get_many``).

Its status follows the CURRENT settings: a route the settings refuse (``disabled:setting_off``,
``not_configured`` after the keys are removed, ...) is shown as such, never an older record;
``pending_verification`` while configured but not checked by this deploy's learning thread;
``running`` while the thread holds a fresh lease (its heartbeat), ``stalled`` after
``STALLED_SECONDS`` without one; else what the account answered (``account_mismatch``, ...).
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from app.learning_account import route as routes
from app.learning_account import store as records
from app.utils.time import utc_now

STALLED_SECONDS = 420.0  # 7 min without a heartbeat
LAST_CLOSED = 20


def _json(text: Any, default: Any) -> Any:
    try:
        value = json.loads(text) if text else default
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def _time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value)) if value else None
    except (TypeError, ValueError):
        return None
    return parsed if parsed is not None and parsed.tzinfo is not None else None


def status(
    settings: Any, record: dict[str, Any], lease: dict[str, Any], *, now: datetime, attach_error: Any = None, owner: str = ""
) -> str:
    """The route to show now (see the module docstring)."""

    if attach_error:
        return routes.ATTACH_FAILED
    route = routes.settings_route(settings)
    if route != routes.PENDING:
        return route
    if not record.get("route"):
        return routes.PENDING
    # In webhook mode this process runs the thread: a record another process wrote is from an
    # earlier deploy, whose settings may not be these. In polling mode the bot process writes it.
    if not bool(getattr(settings, "telegram_polling_enabled", False)) and record.get("owner") != owner:
        return routes.PENDING
    if record["route"] == routes.RUNNING:
        heartbeat = _time(lease.get("heartbeat_at"))
        fresh = (
            lease.get("owner") == record.get("owner")
            and not lease.get("released")
            and heartbeat is not None
            and (now - heartbeat).total_seconds() <= STALLED_SECONDS
        )
        return routes.RUNNING if fresh else "stalled"
    return str(record["route"])


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _by(closed: list[dict[str, Any]], key: Any) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[float]] = {}
    counts: dict[str, int] = {}
    for item in closed:
        name = key(item)
        counts[name] = counts.get(name, 0) + 1
        if isinstance(item.get("r"), (int, float)):
            groups.setdefault(name, []).append(float(item["r"]))
    return {name: {"closed": counts[name], "mean_r": _mean(groups.get(name, []))} for name in sorted(counts)}


def learning_account_report(state: Any, settings: Any, *, app_state: Any = None, now: datetime | None = None) -> dict[str, Any]:
    from app.learning_account.account import (
        LIVE,
        MAX_ENTRIES_PER_DAY,
        MAX_OPEN,
        MAX_ORDERS_PER_DAY,
        RISK_FRACTION,
        UNREPORTED,
        day_counts,
        ny_day,
    )

    now = now or utc_now()
    values = state.get_many([records.BOOK_KEY, records.CLOSED_KEY, records.STATUS_KEY, records.LEASE_KEY])
    book = _json(values.get(records.BOOK_KEY), {}).get("trades")
    trades = book if isinstance(book, dict) else {}
    closed = [item for item in _json(values.get(records.CLOSED_KEY), []) if isinstance(item, dict)]
    record = _json(values.get(records.STATUS_KEY), {})
    lease = _json(values.get(records.LEASE_KEY), {})
    account = getattr(app_state, "learning_account", None)
    shown = status(
        settings,
        record,
        lease,
        now=now,
        attach_error=getattr(app_state, "learning_account_attach_error", None),
        owner=str(getattr(account, "owner", "") or records.PROCESS_OWNER),
    )
    counts = day_counts(trades, ny_day(now))
    open_trades = [t for t in trades.values() if t.get("status") in LIVE]
    unreported = [t for t in trades.values() if t.get("status") == UNREPORTED]
    rs = [float(item["r"]) for item in closed if isinstance(item.get("r"), (int, float))]
    return {
        "route": shown,
        "status": shown,
        "paper_only": True,
        "copied_to_etoro": False,
        "caps": {
            "entries_per_day": MAX_ENTRIES_PER_DAY,
            "orders_per_day": MAX_ORDERS_PER_DAY,
            "max_open": MAX_OPEN,
            "risk_fraction": RISK_FRACTION,
        },
        "today": {"entries": counts["entries"], "orders": counts["orders"]},
        "totals": {
            "closed": len(closed),
            "wins": sum(1 for r in rs if r > 0),
            "losses": sum(1 for r in rs if r <= 0),
            "total_r": round(sum(rs), 4),
            "mean_r": _mean(rs),
        },
        "open": len(open_trades),
        "closed": len(closed),
        "closed_unreported": len(unreported),
        "mean_r_by_key": _by(closed, lambda item: f"{item.get('strategy_name')}:{item.get('timeframe')}"),
        "mean_r_by_kind": _by(closed, lambda item: str(item.get("kind") or "unknown")),
        "open_trades": sorted(
            (
                {key: t.get(key) for key in ("trade_id", "symbol", "strategy_name", "timeframe", "kind", "status", "qty", "entry_fill", "stop_loss", "take_profit", "created_at", "flatten_reason")}
                for t in open_trades
            ),
            key=lambda item: str(item.get("created_at") or ""),
        ),
        "last_closed": closed[-LAST_CLOSED:][::-1],
        "heartbeat_at": lease.get("heartbeat_at"),
    }
