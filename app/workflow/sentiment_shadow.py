"""Sentiment at each paper entry -- RECORD ONLY (operator-approved 2026-11-01).

For every paper entry (paper ledger positions opened since ``BACKFILL_FROM``) it records, once:

* **news**: Alpaca's news headlines about the stock from the ``NEWS_HOURS`` before the entry,
  scored with a small finance word list (positive minus negative words, over both, per article;
  the mean over articles that tag at most ``FOCUSED_MAX_SYMBOLS`` stocks, so market round-ups do
  not count). Alpaca serves past news, so every entry is scored as of its own entry time, the
  October entries included.
* **eToro crowd**: the stock's eToro feed (``GET /api/v1/feeds/markets/{instrumentId}``): the
  share of BUY among the users' ``trade_open`` items, and the posts' word-list mood, over the
  ``CROWD_HOURS`` before the entry. The feed serves only its latest items (eToro documents no
  date filter), so an entry gets a crowd reading only when recorded within
  ``CROWD_MAX_LAG_MINUTES`` of opening; older entries record why there is none. eToro's guides
  disagree on field names (``items``/``posts``, ``text``/``body``), so both are read and the
  item keys seen are recorded.

Nothing reads these records to decide anything: no proposal, order, approval, ranking or alert
changes. Each record goes to ``runtime_state[RECORDS_KEY]`` (the newest ``MAX_RECORDS``) and to
one ``sentiment_shadow_recorded`` run log. The eToro reads use the LIVE client but never open
its rate-limit cooldown (``cooldown_on_rate_limit=False``: a 429 here must not blind the live
backup stop), go through its shared request slot, and are at most ``CROWD_READS_PER_RUN`` a
run. It runs in the phase-gate maintenance step (every 30 minutes); ``sentiment_shadow_enabled``
off stops it.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from app.utils.time import utc_now

RECORDS_KEY = "sentiment:entries"
MAX_RECORDS = 400
BACKFILL_FROM = "2026-10-01"
NEWS_HOURS = 72
CROWD_HOURS = 24
CROWD_MAX_LAG_MINUTES = 60
FOCUSED_MAX_SYMBOLS = 5
NEWS_READS_PER_RUN = 8
CROWD_READS_PER_RUN = 3
FEED_TAKE = 100
LABEL_EDGE = 0.15  # mean score at or beyond +/- this reads positive / negative

POSITIVE = frozenset(
    """beat beats beating surpass surpassed surpasses exceed exceeded exceeds record strong
    stronger strongest growth grow grows growing gain gains gained rally rallies rallied surge
    surges surged soar soars soared jump jumps jumped rise rises rising rose upgrade upgraded
    upgrades outperform outperformed outperforms bullish buy optimistic optimism profit profits
    profitable boost boosts boosted raise raises raised expand expands expansion win wins won
    approval approved breakthrough partnership momentum rebound rebounds rebounded recover
    recovers recovered recovery high highs positive accelerate accelerates accelerating""".split()
)
NEGATIVE = frozenset(
    """miss misses missed missing fall falls fell falling drop drops dropped decline declines
    declined declining slump slumps slumped plunge plunges plunged tumble tumbles tumbled sink
    sinks sank slide slides slid loss losses lose loses losing weak weaker weakest downgrade
    downgraded downgrades underperform underperformed underperforms bearish sell selloff
    pessimistic cut cuts cutting lower lowers lowered warn warns warned warning lawsuit sued
    probe investigation recall recalls layoff layoffs fraud fine fined delay delays delayed
    concern concerns worry worries risk risks negative slowdown slowing halt halted crash
    crashes crashed bankruptcy default lows""".split()
)
_WORD = re.compile(r"[a-z']+")


def word_score(text: str) -> float | None:
    """(positive - negative) / (positive + negative) words, or None when neither occurs."""

    words = _WORD.findall(str(text or "").lower())
    pos = sum(1 for w in words if w in POSITIVE)
    neg = sum(1 for w in words if w in NEGATIVE)
    return None if pos + neg == 0 else (pos - neg) / (pos + neg)


def label(score: float | None) -> str:
    if score is None:
        return "no_signal"
    return "positive" if score >= LABEL_EDGE else "negative" if score <= -LABEL_EDGE else "neutral"


def _when(value: Any) -> datetime | None:
    """An ISO time; one without a zone is read as UTC."""

    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def news_reading(articles: list[dict[str, Any]], symbol: str) -> dict[str, Any]:
    """The news record from Alpaca articles (already limited to the window before the entry)."""

    focused = [a for a in articles if len(a.get("symbols") or []) <= FOCUSED_MAX_SYMBOLS]
    scores = [s for a in focused if (s := word_score(f"{a.get('headline', '')} {a.get('summary', '')}")) is not None]
    mean = sum(scores) / len(scores) if scores else None
    return {
        "articles": len(articles),
        "focused": len(focused),
        "scored": len(scores),
        "score": None if mean is None else round(mean, 3),
        "label": "no_news" if not focused else label(mean),
        "headlines": [str(a.get("headline") or "")[:140] for a in focused[:3]],
    }


# The live feed (first read 2026-11-01) nests each item's content: its top-level keys are
# additionalData, commentsData, emotionsData, id, post, requesterContext and summary. The fields
# are therefore found by name at any depth (breadth-first), and each record keeps the dotted field
# paths of the first item (``item_paths``) so the exact layout can be confirmed.
_TIME_KEYS = frozenset({"createdAt", "created_at", "created", "occurredAt", "occurred", "publishedAt", "timestamp"})
_TEXT_KEYS = frozenset({"text", "body", "content", "message"})
_SIDE_KEYS = frozenset({"direction", "isBuy", "is_buy", "side"})
_TYPE_KEYS = frozenset({"type", "postType", "feedItemType", "itemType"})


def find_field(obj: Any, names: frozenset[str], depth: int = 4) -> Any:
    """The first non-empty value under any of ``names``, breadth-first, ``depth`` levels down."""

    level = [obj]
    for _ in range(depth + 1):
        nxt: list[Any] = []
        for node in level:
            if isinstance(node, dict):
                for key, value in node.items():
                    if key in names and value not in (None, "", [], {}):
                        return value
                nxt.extend(v for v in node.values() if isinstance(v, (dict, list)))
            elif isinstance(node, list):
                nxt.extend(v for v in node[:5] if isinstance(v, (dict, list)))
        level = nxt
    return None


def item_paths(item: Any, prefix: str = "", depth: int = 3) -> list[str]:
    """Dotted field paths of a feed item (values left out), for the record's layout check."""

    if not isinstance(item, dict) or depth < 0:
        return [prefix] if prefix else []
    paths: list[str] = []
    for key, value in item.items():
        path = f"{prefix}.{key}" if prefix else str(key)
        paths.extend(item_paths(value, path, depth - 1) if isinstance(value, dict) and value else [path])
    return paths


