"""News mood before the bot's own backtest trades: RESEARCH ONLY (operator 2026-11-01)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.workflow.news_backtest as research
import app.workflow.sentiment_shadow as shadow
from app.main import create_app
from app.utils.time import utc_now
from tests.conftest import make_settings
from tests.test_workflow_service import FakeLogs, FakeState

SETTINGS = SimpleNamespace(news_backtest_enabled=True, alpaca_api_key="k", alpaca_secret_key="s")


def _trades():
    return [
        {"symbol": "WMT", "day": "2026-10-08", "strategy": "ema_trend_stack", "r": 1.5},
        {"symbol": "WMT", "day": "2026-10-08", "strategy": "momentum_breakout", "r": 1.0},
        {"symbol": "AMD", "day": "2026-10-07", "strategy": "ema_trend_stack", "r": -1.0},
        {"symbol": "META", "day": "2026-10-05", "strategy": "ema_trend_stack", "r": -1.0},
    ]


@pytest.fixture
def news(monkeypatch):
    calls = []
    mood = {"WMT": "Walmart profit falls, shares slump", "AMD": "AMD surges on record growth", "META": ""}

    def fake(settings, symbol, start, end):
        calls.append((symbol, start, end))
        text = mood[symbol]
        return [{"headline": text, "summary": "", "symbols": [symbol]}] if text else []

    monkeypatch.setattr(shadow, "fetch_news", fake)
    return calls


def _state(trades=None):
    state = FakeState()
    state.set(research.TRADES_KEY, json.dumps(_trades() if trades is None else trades))
    return state


def test_each_stock_day_is_scored_once_from_the_news_before_that_open(news, monkeypatch) -> None:
    monkeypatch.setattr(research, "READS_PER_RUN", 2)
    state, logs = _state(), FakeLogs()
    assert research.run_news_backtest(SETTINGS, state, logs, None) == {"scored": 2, "pending": 1}
    symbol, start, end = news[0]
    assert end == datetime.fromisoformat("2026-10-07T13:00:00+00:00") and symbol == "AMD"
    assert end - start == timedelta(hours=shadow.NEWS_HOURS)
    result = research.run_news_backtest(SETTINGS, state, logs, None)
    assert result == {"scored": 3, "pending": 0, "completed": True} and len(news) == 3  # WMT once
    assert research.run_news_backtest(SETTINGS, state, logs, None) is None  # finished: stops
    assert [e for e, _ in logs.items] == ["news_backtest_completed"]


def test_the_summary_compares_moods_and_the_two_rules(news) -> None:
    state = _state()
    research.run_news_backtest(SETTINGS, state, FakeLogs(), None)
    summary = json.loads(state.get(research.SUMMARY_KEY))
    assert summary["trades"] == 4 and summary["pairs"] == 3
    assert summary["by_mood"]["negative"] == {"trades": 2, "mean_r": 1.25, "win_pct": 100.0, "t": 5.0}
    assert summary["by_mood"]["positive"]["trades"] == 1 and summary["by_mood"]["no_news"]["trades"] == 1
    skip_negative = summary["rules"]["skip_negative_news"]
    assert skip_negative["removed"]["mean_r"] == 1.25 and skip_negative["kept"]["mean_r"] == -1.0
    assert summary["by_strategy"]["ema_trend_stack"]["positive"]["trades"] == 1


def test_a_failed_read_stops_the_run_and_is_retried(monkeypatch) -> None:
    monkeypatch.setattr(shadow, "fetch_news", lambda *a: (_ for _ in ()).throw(RuntimeError("429")))
    state = _state()
    assert research.run_news_backtest(SETTINGS, state, FakeLogs(), None)["error"] == "429"
    assert json.loads(state.get(research.SCORES_KEY)) == {} and state.get(research.SUMMARY_KEY) is None


def test_off_it_does_nothing(news) -> None:
    off = SimpleNamespace(news_backtest_enabled=False)
    assert research.run_news_backtest(off, _state(), FakeLogs(), None) is None and news == []


def test_trades_come_from_the_latest_daily_walk_forward_rows(tmp_path: Path) -> None:
    app = create_app(settings=make_settings(tmp_path), enable_background_jobs=False)
    now = utc_now()
    rows = [
        ("ema_trend_stack", "alpaca:1d:WMT:walk_forward_oos", (now - timedelta(days=1)).isoformat(),
         [{"entry_time": "2026-10-08T04:00:00+00:00", "pnl_usd": 150.0}, {"entry_time": "2026-10-20T04:00:00+00:00", "pnl_usd": float("nan")}]),
        ("ema_trend_stack", "alpaca:1d:WMT:walk_forward_oos", (now - timedelta(days=2)).isoformat(),
         [{"entry_time": "2026-09-01T04:00:00+00:00", "pnl_usd": -100.0}]),  # an older row: not used
        ("ema_trend_stack", "alpaca:15m:WMT:walk_forward_oos", now.isoformat(),
         [{"entry_time": "2026-10-09T14:00:00+00:00", "pnl_usd": 50.0}]),  # not daily
    ]
    with app.state.db.connect() as connection:
        info = connection.execute("PRAGMA table_info(backtests)").fetchall()
        for i, (strategy, path, completed, trades) in enumerate(rows):
            # Every NOT NULL column without a default gets a filler of its type; ours override it.
            values = {
                col[1]: (0 if any(t in str(col[2]).upper() for t in ("INT", "REAL", "NUM")) else completed)
                for col in info
                if col[3] and col[4] is None
            }
            values.update({"id": f"b{i}", "strategy_name": strategy, "file_path": path, "completed_at": completed,
                           "trades_json": json.dumps(trades), "metrics_json": "{}", "symbol": "WMT"})
            values = {k: v for k, v in values.items() if k in {col[1] for col in info}}
            connection.execute(
                f"INSERT INTO backtests ({', '.join(values)}) VALUES ({', '.join('?' for _ in values)})",
                tuple(values.values()),
            )
    assert research.load_trades(app.state.db, now) == [
        {"symbol": "WMT", "day": "2026-10-08", "strategy": "ema_trend_stack", "r": 1.5}
    ]


def test_the_job_is_registered_and_can_be_switched_off(tmp_path: Path) -> None:
    def names(**values):
        app = create_app(settings=make_settings(tmp_path, **values), enable_background_jobs=False)
        return [job.name for job in app.state.build_scheduler_worker().jobs]

    assert "news_backtest" in names()
    assert "news_backtest" not in names(news_backtest_enabled=False)
