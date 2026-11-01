"""News and eToro-crowd sentiment at each paper entry: RECORD ONLY (operator 2026-11-01)."""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

import app.workflow.sentiment_shadow as shadow
from app.broker.etoro_rate_limit import EToroRateLimitError
from app.utils.time import utc_now
from tests.test_workflow_service import FakeLogs, FakeState


def _position(pid, symbol, minutes_ago, **extra):
    opened = (utc_now() - timedelta(minutes=minutes_ago)).isoformat()
    values = dict(id=pid, proposal_id=f"prop_{pid}", symbol=symbol, strategy_name="ema_trend_stack",
                  timeframe="1d", side="buy", opened_at=opened, payload={})
    values.update(extra)
    return SimpleNamespace(**values)


class _Positions:
    def __init__(self, rows):
        self.rows = rows

    def list(self, *, status=None, limit=200):
        return list(self.rows)


class _Etoro:
    def __init__(self, items=None, error=None):
        self.items, self.error, self.calls = items or [], error, []
        self._instrument_cache_by_symbol = {"WMT": {"instrument_id": 1035}}

    def _request(self, method, path, *, params=None, cooldown_on_rate_limit=True):
        self.calls.append((path, params, cooldown_on_rate_limit))
        if self.error:
            raise self.error
        if path == "/market-data/search":
            return {"items": [{"internalSymbolFull": params["internalSymbolFull"], "internalInstrumentId": 1484}]}
        return {"items": self.items}


def _service(rows, etoro=None, **settings):
    values = dict(sentiment_shadow_enabled=True, alpaca_api_key="k", alpaca_secret_key="s")
    values.update(settings)
    mirror = SimpleNamespace(client=etoro) if etoro is not None else None
    return SimpleNamespace(
        settings=SimpleNamespace(**values), runtime_state=FakeState(), run_logs=FakeLogs(),
        paper_trading=SimpleNamespace(positions=_Positions(rows)),
        auto_trading=SimpleNamespace(execution=SimpleNamespace(etoro_live_mirror=mirror)),
    )


@pytest.fixture
def news(monkeypatch):
    calls = []

    def fake(settings, symbol, start, end):
        calls.append((symbol, start, end))
        return [
            {"headline": f"{symbol} beats estimates, shares surge", "summary": "record growth", "symbols": [symbol]},
            {"headline": "Markets fall on rate worries", "summary": "", "symbols": list("ABCDEFG")},  # round-up
        ]

    monkeypatch.setattr(shadow, "fetch_news", fake)
    return calls


def test_word_score_and_labels() -> None:
    assert shadow.word_score("Shares surge after earnings beat") == 1.0
    assert shadow.word_score("Profit warning: sales fall, shares plunge") == pytest.approx(-0.5)
    assert shadow.word_score("Company holds annual meeting") is None
    assert [shadow.label(v) for v in (0.2, 0.1, -0.15, None)] == ["positive", "neutral", "negative", "no_signal"]


def test_news_is_read_as_of_each_entry_and_round_ups_do_not_count(news) -> None:
    service = _service([_position("p1", "AAPL", 60 * 24 * 20)])  # an October entry: backfilled
    assert shadow.refresh_sentiment_shadow(service) == {"recorded": 1}
    symbol, start, end = news[0]
    assert symbol == "AAPL" and end - start == timedelta(hours=shadow.NEWS_HOURS)
    record = json.loads(service.runtime_state.get(shadow.RECORDS_KEY))["p1"]
    assert end == shadow._when(record["opened_at"])  # nothing published after the entry
    assert record["news"]["articles"] == 2 and record["news"]["focused"] == 1
    assert record["news"]["label"] == "positive" and record["news"]["score"] == 1.0
    assert record["crowd"] == {"status": "not_available", "reason": "entry_older_than_feed_window"}
    assert [e for e, _ in service.run_logs.items] == ["sentiment_shadow_recorded"]


def test_each_entry_is_recorded_once(news) -> None:
    service = _service([_position("p1", "AAPL", 3000)])
    shadow.refresh_sentiment_shadow(service)
    assert shadow.refresh_sentiment_shadow(service) is None and len(news) == 1


