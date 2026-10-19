"""Event-risk (earnings) check, SHADOW ONLY (operator-approved 2026-10-18).

It logs which entries an earnings rule would have blocked and never blocks anything. The
earnings dates come from an off-hours, once-a-day refresh (Yahoo by default; a fake here: no
test touches the network) and the proposal path reads only the in-memory copy.
"""

from __future__ import annotations

import inspect
import itertools
import json
import threading
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

import app.workflow.earnings_calendar as cal
import app.workflow.event_risk as risk
from tests import test_premarket_deep_scan as scan_tests
from tests import test_shadow_ledger as ledger_tests
from tests import test_short_shadow as short_tests
from tests.conftest import make_settings
from tests.test_workflow_service import FakeLogs, FakeState

MONDAY_0100_NY = datetime(2026, 10, 19, 5, 0, tzinfo=UTC)  # 01:00 EDT: off-hours
MONDAY_1000_NY = datetime(2026, 10, 19, 14, 0, tzinfo=UTC)  # in the session
MONDAY_0835_NY = datetime(2026, 10, 19, 12, 35, tzinfo=UTC)  # the pre-market scan
MONDAY_1300_NY = datetime(2026, 10, 19, 17, 0, tzinfo=UTC)  # in the session; past 16:00 UTC
MONDAY_2100_NY = datetime(2026, 10, 20, 1, 0, tzinfo=UTC)  # off-hours; Tuesday in UTC
SUNDAY_1100_NY = datetime(2026, 10, 18, 15, 0, tzinfo=UTC)  # a weekend day: off-hours


@pytest.fixture(autouse=True)
def _fresh_memory():
    cal.reset_memory()
    yield
    cal.reset_memory()


class _CountingState(FakeState):
    def __init__(self) -> None:
        super().__init__()
        self.reads: list[str] = []

    def get(self, key: str):
        self.reads.append(key)
        return super().get(key)


class _Provider:
    """Fake earnings source: ``dates`` per symbol, ``fail`` raises, ``block`` stalls."""

    def __init__(self, dates=None, *, fail=(), block=()):
        self.dates, self.fail, self.block = dict(dates or {}), set(fail), set(block)
        self.calls: list[str] = []
        self.release = threading.Event()

    def __call__(self, symbol):
        self.calls.append(symbol)
        if symbol in self.fail:
            raise RuntimeError("Too Many Requests")
        if symbol in self.block:
            self.release.wait(5)
        return self.dates.get(symbol, [])


def _service(tmp_path, monkeypatch, *, now=MONDAY_0100_NY, symbols=("AAA", "BBB", "CCC"), **kw):
    monkeypatch.setattr(cal, "utc_now", lambda: now)
    monkeypatch.setattr(risk, "utc_now", lambda: now)
    values = {"event_risk_shadow_enabled": True, "market_universe_symbols": list(symbols)}
    values.update(market_universe_limit=200)
    values.update(kw)
    return SimpleNamespace(
        settings=make_settings(tmp_path, **values),
        runtime_state=_CountingState(),
        run_logs=FakeLogs(),
    )


def _stored(service) -> dict:
    return json.loads(service.runtime_state.get(cal.CACHE_KEY))


# --------------------------------------------------------------------------- the refresh


def test_each_equity_is_fetched_once_a_day_and_funds_or_crypto_never(tmp_path, monkeypatch) -> None:
    provider = _Provider({"AAA": ["2026-10-28"], "BBB": [date(2026, 11, 3)]})
    service = _service(tmp_path, monkeypatch, symbols=("AAA", "SPY", "BBB", "CCC", "BTC/USD"))
    summary = cal.refresh_earnings_cache(service, provider=provider)
    assert provider.calls == ["AAA", "BBB", "CCC"]  # never SPY (a fund) or BTC/USD
    stored = _stored(service)
    assert stored["symbols"]["AAA"] == {
        "status": "ok",
        "upcoming": ["2026-10-28"],
        "last": None,
        "fetched_on": "2026-10-19",
        "checked_on": "2026-10-19",
    }
    assert stored["symbols"]["BBB"]["upcoming"] == ["2026-11-03"]
    assert stored["symbols"]["CCC"]["status"] == "none"  # Yahoo lists nothing
    assert {stored["symbols"][s]["status"] for s in ("SPY", "BTC/USD")} == {"not_applicable"}
    assert stored["done_on"] == "2026-10-19" and summary["done_for_today"]
    assert summary["fetched"] == 3 and summary["not_applicable"] == 2 and summary["shadow_only"]
    assert cal.cached_snapshot() == stored  # the memory copy is the stored one
    assert [e for e, _ in service.run_logs.items] == ["event_risk_earnings_refreshed"]
    reads = list(service.runtime_state.reads)
    assert cal.refresh_earnings_cache(service, provider=provider) is None  # once a day
    assert service.runtime_state.reads == reads  # the memory says done: no database call
    assert provider.calls == ["AAA", "BBB", "CCC"] and len(service.run_logs.items) == 1
    monkeypatch.setattr(cal, "utc_now", lambda: MONDAY_0100_NY + timedelta(days=1))
    assert cal.refresh_earnings_cache(service, provider=provider)["done_for_today"]
    assert provider.calls == ["AAA", "BBB", "CCC"] * 2  # the next New York day: once more


