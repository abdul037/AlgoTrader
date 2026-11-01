"""News mood before the bot's own backtest trades -- RESEARCH ONLY (operator 2026-11-01).

Question: do the daily strategies' trades entered after negative (or positive) news about the
stock do worse? It would take weeks of live entries to know; the bot's own daily walk-forward
trades answer it now. On its first run it snapshots every trade in the latest daily walk-forward
OOS rows of the last ``LOOKBACK_DAYS`` (``alpaca:1d:{SYMBOL}:walk_forward_oos``, all daily
strategies: symbol, entry day, R = pnl / the $100 a backtest trade risks) into
``runtime_state[TRADES_KEY]``. Each run then scores the news of up to ``READS_PER_RUN``
(symbol, entry day) pairs: Alpaca headlines from the ``NEWS_HOURS`` before that day's open,
scored exactly as at live entries (app/workflow/sentiment_shadow.py), into ``SCORES_KEY``. When
every pair is scored it writes ``SUMMARY_KEY``: trades, mean R, win rate and t per news mood,
and what a "skip negative news" or "skip positive news" rule would have removed and kept, and
logs ``news_backtest_completed``. Then it stops. Its own scheduler job; nothing reads it to
decide anything; ``news_backtest_enabled`` off stops it.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from typing import Any

from app.utils.time import utc_now
from app.workflow import sentiment_shadow as shadow

TRADES_KEY = "sentiment:backtest:trades"
SCORES_KEY = "sentiment:backtest:scores"
SUMMARY_KEY = "sentiment:backtest:summary"
LOOKBACK_DAYS = 7
READS_PER_RUN = 20
RUN_SECONDS = 300
OPEN_UTC_HOUR = 13  # 09:00 New York in winter, 08:00 in summer: before every open
RISK_USD = 100.0  # what one batch-backtest trade risks (strategy_evidence.BACKTEST_RISK_USD)
_LATEST_SQL = """
    SELECT b.strategy_name, b.file_path, b.trades_json
    FROM backtests b
    JOIN (
        SELECT strategy_name, file_path, MAX(completed_at) AS completed_at
        FROM backtests
        WHERE file_path LIKE ? AND completed_at >= ?
        GROUP BY strategy_name, file_path
    ) latest
      ON b.strategy_name = latest.strategy_name
     AND b.file_path = latest.file_path
     AND b.completed_at = latest.completed_at
"""


def load_trades(db: Any, now: datetime) -> list[dict[str, Any]]:
    """[{symbol, day, strategy, r}] from the latest daily walk-forward OOS rows."""

    cutoff = (now - timedelta(days=LOOKBACK_DAYS)).isoformat()
    with db.connect() as connection:
        rows = connection.execute(_LATEST_SQL, ("alpaca:1d:%:walk_forward_oos", cutoff)).fetchall()
    trades: list[dict[str, Any]] = []
    for row in rows:
        parts = str(row["file_path"]).split(":")
        try:
            listed = json.loads(row["trades_json"] or "[]")
        except (TypeError, ValueError):
            continue
        for trade in listed if len(parts) >= 3 and isinstance(listed, list) else []:
            pnl = trade.get("pnl_usd") if isinstance(trade, dict) else None
            day = str(trade.get("entry_time") or "")[:10] if isinstance(trade, dict) else ""
            if isinstance(pnl, (int, float)) and math.isfinite(pnl) and len(day) == 10:
                trades.append({"symbol": parts[2].upper(), "day": day, "strategy": row["strategy_name"], "r": round(pnl / RISK_USD, 4)})
    return trades


def _stats(rs: list[float]) -> dict[str, Any]:
    n = len(rs)
    if not n:
        return {"trades": 0}
    mean = sum(rs) / n
    sd = math.sqrt(sum((r - mean) ** 2 for r in rs) / (n - 1)) if n > 1 else 0.0
    return {
        "trades": n,
        "mean_r": round(mean, 4),
        "win_pct": round(100.0 * sum(1 for r in rs if r > 0) / n, 1),
        "t": None if sd == 0 else round(mean / (sd / math.sqrt(n)), 2),
    }


def summarize(trades: list[dict[str, Any]], scores: dict[str, Any]) -> dict[str, Any]:
    """Per news mood, overall and per strategy, and the two candidate rules' removed vs kept."""

    moods: dict[str, list[float]] = {}
    by_strategy: dict[str, dict[str, list[float]]] = {}
    for trade in trades:
        mood = str((scores.get(f"{trade['symbol']}:{trade['day']}") or {}).get("label") or "unread")
        moods.setdefault(mood, []).append(trade["r"])
        by_strategy.setdefault(trade["strategy"], {}).setdefault(mood, []).append(trade["r"])
    rules = {}
    for name, skip in (("skip_negative_news", "negative"), ("skip_positive_news", "positive")):
        removed = moods.get(skip, [])
        kept = [r for mood, rs in moods.items() if mood not in (skip, "unread") for r in rs]
        rules[name] = {"removed": _stats(removed), "kept": _stats(kept)}
    return {
        "trades": len(trades),
        "pairs": len(scores),
        "by_mood": {mood: _stats(rs) for mood, rs in sorted(moods.items())},
        "rules": rules,
        "by_strategy": {
            name: {mood: _stats(rs) for mood, rs in sorted(groups.items())}
            for name, groups in sorted(by_strategy.items())
        },
    }


def _load(runtime_state: Any, key: str, default: Any) -> Any:
    try:
        value = json.loads(runtime_state.get(key) or "null")
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def run_news_backtest(settings: Any, runtime_state: Any, run_logs: Any, db: Any) -> dict[str, Any] | None:
    """One bounded run; None when off or finished."""

    if not bool(getattr(settings, "news_backtest_enabled", True)) or _load(runtime_state, SUMMARY_KEY, {}):
        return None
    now = utc_now()
    trades = _load(runtime_state, TRADES_KEY, [])
    if not trades:
        trades = load_trades(db, now)
        if not trades:
            return None
        runtime_state.set(TRADES_KEY, json.dumps(trades))
    scores = _load(runtime_state, SCORES_KEY, {})
    pending = sorted({f"{t['symbol']}:{t['day']}" for t in trades} - set(scores))
    for key in pending[:READS_PER_RUN]:
        symbol, day = key.split(":")
        end = datetime.fromisoformat(f"{day}T{OPEN_UTC_HOUR:02d}:00:00+00:00")
        try:
            articles = shadow.fetch_news(settings, symbol, end - timedelta(hours=shadow.NEWS_HOURS), end)
        except Exception as exc:  # noqa: BLE001 - stop this run; the pair is retried next run
            runtime_state.set(SCORES_KEY, json.dumps(scores))
            return {"scored": len(scores), "pending": len(pending), "error": str(exc)[:160]}
        reading = shadow.news_reading(articles, symbol)
        scores[key] = {k: reading[k] for k in ("label", "score", "focused", "articles")}
    runtime_state.set(SCORES_KEY, json.dumps(scores))
    if len(pending) > READS_PER_RUN:
        return {"scored": len(scores), "pending": len(pending) - READS_PER_RUN}
    summary = {**summarize(trades, scores), "completed_at": now.isoformat()}
    runtime_state.set(SUMMARY_KEY, json.dumps(summary))
    run_logs.log("news_backtest_completed", {k: summary[k] for k in ("trades", "pairs", "by_mood", "rules")})
    return {"scored": len(scores), "pending": 0, "completed": True}