def _item_text(item: Any) -> str:
    value = find_field(item, _TEXT_KEYS)
    if isinstance(value, dict):
        value = find_field(value, frozenset({"text", "body", "content"}), depth=2)
    return value if isinstance(value, str) else ""


def _item_side(item: Any) -> str | None:
    value = find_field(item, _SIDE_KEYS)
    if value is True or str(value).lower() in {"buy", "long"}:
        return "BUY"
    if value is False or str(value).lower() in {"sell", "short"}:
        return "SELL"
    return None


def crowd_reading(items: list[dict[str, Any]], opened_at: datetime) -> dict[str, Any]:
    """The eToro crowd record from feed items: those in the ``CROWD_HOURS`` before the entry. A
    trade open is an item with a buy/sell side whose type does not say it closed; a post is an
    item with text."""

    since = opened_at - timedelta(hours=CROWD_HOURS)
    window = [i for i in items if (t := _when(find_field(i, _TIME_KEYS))) and since <= t <= opened_at]
    sides = [(i, _item_side(i)) for i in window]
    opens = [side for i, side in sides if side and "close" not in str(find_field(i, _TYPE_KEYS) or "").lower()]
    buys, sells = opens.count("BUY"), opens.count("SELL")
    texts = [text for i in window if (text := _item_text(i))]
    scores = [s for text in texts if (s := word_score(text)) is not None]
    mean = sum(scores) / len(scores) if scores else None
    return {
        "items_seen": len(items),
        "in_window": len(window),
        "trade_opens": len(opens),
        "buys": buys,
        "sells": sells,
        "buy_pct": None if buys + sells == 0 else round(100.0 * buys / (buys + sells), 1),
        "posts": len(texts),
        "post_score": None if mean is None else round(mean, 3),
        "post_label": label(mean),
        "item_keys": sorted({k for i in items[:20] for k in i})[:30],
        "item_paths": item_paths(items[0])[:60] if items else [],
    }