def test_it_runs_off_hours_only_and_only_with_the_switch(tmp_path, monkeypatch) -> None:
    provider = _Provider({"AAA": ["2026-10-28"]})
    for now, enabled in ((MONDAY_1000_NY, True), (MONDAY_0835_NY, True), (MONDAY_0100_NY, False)):
        cal.reset_memory()  # MONDAY_0835_NY: the pre-market scan's own time
        service = _service(tmp_path, monkeypatch, now=now, event_risk_shadow_enabled=enabled)
        assert cal.refresh_earnings_cache(service, provider=provider) is None
        assert service.runtime_state.reads == [] and service.run_logs.items == []
        cal.warm_memory(service)  # the memory load runs at any hour, with the switch
        assert (cal.cached_snapshot() is not None) is enabled
    assert provider.calls == []
    cal.reset_memory()
    weekend = _service(tmp_path, monkeypatch, now=SUNDAY_1100_NY)
    assert cal.refresh_earnings_cache(weekend, provider=provider)["day"] == "2026-10-18"


def test_it_never_runs_while_crypto_trades(tmp_path, monkeypatch) -> None:
    # Crypto trades around the clock: every tick's crypto scan places orders inside the same
    # tick budget as maintenance, so no tick is off-hours (as the short shadow ledger).
    provider = _Provider({"AAA": ["2026-10-28"]})
    for now in (MONDAY_0100_NY, MONDAY_2100_NY, SUNDAY_1100_NY):
        service = _service(
            tmp_path, monkeypatch, now=now, crypto_trading_enabled=True, crypto_symbols=["BTC/USD"]
        )
        assert cal.refresh_earnings_cache(service, provider=provider) is None
        assert service.runtime_state.reads == [] and service.run_logs.items == []
    assert provider.calls == [] and cal.cached_snapshot() is None
    off = _service(tmp_path, monkeypatch, crypto_trading_enabled=False, crypto_symbols=["BTC/USD"])
    assert cal.refresh_earnings_cache(off, provider=provider)["fetched"] == 3


@pytest.mark.parametrize("zone", ["America/New_York", "Europe/London"])
def test_the_day_is_new_yorks_whatever_the_utc_or_schedule_day(tmp_path, monkeypatch, zone) -> None:
    # 21:00 New York on Monday is Tuesday in UTC and in London: still Monday's one fetch.
    service = _service(tmp_path, monkeypatch, now=MONDAY_2100_NY, schedule_timezone=zone)
    provider = _Provider({"AAA": ["2026-10-19"]})
    summary = cal.refresh_earnings_cache(service, provider=provider)
    assert summary["day"] == "2026-10-19" and summary["done_for_today"]
    record = _stored(service)["symbols"]["AAA"]
    assert (record["checked_on"], record["upcoming"], record["last"]) == (
        "2026-10-19",
        ["2026-10-19"],  # still today's report in New York, not a past one
        None,
    )
    monkeypatch.setattr(cal, "utc_now", lambda: MONDAY_2100_NY + timedelta(hours=2))
    assert cal.refresh_earnings_cache(service, provider=provider) is None  # 23:00: done
    monkeypatch.setattr(cal, "utc_now", lambda: MONDAY_2100_NY + timedelta(hours=4))
    assert cal.refresh_earnings_cache(service, provider=provider)["day"] == "2026-10-20"
    assert provider.calls == ["AAA", "BBB", "CCC"] * 2  # 01:00 Tuesday: the next day's


def test_a_slow_or_failing_fetch_keeps_the_old_dates_and_waits_a_day(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cal, "FETCH_TIMEOUT_SECONDS", 1.0)  # ample for the fakes that answer
    service = _service(tmp_path, monkeypatch)
    yesterday = {"status": "ok", "upcoming": ["2026-10-28"], "last": "2026-07-29"}
    yesterday.update(fetched_on="2026-10-18", checked_on="2026-10-18")
    service.runtime_state.set(
        cal.CACHE_KEY, json.dumps({"day": "2026-10-18", "symbols": {"AAA": yesterday}})
    )
    provider = _Provider({"AAA": ["2026-10-20"], "CCC": ["2026-11-05"]}, block={"AAA"}, fail={"BBB"})
    try:
        summary = cal.refresh_earnings_cache(service, provider=provider)
    finally:
        provider.release.set()
    stalled = _stored(service)["symbols"]["AAA"]
    assert stalled["status"] == "error" and stalled["error"].startswith("TimeoutError")
    assert (stalled["upcoming"], stalled["last"], stalled["fetched_on"]) == (
        ["2026-10-28"],
        "2026-07-29",
        "2026-10-18",
    )  # yesterday's dates are kept
    assert _stored(service)["symbols"]["BBB"]["error"] == "RuntimeError: Too Many Requests"
    assert summary["failed_symbols"] == ["AAA", "BBB"] and summary["fetched"] == 1
    assert summary["done_for_today"]  # a failed fetch counts as the day's one try
    assert cal.refresh_earnings_cache(service, provider=provider) is None
    assert provider.calls == ["AAA", "BBB", "CCC"]


def test_a_failure_streak_or_the_budget_leaves_the_rest_for_the_next_run(
    tmp_path, monkeypatch
) -> None:
    symbols = tuple(f"S{i}" for i in range(6))
    service = _service(tmp_path, monkeypatch, symbols=symbols)
    down = _Provider(fail=set(symbols))
    first = cal.refresh_earnings_cache(service, provider=down)
    assert down.calls == ["S0", "S1", "S2"]  # Yahoo throttling: stop, never burn the list
    assert (first["stopped"], first["remaining"], first["done_for_today"]) == (
        "failure_streak",
        3,
        False,
    )
    up = _Provider({"S3": ["2026-10-30"]})
    assert cal.refresh_earnings_cache(service, provider=up)["done_for_today"]
    assert up.calls == ["S3", "S4", "S5"]  # the failed three wait for tomorrow

    timed = _service(tmp_path, monkeypatch, symbols=symbols)
    cal.reset_memory()
    ticks = itertools.count(0, 10)  # each clock read is 10 s later; the budget is 30 s
    slow = _Provider()
    summary = cal.refresh_earnings_cache(timed, provider=slow, clock=lambda: next(ticks))
    assert slow.calls == ["S0", "S1"] and summary["stopped"] == "budget"  # 8 s left < a timeout
    assert not summary["done_for_today"] and summary["remaining"] == 4