def test_a_recent_entry_gets_the_etoro_crowd_without_the_live_cooldown(news) -> None:
    opened_minutes_ago = 20
    now = utc_now()
    items = [
        {"type": "trade_open", "direction": "BUY", "createdAt": (now - timedelta(minutes=30)).isoformat()},
        {"type": "trade_open", "direction": "BUY", "createdAt": (now - timedelta(hours=3)).isoformat()},
        {"type": "trade_open", "direction": "SELL", "createdAt": (now - timedelta(hours=5)).isoformat()},
        {"type": "trade_open", "direction": "SELL", "createdAt": (now - timedelta(minutes=5)).isoformat()},  # after
        {"type": "trade_open", "direction": "BUY", "createdAt": (now - timedelta(hours=30)).isoformat()},  # too old
        {"type": "post", "text": "WMT looks strong, expect a rally", "createdAt": (now - timedelta(hours=1)).isoformat()},
        {"type": "post", "body": "weak guidance, I sold", "createdAt": (now - timedelta(hours=2)).isoformat()},
    ]
    etoro = _Etoro(items)
    service = _service([_position("p1", "WMT", opened_minutes_ago)], etoro=etoro)
    shadow.refresh_sentiment_shadow(service)
    crowd = json.loads(service.runtime_state.get(shadow.RECORDS_KEY))["p1"]["crowd"]
    assert (crowd["buys"], crowd["sells"], crowd["buy_pct"]) == (2, 1, 66.7)
    assert crowd["posts"] == 2 and crowd["post_score"] == 0.0
    assert etoro.calls == [("/feeds/markets/1035", {"take": shadow.FEED_TAKE}, False)]


def test_an_unknown_instrument_is_looked_up_without_the_cooldown(news) -> None:
    etoro = _Etoro([])
    service = _service([_position("p1", "PYPL", 10)], etoro=etoro)
    shadow.refresh_sentiment_shadow(service)
    assert [(path, cool) for path, _, cool in etoro.calls] == [
        ("/market-data/search", False), ("/feeds/markets/1484", False), ("/feeds/instruments/1484", False)
    ]  # an empty feed: the other documented path is tried too, and both shapes recorded
    crowd = json.loads(service.runtime_state.get(shadow.RECORDS_KEY))["p1"]["crowd"]
    assert [a["path"] for a in crowd["feed"]["attempts"]] == ["/feeds/markets/1484", "/feeds/instruments/1484"]
    assert crowd["feed"]["attempts"][0]["lists"] == {"items": 0}


def test_crowd_reads_are_capped_per_run_and_the_rest_wait(news) -> None:
    rows = [_position(f"p{i}", "WMT", 5 + i) for i in range(shadow.CROWD_READS_PER_RUN + 2)]
    etoro = _Etoro([])
    service = _service(rows, etoro=etoro)
    assert shadow.refresh_sentiment_shadow(service) == {"recorded": shadow.CROWD_READS_PER_RUN}
    assert len(etoro.calls) == 2 * shadow.CROWD_READS_PER_RUN  # empty feeds: both paths each
    assert shadow.refresh_sentiment_shadow(service) == {"recorded": 2}  # the next run takes the rest


def test_failures_are_noted_in_the_record_and_never_raise(monkeypatch) -> None:
    monkeypatch.setattr(shadow, "fetch_news", lambda *a: (_ for _ in ()).throw(RuntimeError("news down")))
    etoro = _Etoro(error=EToroRateLimitError("eToro API rate-limited"))
    service = _service([_position("p1", "WMT", 10)], etoro=etoro)
    shadow.refresh_sentiment_shadow(service)
    record = json.loads(service.runtime_state.get(shadow.RECORDS_KEY))["p1"]
    assert record["news"] == {"error": "news down"}
    assert record["crowd"]["status"] == "error" and "rate-limited" in record["crowd"]["reason"]


def test_off_or_without_a_ledger_it_does_nothing(news) -> None:
    assert shadow.refresh_sentiment_shadow(_service([_position("p1", "AAPL", 10)], sentiment_shadow_enabled=False)) is None
    service = _service([])
    service.paper_trading = None
    assert shadow.refresh_sentiment_shadow(service) is None and news == []


def test_entries_before_the_backfill_date_or_short_are_skipped(news) -> None:
    old = _position("p1", "AAPL", 0, opened_at="2026-09-15T15:00:00+00:00")
    short = _position("p2", "AAPL", 10, side="sell")
    assert shadow.refresh_sentiment_shadow(_service([old, short])) is None and news == []


@pytest.mark.parametrize(
    "payload",
    [
        {"items": [{"body": "x"}, "junk"]},
        {"posts": [{"body": "x"}]},
        {"data": {"items": [{"body": "x"}]}},
        {"cursor": "c", "unexpected": [{"body": "x"}]},
        [{"body": "x"}],
    ],
)
def test_the_feed_list_is_found_under_any_key(payload) -> None:
    assert shadow.feed_items(payload) == [{"body": "x"}]


def test_a_failing_path_is_recorded_and_the_other_tried() -> None:
    class _Half(_Etoro):
        def _request(self, method, path, *, params=None, cooldown_on_rate_limit=True):
            self.calls.append(path)
            if path.startswith("/feeds/markets/"):
                raise RuntimeError("Broker request failed with status 404")
            return {"posts": [{"type": "trade_open", "direction": "BUY", "createdAt": utc_now().isoformat()}]}

    items, feed = shadow.read_feed(_Half(), "WMT")
    assert len(items) == 1 and feed["attempts"][0]["error"].endswith("404")
    assert feed["attempts"][1] == {"path": "/feeds/instruments/1035", "type": "object", "keys": ["posts"], "lists": {"posts": 1}}
