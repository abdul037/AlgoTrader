"""Sentiment now for the stocks the bot holds: DISPLAY ONLY (operator 2026-11-01)."""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

import app.workflow.sentiment_now as now_sentiment
import app.workflow.sentiment_shadow as shadow
from app.utils.time import utc_now
from tests.test_sentiment_shadow import _Etoro, _Positions, _position, _service


@pytest.fixture
def news(monkeypatch):
    calls = []

    def fake(settings, symbol, start, end):
        calls.append(symbol)
        return [{"headline": f"{symbol} shares surge on record growth", "summary": "", "symbols": [symbol]}]

    monkeypatch.setattr(shadow, "fetch_news", fake)
    return calls


def _with_etoro_open(service, symbols):
    service.runtime_state.set("etoro_live:state", json.dumps({"open_symbols": symbols}))
    return service


def test_held_stocks_come_from_both_accounts() -> None:
    rows = [_position("p1", "WMT", 10), _position("p2", "AAPL", 10), _position("p3", "TSLA", 10, side="sell")]
    service = _with_etoro_open(_service(rows), ["WMT", "DIS", "100001", None])
    assert now_sentiment.held_symbols(service) == {"WMT": ["paper", "eToro"], "AAPL": ["paper"], "DIS": ["eToro"]}


def test_news_for_every_held_stock_and_the_crowd_for_three_a_run(news) -> None:
    rows = [_position(f"p{i}", s, 10) for i, s in enumerate(["AAPL", "ADBE", "COST", "CRM", "WMT"])]
    etoro = _Etoro([])
    etoro._instrument_cache_by_symbol = {s: {"instrument_id": i} for i, s in enumerate(["AAPL", "ADBE", "COST", "CRM", "WMT"])}
    service = _service(rows, etoro=etoro)
    result = now_sentiment.refresh_open_sentiment(service)
    assert sorted(news) == ["AAPL", "ADBE", "COST", "CRM", "WMT"]
    assert len(result["crowd"]) == now_sentiment.CROWD_READS_PER_RUN == len(etoro.calls)
    assert all(cooldown is False for _, _, cooldown in etoro.calls)
    stored = json.loads(service.runtime_state.get(now_sentiment.OPEN_KEY))
    assert stored["WMT"]["news"]["label"] == "positive" and stored["WMT"]["held"] == ["paper"]
    second = now_sentiment.refresh_open_sentiment(service)  # news is fresh; the other two crowds now
    assert second["news"] == [] and sorted(second["crowd"]) == sorted(set(stored) - set(result["crowd"]))


def test_parts_refresh_every_two_hours_and_sold_stocks_drop_out(news) -> None:
    rows = [_position("p1", "WMT", 10), _position("p2", "AAPL", 10)]
    service = _service(rows)
    now_sentiment.refresh_open_sentiment(service)
    assert now_sentiment.refresh_open_sentiment(service) is None  # nothing due, nothing changed
    stored = json.loads(service.runtime_state.get(now_sentiment.OPEN_KEY))
    stored["WMT"]["news"]["at"] = (utc_now() - timedelta(minutes=121)).isoformat()
    service.runtime_state.set(now_sentiment.OPEN_KEY, json.dumps(stored))
    service.paper_trading.positions = _Positions([rows[0]])  # AAPL sold
    result = now_sentiment.refresh_open_sentiment(service)
    assert result["news"] == ["WMT"] and result["symbols"] == ["WMT"]


def test_failures_are_shown_in_place_and_never_raise(monkeypatch) -> None:
    monkeypatch.setattr(shadow, "fetch_news", lambda *a: (_ for _ in ()).throw(RuntimeError("news down")))
    service = _service([_position("p1", "WMT", 10)], etoro=_Etoro(error=RuntimeError("eToro API rate-limited")))
    now_sentiment.refresh_open_sentiment(service)
    record = json.loads(service.runtime_state.get(now_sentiment.OPEN_KEY))["WMT"]
    assert record["news"]["error"] == "news down" and record["crowd"]["status"] == "error"


def test_off_it_does_nothing(news) -> None:
    service = _service([_position("p1", "WMT", 10)], sentiment_shadow_enabled=False)
    assert now_sentiment.refresh_open_sentiment(service) is None and news == []