def test_a_listed_date_that_passes_becomes_the_last_report(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch, symbols=("AAA",))
    cal.refresh_earnings_cache(service, provider=_Provider({"AAA": ["2026-10-20"]}))
    monkeypatch.setattr(cal, "utc_now", lambda: MONDAY_0100_NY + timedelta(days=2))
    cal.refresh_earnings_cache(service, provider=_Provider({"AAA": ["2027-01-28"]}))
    record = cal.cached_snapshot()["symbols"]["AAA"]
    assert record["upcoming"] == ["2027-01-28"] and record["last"] == "2026-10-20"
    # Tuesday's report, then a Tuesday signal bar entering Wednesday: flagged.
    flag = risk.assess(
        record,
        signal_day=date(2026, 10, 20),
        entry_day=date(2026, 10, 21),
        hold=risk.HOLD_SESSIONS,
        today=date(2026, 10, 21),
    )
    assert flag["would_block"] and flag["reason"] == "earnings_before_entry"
    assert flag["earnings_date"] == "2026-10-20"


def test_the_last_report_survives_every_refresh_after_yahoo_moves_on(tmp_path, monkeypatch) -> None:
    # Friday's report, one refresh per New York day Thursday..Monday (the weekend refreshes
    # too); from Saturday Yahoo lists only next quarter. Monday's entry from Friday's bar must
    # still see Friday's report, else it is counted as unflagged in the ledger cohorts.
    service = _service(tmp_path, monkeypatch, symbols=("AAA",))
    friday, next_quarter = ["2026-10-23"], ["2027-01-28"]
    days = {3: friday, 4: friday, 5: next_quarter, 6: next_quarter, 7: next_quarter}
    for offset, dates in days.items():  # Thu 10-22 .. Mon 10-26, 01:00 New York each
        monkeypatch.setattr(cal, "utc_now", lambda o=offset: MONDAY_0100_NY + timedelta(days=o))
        summary = cal.refresh_earnings_cache(service, provider=_Provider({"AAA": dates}))
        assert summary["fetched"] == 1 and summary["done_for_today"]
        record = cal.cached_snapshot()["symbols"]["AAA"]
        assert record["last"] == (None if offset < 5 else "2026-10-23"), record["checked_on"]
    assert record == {
        "status": "ok",
        "upcoming": ["2027-01-28"],
        "last": "2026-10-23",  # two refreshes after the one that turned it into the last
        "fetched_on": "2026-10-26",
        "checked_on": "2026-10-26",
    }
    assert _stored(service)["symbols"]["AAA"] == record
    flag = risk.assess(
        record,
        signal_day=date(2026, 10, 23),
        entry_day=date(2026, 10, 26),
        hold=risk.HOLD_SESSIONS,
        today=date(2026, 10, 26),
    )
    assert flag["would_block"] and flag["reason"] == "earnings_before_entry"
    assert flag["earnings_date"] == "2026-10-23"


@pytest.mark.parametrize("stored", ["not json", "[1]", json.dumps({"symbols": {"AAA": "x"}})])
def test_an_unreadable_stored_copy_starts_over(tmp_path, monkeypatch, stored) -> None:
    service = _service(tmp_path, monkeypatch, symbols=("AAA",))
    service.runtime_state.set(cal.CACHE_KEY, stored)
    provider = _Provider({"AAA": ["2026-10-28"]})
    assert cal.refresh_earnings_cache(service, provider=provider)["done_for_today"]
    assert provider.calls == ["AAA"] and _stored(service)["symbols"]["AAA"]["status"] == "ok"


def test_an_empty_answer_keeps_a_report_still_ahead_until_it_is_stale(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch, symbols=("AAA", "BBB"))
    cal.refresh_earnings_cache(service, provider=_Provider({"AAA": ["2026-10-28"]}))
    monkeypatch.setattr(cal, "utc_now", lambda: MONDAY_0100_NY + timedelta(days=1))
    cal.refresh_earnings_cache(service, provider=_Provider())  # Yahoo answers an empty calendar
    aaa, bbb = (cal.cached_snapshot()["symbols"][s] for s in ("AAA", "BBB"))
    assert (aaa["status"], aaa["upcoming"], aaa["fetched_on"]) == (
        "empty",
        ["2026-10-28"],
        "2026-10-19",  # the day the dates were read, so they age out
    )
    assert aaa["checked_on"] == "2026-10-20" and bbb["status"] == "none"  # nothing to keep
    kw = {"signal_day": date(2026, 10, 23), "entry_day": date(2026, 10, 26), "hold": 10}
    assert risk.assess(aaa, today=date(2026, 10, 26), **kw)["would_block"]
    assert risk.assess(aaa, today=date(2026, 10, 27), **kw)["reason"] == "stale_earnings_data"


def test_after_a_restart_the_stored_copy_is_loaded_once(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch, symbols=("AAA",))
    cal.refresh_earnings_cache(service, provider=_Provider({"AAA": ["2026-10-28"]}))
    stored = _stored(service)
    cal.reset_memory()  # a redeploy
    reads = len(service.runtime_state.reads)
    cal.warm_memory(service)
    cal.warm_memory(service)
    assert cal.cached_snapshot() == stored and len(service.runtime_state.reads) == reads + 1

    class _Down(_CountingState):
        def get(self, key):
            super().get(key)
            raise RuntimeError("pool timeout")

    cal.reset_memory()
    service.runtime_state = _Down()
    clock = iter([0.0, 10.0, 301.0, 302.0])
    for _ in range(3):
        cal.warm_memory(service, clock=lambda: next(clock))  # never raises
    assert cal.cached_snapshot() is None
    assert len(service.runtime_state.reads) == 2  # retried after 5 minutes, not every tick


