"""Sentiment now for the stocks the bot holds -- DISPLAY ONLY (operator 2026-11-01).

For every stock open on the paper account or the AlgoBot eToro account, it keeps the latest
news mood (Alpaca headlines from the last ``NEWS_HOURS``) and eToro crowd (the stock's eToro feed
over the last ``CROWD_HOURS``: BUY share of users' trade opens, posts' word mood), scored as in
app/workflow/sentiment_shadow.py, in ``runtime_state[OPEN_KEY]`` for the live scorecard. Each
part refreshes when it is ``REFRESH_MINUTES`` old; a run reads at most ``CROWD_READS_PER_RUN``
eToro feeds (oldest first), never opening the live account's rate-limit cooldown. Stocks no
longer held are dropped. Nothing reads it to decide anything. It runs in the phase-gate
maintenance step and stops with ``sentiment_shadow_enabled``.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any

from app.utils.time import utc_now
from app.workflow import sentiment_shadow as shadow

OPEN_KEY = "sentiment:open"
REFRESH_MINUTES = 120
CROWD_READS_PER_RUN = 3
_TICKER = re.compile(r"^[A-Z][A-Z.]{0,9}$")  # eToro's open_symbols may hold numeric instrument ids


def held_symbols(service: Any) -> dict[str, list[str]]:
    """{symbol: ["paper", "eToro"]} for every long stock open on either account."""

    held: dict[str, list[str]] = {}
    positions = getattr(getattr(service, "paper_trading", None), "positions", None)
    for position in positions.list(status="open", limit=500) if positions is not None else []:
        symbol = str(position.symbol or "").upper()
        if str(position.side).lower() == "buy" and _TICKER.match(symbol):
            held.setdefault(symbol, []).append("paper")
    try:
        state = json.loads(service.runtime_state.get("etoro_live:state") or "{}")
    except (TypeError, ValueError):
        state = {}
    symbols = state.get("open_symbols") if isinstance(state, dict) else None
    for raw in symbols if isinstance(symbols, list) else []:
        symbol = str(raw or "").upper()
        if _TICKER.match(symbol) and "eToro" not in held.get(symbol, []):
            held.setdefault(symbol, []).append("eToro")
    return held


def _stale(part: Any, now: datetime) -> bool:
    at = shadow._when(part.get("at")) if isinstance(part, dict) else None
    return at is None or now - at >= timedelta(minutes=REFRESH_MINUTES)


def refresh_open_sentiment(service: Any) -> dict[str, Any] | None:
    """Refresh what is due for the held stocks; None when off or nothing changed."""

    settings = service.settings
    if not bool(getattr(settings, "sentiment_shadow_enabled", True)):
        return None
    held = held_symbols(service)
    try:
        stored = json.loads(service.runtime_state.get(OPEN_KEY) or "{}")
    except (TypeError, ValueError):
        stored = {}
    stored = stored if isinstance(stored, dict) else {}
    kept = {s: v for s, v in stored.items() if s in held and isinstance(v, dict)}
    mirror = getattr(getattr(getattr(service, "auto_trading", None), "execution", None), "etoro_live_mirror", None)
    etoro = getattr(mirror, "client", None)
    now = utc_now()
    news_due = [s for s in sorted(held) if _stale(kept.get(s, {}).get("news"), now)]
    crowd_due = sorted(
        (s for s in held if _stale(kept.get(s, {}).get("crowd"), now)),
        key=lambda s: str((kept.get(s, {}).get("crowd") or {}).get("at") or ""),
    )[:CROWD_READS_PER_RUN] if etoro is not None else []
    for symbol in held:
        kept.setdefault(symbol, {})["held"] = held[symbol]
    for symbol in news_due:
        try:
            part = shadow.news_reading(shadow.fetch_news(settings, symbol, now - timedelta(hours=shadow.NEWS_HOURS), now), symbol)
        except Exception as exc:  # noqa: BLE001 - display only: the error is shown in its place
            part = {"error": str(exc)[:160]}
        kept[symbol]["news"] = {**part, "at": now.isoformat()}
    for symbol in crowd_due:
        try:
            part = shadow.crowd_now(etoro, symbol, now)
        except Exception as exc:  # noqa: BLE001 - a rate limit or a format change: shown, retried later
            part = {"status": "error", "reason": str(exc)[:160]}
        kept[symbol]["crowd"] = {**part, "at": now.isoformat()}
    if not news_due and not crowd_due and kept.keys() == stored.keys():
        return None
    service.runtime_state.set(OPEN_KEY, json.dumps(kept))
    return {"symbols": sorted(kept), "news": news_due, "crowd": crowd_due}