def fetch_news(settings: Any, symbol: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Alpaca news about ``symbol`` published in [start, end] (newest first, at most 50)."""

    import httpx

    key, secret = str(settings.alpaca_api_key or ""), str(settings.alpaca_secret_key or "")
    if not key or not secret:
        raise RuntimeError("alpaca_keys_missing")
    root = str(getattr(settings, "alpaca_data_url", "") or "https://data.alpaca.markets").rstrip("/")
    with httpx.Client(timeout=10.0) as client:
        response = client.get(
            f"{root}/v1beta1/news",
            headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
            params={
                "symbols": symbol,
                "start": start.isoformat(),
                "end": end.isoformat(),
                "limit": 50,
                "sort": "desc",
                "include_content": "false",
            },
        )
        response.raise_for_status()
        return list((response.json() or {}).get("news") or [])


# eToro's guides disagree on the feed's path and list key, and its API reference has no feed
# page (checked 2026-11-01). The first live reads (11-01) came back 200 with no items, so the
# reader tries both documented paths, finds the item list under any key, and records the shape
# of each reply (``feed``) until the real one is known.
FEED_PATHS = ("/feeds/markets/{id}", "/feeds/instruments/{id}")
_LIST_KEYS = ("items", "posts", "data", "results", "feed", "feeds", "discussions", "activities", "entries")


def _instrument_id(client: Any, symbol: str) -> int:
    resolved = client._instrument_cache_by_symbol.get(symbol)
    if resolved is not None:
        return int(resolved["instrument_id"])
    found = client._request(
        "GET", "/market-data/search", params={"internalSymbolFull": symbol}, cooldown_on_rate_limit=False
    )
    exact = next((i for i in found.get("items", []) if str(i.get("internalSymbolFull", "")).upper() == symbol), None)
    if exact is None:
        raise RuntimeError(f"instrument_not_found:{symbol}")
    return int(exact["internalInstrumentId"])


def feed_items(payload: Any) -> list[dict[str, Any]]:
    """The item list of a feed reply, under whichever key holds it."""

    if isinstance(payload, list):
        return [i for i in payload if isinstance(i, dict)]
    if not isinstance(payload, dict):
        return []
    for key in _LIST_KEYS:
        value = payload.get(key)
        if isinstance(value, dict):
            value = next((value[k] for k in ("items", "posts", "results", "data") if isinstance(value.get(k), list)), None)
        if isinstance(value, list):
            return [i for i in value if isinstance(i, dict)]
    for value in payload.values():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            return [i for i in value if isinstance(i, dict)]
    return []


def _shape(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        lists = {str(k): len(v) for k, v in payload.items() if isinstance(v, list)}
        return {"type": "object", "keys": sorted(str(k) for k in payload)[:20], "lists": lists}
    if isinstance(payload, list):
        return {"type": "array", "length": len(payload)}
    return {"type": type(payload).__name__}


def read_feed(client: Any, symbol: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """(the stock's latest eToro feed items, how they were read). Never opens the live account's
    rate-limit cooldown; a rate limit stops the reads (raised), any other error is recorded."""

    from app.broker.etoro_rate_limit import EToroRateLimitError

    instrument_id = _instrument_id(client, symbol)
    attempts: list[dict[str, Any]] = []
    for template in FEED_PATHS:
        path = template.format(id=instrument_id)
        try:
            payload = client._request("GET", path, params={"take": FEED_TAKE}, cooldown_on_rate_limit=False)
        except EToroRateLimitError:
            raise
        except Exception as exc:  # noqa: BLE001 - e.g. a 404 on the path the guides disagree about
            attempts.append({"path": path, "error": str(exc)[:120]})
            continue
        items = feed_items(payload)
        attempts.append({"path": path, **_shape(payload)})
        if items:
            break
    else:
        items = []
    return items, {"instrument_id": instrument_id, "attempts": attempts}


def crowd_now(client: Any, symbol: str, until: datetime) -> dict[str, Any]:
    """The crowd record for ``symbol`` over the ``CROWD_HOURS`` before ``until``, with the read's
    shape when it found no items (so a changed or unexpected feed format shows)."""

    items, feed = read_feed(client, symbol)
    record = crowd_reading(items, until)
    if not items:
        record["feed"] = feed
    else:
        record["feed_path"] = feed["attempts"][-1]["path"]
    return record


def _records(runtime_state: Any) -> dict[str, Any]:
    try:
        stored = json.loads(runtime_state.get(RECORDS_KEY) or "{}")
    except (TypeError, ValueError):
        stored = {}
    return stored if isinstance(stored, dict) else {}


def refresh_sentiment_shadow(service: Any) -> dict[str, Any] | None:
    """Record the entries not yet recorded (newest first, bounded per run); None when off,
    nothing is due, or there is no paper ledger."""

    settings = service.settings
    if not bool(getattr(settings, "sentiment_shadow_enabled", True)):
        return None
    positions = getattr(getattr(service, "paper_trading", None), "positions", None)
    if positions is None:
        return None
    records = _records(service.runtime_state)
    entries = [
        p for p in positions.list(limit=500)
        if p.id not in records and str(p.opened_at) >= BACKFILL_FROM and str(p.side).lower() == "buy"
    ]
    if not entries:
        return None
    entries.sort(key=lambda p: str(p.opened_at), reverse=True)
    mirror = getattr(getattr(getattr(service, "auto_trading", None), "execution", None), "etoro_live_mirror", None)
    etoro = getattr(mirror, "client", None)
    now, done, crowd_reads = utc_now(), [], 0
    for position in entries[:NEWS_READS_PER_RUN]:
        opened = _when(position.opened_at)
        if opened is None:
            continue
        symbol = str(position.symbol).upper()
        lag = (now - opened).total_seconds() / 60.0
        wants_crowd = lag <= CROWD_MAX_LAG_MINUTES and etoro is not None
        if wants_crowd and crowd_reads >= CROWD_READS_PER_RUN:
            continue  # waits for the next run's crowd read, while still inside the window
        record: dict[str, Any] = {
            "position_id": position.id,
            "proposal_id": position.proposal_id,
            "symbol": symbol,
            "strategy": position.strategy_name,
            "timeframe": position.timeframe,
            "paper_only": bool((position.payload or {}).get("paper_only")),
            "opened_at": position.opened_at,
            "recorded_at": now.isoformat(),
        }
        try:
            articles = fetch_news(settings, symbol, opened - timedelta(hours=NEWS_HOURS), opened)
            record["news"] = news_reading(articles, symbol)
        except Exception as exc:  # noqa: BLE001 - record-only: one failed read is that record's note
            record["news"] = {"error": str(exc)[:160]}
        if lag > CROWD_MAX_LAG_MINUTES:
            record["crowd"] = {"status": "not_available", "reason": "entry_older_than_feed_window"}
        elif etoro is None:
            record["crowd"] = {"status": "not_available", "reason": "no_etoro_client"}
        else:
            crowd_reads += 1
            try:
                record["crowd"] = crowd_now(etoro, symbol, opened)
            except Exception as exc:  # noqa: BLE001 - a rate limit or format change: noted, not retried
                record["crowd"] = {"status": "error", "reason": str(exc)[:160]}
        records[position.id] = record
        done.append(record)
        service.run_logs.log("sentiment_shadow_recorded", record)
    if not done:
        return None
    newest = sorted(records.items(), key=lambda kv: str(kv[1].get("opened_at")), reverse=True)
    service.runtime_state.set(RECORDS_KEY, json.dumps(dict(newest[:MAX_RECORDS])))
    return {"recorded": len(done)}