def test_the_yahoo_provider_reads_the_calendar_without_the_network(monkeypatch) -> None:
    import yfinance

    seen = []

    class _Ticker:
        calendar = {"Earnings Date": [date(2026, 10, 29), date(2026, 10, 27)], "Earnings High": 2}

        def __init__(self, symbol):
            seen.append(symbol)

    monkeypatch.setattr(yfinance, "Ticker", _Ticker)
    assert cal.yahoo_earnings_dates("brk.b") == ["2026-10-27", "2026-10-29"]
    assert seen == ["BRK-B"]
    _Ticker.calendar = {}
    assert cal.yahoo_earnings_dates("AAPL") == []
    mixed = [pd.Timestamp("2026-10-29 20:00", tz="UTC"), datetime(2026, 10, 30, 1), "2026-11-02T00:00Z"]
    odd = [None, "soon", 5, pd.NaT, float("nan")]
    assert cal.iso_dates([*mixed, *odd]) == ["2026-10-29", "2026-10-30", "2026-11-02"]
    assert cal.iso_dates(None) == [] and cal.iso_dates("2026-10-29") == ["2026-10-29"]


# --------------------------------------------------------------------------- the rule


@pytest.mark.parametrize(
    "earnings, reason, blocked",
    [
        ("2026-10-16", "earnings_before_entry", True),  # the signal bar's session (Friday)
        ("2026-10-19", "earnings_on_entry_day", True),
        ("2026-10-30", "earnings_within_hold", True),  # the 10th session from Monday 10-19
        ("2026-11-02", "no_earnings_in_window", False),  # the 11th
        ("2026-10-15", "no_earnings_in_window", False),  # before the signal bar: in its price
    ],
)
def test_the_window_runs_from_the_signal_bar_through_ten_sessions(earnings, reason, blocked) -> None:
    record = {"status": "ok", "upcoming": [earnings], "last": None, "fetched_on": "2026-10-19"}
    flag = risk.assess(
        record,
        signal_day=date(2026, 10, 16),
        entry_day=date(2026, 10, 19),
        hold=risk.HOLD_SESSIONS,
        today=date(2026, 10, 19),
    )
    assert (flag["reason"], flag["would_block"]) == (reason, blocked)
    assert flag["window_end"] == "2026-10-30"
    assert flag["earnings_date"] == (earnings if earnings != "2026-10-15" else None)


def test_intraday_entries_are_flattened_so_only_the_entry_session_counts(tmp_path) -> None:
    settings = make_settings(tmp_path)
    assert risk.hold_sessions("5m", settings) == 1 and risk.hold_sessions("1d", settings) == 10
    carried = make_settings(tmp_path, intraday_flatten_minutes_before_close=0)
    assert risk.hold_sessions("15m", carried) == 10  # not flattened: held like a swing
    record = {"status": "ok", "upcoming": ["2026-10-20"], "fetched_on": "2026-10-19"}
    day = date(2026, 10, 19)
    kw = {"signal_day": day, "entry_day": day, "today": day}
    assert not risk.assess(record, hold=1, **kw)["would_block"]  # tomorrow: already flat
    assert risk.assess({**record, "upcoming": ["2026-10-19"]}, hold=1, **kw)["would_block"]
    assert risk.add_sessions(date(2026, 10, 16), 1) == date(2026, 10, 19)  # Friday -> Monday
    assert risk.next_session(date(2026, 10, 18)) == date(2026, 10, 19)


def test_missing_stale_odd_or_fund_data_never_flags() -> None:
    kw = {
        "signal_day": date(2026, 10, 16),
        "entry_day": date(2026, 10, 19),
        "hold": 10,
        "today": date(2026, 10, 19),
    }
    nothing = {"status": "none", "upcoming": [], "last": None, "fetched_on": "2026-10-19"}
    never_fetched = {"status": "error", "error": "TimeoutError", "checked_on": "2026-10-19"}
    odd = {"status": "ok", "upcoming": ["soon", None, 5], "last": "n/a", "fetched_on": "2026-10-19"}
    for record in (None, nothing, never_fetched, odd, "garbage"):
        flag = risk.assess(record, **kw)
        assert (flag["would_block"], flag["reason"]) == (False, "no_earnings_data")
    stale = {"status": "ok", "upcoming": ["2026-10-20"], "fetched_on": "2026-10-09"}
    assert risk.assess(stale, **kw)["reason"] == "stale_earnings_data"
    assert not risk.assess(stale, **kw)["would_block"]
    assert risk.assess({"status": "not_applicable"}, **kw)["reason"] == "not_applicable"


# --------------------------------------------------------------------------- (a) the deep scan


def _cache(*, fetched_on="2026-10-12") -> dict:
    """Signal bar Fri 10-09, entry Mon 10-12, window to Fri 10-23."""

    def record(upcoming=(), last=None):
        return {"status": "ok", "upcoming": list(upcoming), "last": last, "fetched_on": fetched_on}

    return {
        "day": fetched_on,
        "symbols": {
            "S00": record(["2026-10-14"]),  # inside the hold
            "S01": record(["2026-11-20"]),  # after it
            "S02": record(["2027-01-20"], last="2026-10-09"),  # after the signal bar's close
        },
    }


def _strip(view: list) -> list:
    """The long scan view without the shadow hints."""

    watchlist = dict(view[0])
    for key in ("signals", "items", "shadow_items"):
        watchlist[key] = [
            {k: v for k, v in item.items() if k != "event_risk"} for item in watchlist[key]
        ]
    return [watchlist, *view[1:]]


def test_long_watchlist_items_carry_the_hint_and_nothing_else_changes(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(risk, "utc_now", lambda: scan_tests.MONDAY_0835_NY)
    off = short_tests._scan(tmp_path / "off", monkeypatch, short_verdicts=True)
    cal._publish(_cache())
    on = short_tests._scan(
        tmp_path / "on", monkeypatch, short_verdicts=True, event_risk_shadow_enabled=True
    )
    view_off, view_on = short_tests._long_scan_view(off), short_tests._long_scan_view(on)
    assert _strip(view_on) == view_off  # ranking, tradeable, items, coverage, logs: identical
    assert short_tests._digest(view_off) == short_tests.GOLDEN_DEEP_SCAN
    signals = view_on[0]["signals"]
    assert signals and all("event_risk" in item for item in signals)
    by_symbol = {item["symbol"]: item["event_risk"] for item in signals}
    assert by_symbol["S00"] == {
        "would_block": True,
        "earnings_date": "2026-10-14",
        "reason": "earnings_within_hold",
        "window_end": "2026-10-23",
    }
    assert (by_symbol["S01"]["would_block"], by_symbol["S01"]["reason"]) == (
        False,
        "no_earnings_in_window",
    )
    assert by_symbol["S02"]["reason"] == "earnings_before_entry"
    assert by_symbol["S04"]["reason"] == "no_earnings_data"  # not in the cache
    shorts = short_tests._shorts(on)
    assert shorts and not any("event_risk" in item for item in shorts)  # long items only
    assert on.notifier.messages == off.notifier.messages  # nothing in any note

    cal.reset_memory()  # the switch on, no earnings ever cached: the list is unchanged
    never = short_tests._scan(
        tmp_path / "never", monkeypatch, short_verdicts=True, event_risk_shadow_enabled=True
    )
    assert short_tests._long_scan_view(never) == view_off
    assert never.runtime_state.get(cal.CACHE_KEY) is None


def test_the_scan_reads_the_stored_copy_once_when_memory_is_empty(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(risk, "utc_now", lambda: scan_tests.MONDAY_0835_NY)
    service = SimpleNamespace(
        settings=make_settings(tmp_path, event_risk_shadow_enabled=True),
        runtime_state=_CountingState(),
    )
    service.runtime_state.set(cal.CACHE_KEY, json.dumps(_cache()))
    hint = risk.watchlist_hint(service)
    assert hint("S00", "2026-10-09 04:00:00+00:00")["would_block"] is True
    assert hint("S01", None)["reason"] == "no_earnings_in_window"  # no bar: the entry session
    assert hint("SPY", "2026-10-09")["reason"] == "not_applicable"  # a fund, cached or not
    assert service.runtime_state.reads == [cal.CACHE_KEY] and cal.cached_snapshot() == _cache()
    risk.watchlist_hint(service)
    assert service.runtime_state.reads == [cal.CACHE_KEY]  # memory from then on

    class _Down(_CountingState):
        def get(self, key):
            raise RuntimeError("pool timeout")

    cal.reset_memory()
    service.runtime_state = _Down()
    assert risk.watchlist_hint(service) is None  # the scan goes on without hints
    cal._publish(_cache())
    off = SimpleNamespace(settings=make_settings(tmp_path), runtime_state=_Down())
    assert risk.watchlist_hint(off) is None  # the switch off: no hint, cached or not


def test_the_hint_enters_on_the_new_york_session_whatever_schedule_timezone(
    tmp_path, monkeypatch
) -> None:
    # 08:35 New York on Monday 10-12 is already Tuesday in Auckland: the entry is still Monday.
    monkeypatch.setattr(risk, "utc_now", lambda: scan_tests.MONDAY_0835_NY)
    cal._publish(_cache())
    for zone in ("America/New_York", "Pacific/Auckland"):
        settings = make_settings(tmp_path, event_risk_shadow_enabled=True, schedule_timezone=zone)
        hint = risk.watchlist_hint(SimpleNamespace(settings=settings, runtime_state=FakeState()))
        flag = hint("S00", "2026-10-09")
        assert (flag["would_block"], flag["window_end"]) == (True, "2026-10-23"), zone


# --------------------------------------------------------------------------- (c) the reports


def test_the_shadow_ledger_keeps_the_hint_and_reports_flagged_against_unflagged(
    tmp_path, monkeypatch
) -> None:
    service = ledger_tests._service(tmp_path, monkeypatch)
    flagged = {"would_block": True, "earnings_date": "2026-10-14", "reason": "earnings_within_hold"}
    clear = {"would_block": False, "earnings_date": None, "reason": "no_earnings_in_window"}
    signals = [
        {**ledger_tests._signal("S00"), "event_risk": {**flagged, "window_end": "2026-10-23"}},
        {**ledger_tests._signal("S01", stop=99.5), "event_risk": {**clear, "window_end": "x"}},
    ]
    service.runtime_state.set(
        "premarket:watchlist:2026-10-12", json.dumps({"status": "complete", "signals": signals})
    )
    ledger_tests.refresh_shadow_ledger(service)
    entries = json.loads(service.runtime_state.get(ledger_tests.LEDGER_KEY))["entries"]
    assert entries["2026-10-12|S00|trend_following:1d"]["event_risk"]["would_block"] is True
    assert "event_risk" not in entries["2026-10-13|S00|trend_following:1d"]  # no hint recorded
    report = risk.event_risk_report(service.runtime_state)
    cohorts = report["shadow_ledger"]
    assert cohorts["flagged"]["taken"] == 1 and cohorts["flagged"]["open"] == 1  # S00 still open
    assert cohorts["unflagged"]["taken"] == 1 and cohorts["unflagged"]["stop_target"]["closed"] == 1
    assert cohorts["not_assessed"]["taken"] == 0  # the 10-13 S00 setup was skipped_open
    assert report["shadow_only"] and report["rule"].startswith("would block")


def test_live_trades_are_split_by_their_proposal_log() -> None:
    logs = [
        {"payload": {"proposal_id": "p1", "would_block": True, "reason": "earnings_within_hold"}},
        {"payload": {"proposal_id": "p1", "would_block": False, "reason": "no_cache"}},  # older
        {"payload": {"proposal_id": "p2", "would_block": False, "reason": "no_earnings_in_window"}},
        {"payload": {"proposal_id": "p3", "would_block": False, "reason": "no_cache"}},
        {"payload": "bad"},
    ]
    trades = [
        {"proposal_id": "p1", "status": "closed", "realized_r": -1.0, "pnl_usd": -155.5},
        {"proposal_id": "p2", "status": "closed", "realized_r": 1.3, "pnl_usd": 36.3},
        {"proposal_id": "p2", "status": "open"},
        {"proposal_id": "p3", "status": "open"},
        {"proposal_id": "p9", "status": "open"},
    ]
    live = risk.event_risk_report(FakeState(), trades=trades, proposal_logs=logs)["live_trades"]
    assert live["flagged"] == {
        "trades": 1,
        "closed": 1,
        "avg_realized_r": -1.0,
        "win_rate": 0.0,
        "pnl_usd": -155.5,
    }
    assert (live["unflagged"]["trades"], live["unflagged"]["avg_realized_r"]) == (2, 1.3)
    assert live["no_data"]["trades"] == 1 and live["not_assessed"]["trades"] == 1


def test_the_live_scorecard_carries_the_section(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from app.broker.etoro_live_scorecard import SCORECARD_KEY
    from app.main import create_app
    from app.storage.repositories import RunLogRepository, RuntimeStateRepository
    from tests.conftest import MockBroker

    app = create_app(
        make_settings(tmp_path, control_api_token="secret"),
        broker=MockBroker(),
        enable_background_jobs=False,
    )
    card = {"symbol": "AMD", "strategy_name": "trend_following", "timeframe": "1d"}
    card.update(proposal_id="p1", status="closed", realized_r=-1.0, pnl_usd=-155.51)
    RuntimeStateRepository(app.state.db).set(SCORECARD_KEY, json.dumps({"e1": card}))
    payload = {"proposal_id": "p1", "symbol": "AMD", "would_block": True, "shadow_only": True}
    RunLogRepository(app.state.db).log(risk.EVENT, {**payload, "reason": "earnings_within_hold"})
    body = (
        TestClient(app)
        .get("/performance/live-scorecard", headers={"X-Control-Token": "secret"})
        .json()
    )
    section = body["event_risk_shadow"]
    assert section["live_trades"]["flagged"]["trades"] == 1 and section["proposals_flagged"] == 1
    assert body["trades"][0]["symbol"] == "AMD"  # the scorecard itself is unchanged


# --------------------------------------------------------------------------- (b) proposals


class _Proposals:
    def __init__(self):
        self.created = []

    def list_proposals(self, *, status):
        return []

    def create_proposal(self, request):
        self.created.append(request)
        return SimpleNamespace(
            id=f"proposal_{len(self.created)}",
            order=SimpleNamespace(
                symbol=request.symbol,
                proposed_price=request.proposed_price,
                stop_loss=request.stop_loss,
                take_profit=request.take_profit,
            ),
        )


def _candidates():
    from tests.test_workflow_service import _snapshot

    weak = {
        "alert_eligible": True,
        "source": "supervised_weak_valid",
        "signal_classification": "supervised_weak_valid",
        "production_qualified": False,
        "supervised_approval_required": True,
    }
    base = _snapshot().model_copy(
        update={"execution_ready": True, "metadata": weak, "candle_timestamp": "2026-10-16T04:00Z"}
    )
    return [base, base.model_copy(update={"symbol": "AMD"})]


def _propose(tmp_path, monkeypatch, *, enabled=True, logs=None, state=None):
    from app.models.screener import ScreenerRunResponse
    from app.workflow.operations import auto_propose_candidates
    from app.workflow.service import SignalWorkflowService
    from tests.test_workflow_service import (
        FakeAlertHistory,
        FakeMarketDataEngine,
        FakeMarketScreener,
        FakeNotifier,
        FakeSupervisedAutoTrading,
        FakeTrackedSignals,
        MarketQuote,
    )

    monkeypatch.setattr(risk, "utc_now", lambda: MONDAY_1000_NY)
    proposals, trading = _Proposals(), FakeSupervisedAutoTrading()
    workflow = SignalWorkflowService(
        settings=make_settings(
            tmp_path,
            auto_propose_enabled=True,
            paper_auto_operation_mode="supervised",
            event_risk_shadow_enabled=enabled,
        ),
        market_screener=FakeMarketScreener([]),
        market_data_engine=FakeMarketDataEngine(MarketQuote(symbol="NVDA", last_execution=101.0)),
        notifier=FakeNotifier(),
        tracked_signals=FakeTrackedSignals(),
        alert_history=FakeAlertHistory(),
        runtime_state=state or _CountingState(),
        run_logs=logs or FakeLogs(),
        proposal_service=proposals,
        auto_trading_service=trading,
    )
    response = ScreenerRunResponse(
        generated_at="2026-10-19T14:00:00+00:00",
        universe_name="top100_us",
        timeframes=["1d"],
        evaluated_symbols=2,
        evaluated_strategy_runs=2,
        candidates=_candidates(),
    )
    created = auto_propose_candidates(workflow, response, origin="swing_scan", notify=False)
    return created, workflow, proposals, trading


def _no_fetch(monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise AssertionError("the proposal path fetched earnings")

    for name in ("yahoo_earnings_dates", "_bounded", "refresh_earnings_cache", "snapshot_for_scan"):
        monkeypatch.setattr(cal, name, boom)


def test_each_proposal_gets_one_log_from_memory_only(tmp_path, monkeypatch) -> None:
    cal._publish(
        {
            "day": "2026-10-19",
            "symbols": {
                "NVDA": {"status": "ok", "upcoming": ["2026-10-22"], "fetched_on": "2026-10-19"},
                "AMD": {"status": "ok", "upcoming": ["2027-01-27"], "fetched_on": "2026-10-19"},
            },
        }
    )
    _no_fetch(monkeypatch)
    created, workflow, proposals, trading = _propose(tmp_path, monkeypatch)
    assert created == 2 and trading.approve_calls == 2
    events = [event for event, _ in workflow.run_logs.items]
    assert events[-3:] == ["auto_propose_funnel", risk.EVENT, risk.EVENT]  # after every order
    nvda, amd = (payload for event, payload in workflow.run_logs.items if event == risk.EVENT)
    assert {k: nvda[k] for k in ("symbol", "strategy", "proposal_id", "earnings_date")} == {
        "symbol": "NVDA",
        "strategy": "momentum_breakout",
        "proposal_id": "proposal_1",
        "earnings_date": "2026-10-22",
    }
    assert (nvda["would_block"], nvda["reason"], nvda["shadow_only"]) == (
        True,
        "earnings_within_hold",
        True,
    )
    assert (nvda["signal_day"], nvda["entry_day"], nvda["hold_sessions"]) == (
        "2026-10-16",
        "2026-10-19",
        10,
    )
    assert (amd["proposal_id"], amd["would_block"], amd["reason"]) == (
        "proposal_2",
        False,
        "no_earnings_in_window",
    )
    assert not any(key.startswith("event_risk") for key in workflow.runtime_state.reads)


@pytest.mark.parametrize("memory", [None, {"symbols": {}}])  # never refreshed / nothing stored
def test_before_any_cache_the_log_says_so_and_still_reads_nothing(
    tmp_path, monkeypatch, memory
) -> None:
    if memory is not None:
        cal._publish(memory)
    _no_fetch(monkeypatch)
    created, workflow, _, _ = _propose(tmp_path, monkeypatch)
    rows = [payload for event, payload in workflow.run_logs.items if event == risk.EVENT]
    assert created == 2 and [r["reason"] for r in rows] == ["no_cache", "no_cache"]
    assert not any(r["would_block"] for r in rows)
    assert not any(key.startswith("event_risk") for key in workflow.runtime_state.reads)


def test_proposals_orders_and_other_logs_are_identical_with_the_check_on_or_off(
    tmp_path, monkeypatch
) -> None:
    cal._publish({"symbols": {"NVDA": {"status": "ok", "upcoming": ["2026-10-22"], "fetched_on": "2026-10-19"}}})
    on = _propose(tmp_path / "on", monkeypatch, enabled=True)
    off = _propose(tmp_path / "off", monkeypatch, enabled=False)
    assert on[0] == off[0] == 2 and on[3].approve_calls == off[3].approve_calls == 2
    assert [r.model_dump() for r in on[2].created] == [r.model_dump() for r in off[2].created]
    others = [(e, p) for e, p in on[1].run_logs.items if e != risk.EVENT]
    assert others == off[1].run_logs.items and len(on[1].run_logs.items) == len(others) + 2
    assert not any(e == risk.EVENT for e, _ in off[1].run_logs.items)


def test_a_failing_log_write_is_swallowed_and_stops_the_rest(tmp_path, monkeypatch) -> None:
    class _Failing(FakeLogs):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        def log(self, event_type, payload):
            if event_type == risk.EVENT:
                self.attempts += 1
                raise RuntimeError("pool timeout")
            super().log(event_type, payload)

    logs = _Failing()
    created, _, proposals, trading = _propose(tmp_path, monkeypatch, logs=logs)
    assert created == 2 and len(proposals.created) == 2 and trading.approve_calls == 2
    assert logs.attempts == 1  # one pool timeout, not one per proposal
    monkeypatch.setattr(risk, "assess", lambda *a, **k: 1 / 0)
    cal._publish({"symbols": {"NVDA": {"status": "none", "fetched_on": "2026-10-19"}}})
    created, workflow, _, _ = _propose(tmp_path / "b", monkeypatch)
    rows = [p for e, p in workflow.run_logs.items if e == risk.EVENT]
    assert created == 2 and [r["reason"] for r in rows] == ["error", "error"]


def test_the_entry_session_hold_and_funds_on_the_proposal_log(tmp_path, monkeypatch) -> None:
    upcoming = {"status": "ok", "upcoming": ["2026-10-20"], "fetched_on": "2026-10-19"}
    cal._publish({"symbols": {"NVDA": upcoming}})
    service = SimpleNamespace(
        settings=make_settings(tmp_path, event_risk_shadow_enabled=True), run_logs=FakeLogs()
    )

    def candidate(symbol, timeframe="1d"):
        return SimpleNamespace(symbol=symbol, timeframe=timeframe, strategy_name="s")

    monkeypatch.setattr(risk, "utc_now", lambda: MONDAY_1000_NY)
    created = [("p1", candidate("NVDA", "5m")), ("p2", candidate("SPY")), ("p3", candidate("BTC/USD"))]
    risk.log_proposal_event_risk(service, [*created, ("p4", candidate("NVDA"))])
    rows = [payload for _, payload in service.run_logs.items]
    assert [(r["proposal_id"], r["reason"]) for r in rows] == [
        ("p1", "no_earnings_in_window"),  # an intraday entry is flat before tomorrow's report
        ("p2", "not_applicable"),
        ("p3", "not_applicable"),
        ("p4", "earnings_within_hold"),
    ]
    assert rows[0]["hold_sessions"] == 1 and rows[0]["signal_day"] == "2026-10-19"  # no bar
    monkeypatch.setattr(risk, "utc_now", lambda: MONDAY_1000_NY + timedelta(hours=6, minutes=5))
    risk.log_proposal_event_risk(service, [("p5", candidate("NVDA", "5m"))])
    late = service.run_logs.items[-1][1]  # 16:05 New York: it enters on Tuesday's session
    assert (late["entry_day"], late["would_block"], late["reason"]) == (
        "2026-10-20",
        True,
        "earnings_on_entry_day",
    )
    off = SimpleNamespace(settings=make_settings(tmp_path), run_logs=FakeLogs())
    risk.log_proposal_event_risk(off, created)
    assert off.run_logs.items == []


@pytest.mark.parametrize("zone", ["America/New_York", "Asia/Tokyo"])
def test_the_entry_day_is_new_yorks_whatever_the_utc_or_schedule_clock(
    tmp_path, monkeypatch, zone
) -> None:
    # 13:00 New York is past 16:00 in UTC and Tuesday in Tokyo: it still enters on Monday.
    upcoming = {"status": "ok", "upcoming": ["2026-10-20"], "fetched_on": "2026-10-19"}
    cal._publish({"symbols": {"NVDA": upcoming}})
    settings = make_settings(tmp_path, event_risk_shadow_enabled=True, schedule_timezone=zone)
    service = SimpleNamespace(settings=settings, run_logs=FakeLogs())
    candidate = SimpleNamespace(symbol="NVDA", timeframe="5m", strategy_name="s")
    monkeypatch.setattr(risk, "utc_now", lambda: MONDAY_1300_NY)
    risk.log_proposal_event_risk(service, [("p1", candidate)])
    row = service.run_logs.items[-1][1]
    assert (row["entry_day"], row["window_end"], row["would_block"], row["reason"]) == (
        "2026-10-19",
        "2026-10-19",
        False,
        "no_earnings_in_window",  # an intraday entry is flat before Tuesday's report
    )
    monkeypatch.setattr(risk, "utc_now", lambda: MONDAY_1300_NY + timedelta(hours=3, minutes=5))
    risk.log_proposal_event_risk(service, [("p2", candidate)])
    assert service.run_logs.items[-1][1]["entry_day"] == "2026-10-20"  # 16:05 New York


def test_the_proposal_log_has_no_network_or_database_reads_in_its_source() -> None:
    source = inspect.getsource(risk.log_proposal_event_risk)
    assert "cached_snapshot()" in source
    for token in ("runtime_state", "yahoo", "refresh_earnings_cache", "snapshot_for_scan"):
        assert token not in source, token


# --------------------------------------------------------------------------- wiring


def test_maintenance_warms_and_refreshes_it_in_isolation(tmp_path, monkeypatch) -> None:
    import app.workflow.shadow_ledger as shadow
    from app.workflow.operations import refresh_phase_gates

    calls = []
    monkeypatch.setattr(shadow, "refresh_shadow_ledger", lambda service: calls.append("long") or {})
    monkeypatch.setattr(cal, "warm_memory", lambda service: calls.append("warm"))

    def refresh(service):
        calls.append("earnings")
        raise RuntimeError("boom")

    monkeypatch.setattr(cal, "refresh_earnings_cache", refresh)
    service = SimpleNamespace(
        settings=make_settings(tmp_path, event_risk_shadow_enabled=True),
        runtime_state=FakeState(),
        run_logs=FakeLogs(),
        market_screener=SimpleNamespace(),
        auto_trading=SimpleNamespace(),
        _is_due=lambda key, minutes: True,
    )
    completed, errors = [], []
    refresh_phase_gates(service, completed, errors)
    assert calls == ["warm", "long", "earnings"] and "premarket_shadow_ledger" in completed
    assert errors[-1] == "phase_gates_refresh:event_risk_earnings_refresh:boom"
    monkeypatch.setattr(cal, "refresh_earnings_cache", lambda service: {"fetched": 1})
    completed = []
    refresh_phase_gates(service, completed, [])
    assert "event_risk_earnings_refresh" in completed


def test_the_switch_is_on_by_default_and_logged_at_boot(tmp_path) -> None:
    from app.automation.policy_snapshot import effective_execution_policy
    from app.runtime_settings import AppSettings

    assert AppSettings.model_fields["event_risk_shadow_enabled"].default is True
    on = make_settings(tmp_path, event_risk_shadow_enabled=True)
    assert effective_execution_policy(on)["event_risk_shadow_enabled"] is True
    off = make_settings(tmp_path)  # tests default it off (wall clock, network by default)
    assert effective_execution_policy(off)["event_risk_shadow_enabled"] is False


def test_nothing_that_decides_a_trade_reads_the_flag() -> None:
    app_root = Path(risk.__file__).resolve().parents[1]
    deciding = ("broker/", "automation/", "risk/", "execution/", "approvals/", "screener/")
    deciding += ("signals/", "paper/", "strategies/", "workflow/service.py")
    for path in app_root.rglob("*.py"):
        rel = path.relative_to(app_root).as_posix()
        text = path.read_text()
        if rel.startswith(deciding) and rel != "automation/policy_snapshot.py":  # boot log
            assert "event_risk" not in text and "earnings_calendar" not in text, rel
    scan = Path(app_root / "workflow" / "premarket_deep_scan.py").read_text()
    rank = scan[scan.index("def rank_key") : scan.index("def run_premarket_deep_scan")]
    assert "event_risk" not in rank
