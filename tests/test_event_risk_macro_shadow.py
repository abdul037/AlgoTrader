"""Event-risk macro calendar (CPI, PPI, jobs report, FOMC statement days), SHADOW ONLY.

Operator-approved 2026-10-18 (post-mortem item 1, the calendar part): an entry on an event day,
or held across one inside the earnings rule's hold window, is flagged on the same hint and log
row; nothing is blocked. The dates are a static table in code: no test (and no code path)
touches the network for them.
"""

from __future__ import annotations

import inspect
import json
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import app.workflow.earnings_calendar as cal
import app.workflow.event_risk as risk
import app.workflow.macro_calendar as macro
from tests import test_event_risk_shadow as er_tests
from tests import test_premarket_deep_scan as scan_tests
from tests import test_shadow_ledger as ledger_tests
from tests import test_short_shadow as short_tests
from tests.conftest import make_settings
from tests.test_workflow_service import FakeLogs, FakeState


@pytest.fixture(autouse=True)
def _fresh_memory():
    cal.reset_memory()
    yield
    cal.reset_memory()


def _series(kind, dates=(), *, known_from="2026-10-01", known_through="2026-12-31"):
    source = ("https://example.test",)
    return macro.Series(kind, kind, None, source, known_from, known_through, tuple(dates))


def _calendar(**dates):
    """All four kinds known 2026-10-01..2026-12-31; ``dates`` per kind."""

    return tuple(_series(kind, tuple(dates.get(kind, ()))) for kind in macro.KINDS)


def _clear(flag) -> tuple:
    return flag["would_block"], flag["reason"]


# --------------------------------------------------------------------------- the table


def test_the_table_is_dated_sourced_and_inside_its_horizon() -> None:
    assert macro.CHECKED_ON == "2026-10-19" and macro.TABLE_FROM == "2026-10-01"
    assert tuple(s.kind for s in macro.CALENDAR) == macro.KINDS == ("cpi", "ppi", "jobs", "fomc")
    for series in macro.CALENDAR:
        days = [date.fromisoformat(d) for d in series.dates]  # ISO dates only
        assert list(series.dates) == sorted(set(series.dates)), series.kind  # sorted, unique
        assert all(day.weekday() < 5 for day in days), series.kind  # sessions only
        assert series.known_from <= series.dates[0] and series.dates[-1] <= series.known_through
        assert series.sources and all(
            s.startswith(("https://www.bls.gov/schedule/", "https://www.federalreserve.gov/"))
            for s in series.sources
        )
    by_kind = {s.kind: s for s in macro.CALENDAR}
    for kind in ("cpi", "ppi", "jobs"):  # BLS, 08:30: Oct-Dec 2026 and the twelve 2027 releases
        bls = by_kind[kind]
        assert (bls.time_et, bls.known_through, len(bls.dates)) == ("08:30", "2027-12-31", 15)
        assert sum(d.startswith("2027-") for d in bls.dates) == 12
    fomc = by_kind["fomc"]  # the statement day: the second day of each two-day meeting
    assert fomc.time_et is None and fomc.known_through == fomc.dates[-1] == "2028-01-26"
    assert sum(d.startswith("2027-") for d in fomc.dates) == 8
    # The post-mortem's dates: hot CPI 10-14 and PPI 10-15; the October FOMC on 10-27/28.
    assert {"2026-10-14"} <= set(by_kind["cpi"].dates) and "2026-10-15" in by_kind["ppi"].dates
    assert "2026-10-28" in fomc.dates and "2026-10-02" in by_kind["jobs"].dates
    described = macro.describe()
    assert described["checked_on"] == "2026-10-19" and described["known_through"] == "2027-12-31"
    assert described["kinds"]["fomc"]["source"].endswith("fomccalendars.htm")


def test_the_amd_entry_would_have_been_flagged_by_the_real_table() -> None:
    # AMD: bought Wed 10-07, stopped out 10-15 at the PPI release; CPI came out on 10-14.
    flag = risk._macro_for(date(2026, 10, 7), risk.HOLD_SESSIONS)
    assert flag == {
        "would_block": True,
        "reason": "macro_within_hold",
        "events": [{"date": "2026-10-14", "kind": "cpi"}, {"date": "2026-10-15", "kind": "ppi"}],
        "unknown_kinds": [],
    }
    assert _clear(risk._macro_for(date(2026, 10, 14), 1)) == (True, "macro_on_entry_day")
    assert _clear(risk._macro_for(date(2026, 10, 16), 1)) == (False, "no_macro_in_window")


def test_the_module_reads_nothing_but_its_own_table() -> None:
    source = inspect.getsource(macro)
    for token in ("requests", "urllib", "httpx", "socket", "yfinance", "runtime_state", "run_logs"):
        assert token not in source, token
    assert "import app" not in source and "from app" not in source


# --------------------------------------------------------------------------- the rule


@pytest.mark.parametrize(
    "event, hold, reason, blocked",
    [
        ("2026-11-02", 10, "macro_on_entry_day", True),  # Monday: the entry session
        ("2026-11-13", 10, "macro_within_hold", True),  # the 10th session from Monday 11-02
        ("2026-11-16", 10, "no_macro_in_window", False),  # the 11th
        ("2026-10-30", 10, "no_macro_in_window", False),  # before the entry: in its price
        ("2026-11-03", 1, "no_macro_in_window", False),  # intraday: flat before tomorrow's
        ("2026-11-02", 1, "macro_on_entry_day", True),
    ],
)
def test_an_event_from_the_entry_session_through_the_hold_flags(
    event, hold, reason, blocked
) -> None:
    entry = date(2026, 11, 2)
    for kind in macro.KINDS:
        calendar = _calendar(**{kind: [event]})
        window_end = risk.add_sessions(entry, hold - 1)
        flag = risk.assess_macro(entry_day=entry, window_end=window_end, calendar=calendar)
        assert _clear(flag) == (blocked, reason), kind
        assert flag["events"] == ([{"date": event, "kind": kind}] if blocked else [])
        assert flag["unknown_kinds"] == []


def test_every_event_in_the_window_is_listed_by_date() -> None:
    calendar = _calendar(fomc=["2026-11-04"], cpi=["2026-11-10", "2026-12-10"], jobs=["2026-11-06"])
    flag = risk.assess_macro(
        entry_day=date(2026, 11, 3), window_end=date(2026, 11, 16), calendar=calendar
    )
    assert [(e["date"], e["kind"]) for e in flag["events"]] == [
        ("2026-11-04", "fomc"),
        ("2026-11-06", "jobs"),
        ("2026-11-10", "cpi"),
    ]
    assert flag["reason"] == "macro_within_hold"


def test_a_window_the_table_cannot_vouch_for_is_calendar_unknown_never_clear() -> None:
    calendar = _calendar(cpi=["2026-12-10"])
    past_end = risk.assess_macro(
        entry_day=date(2026, 12, 28), window_end=date(2027, 1, 8), calendar=calendar
    )
    assert past_end == {
        "would_block": False,
        "reason": "calendar_unknown",
        "events": [],
        "unknown_kinds": ["cpi", "ppi", "jobs", "fomc"],
    }
    before = risk.assess_macro(
        entry_day=date(2026, 9, 28), window_end=date(2026, 10, 9), calendar=calendar
    )
    assert _clear(before) == (False, "calendar_unknown")
    one_short = (*_calendar()[:3], _series("fomc", known_through="2026-12-30"))
    flag = risk.assess_macro(
        entry_day=date(2026, 12, 21), window_end=date(2026, 12, 31), calendar=one_short
    )
    assert (flag["reason"], flag["unknown_kinds"]) == ("calendar_unknown", ["fomc"])
    inside = risk.assess_macro(  # the last known day itself is known
        entry_day=date(2026, 12, 21), window_end=date(2026, 12, 31), calendar=_calendar()
    )
    assert _clear(inside) == (False, "no_macro_in_window")
    known_event = risk.assess_macro(  # a known event still flags, the gap is listed
        entry_day=date(2026, 12, 7), window_end=date(2027, 1, 8), calendar=calendar
    )
    assert _clear(known_event) == (True, "macro_within_hold")
    assert known_event["unknown_kinds"] == ["cpi", "ppi", "jobs", "fomc"]


@pytest.mark.parametrize("calendar", [(), _calendar()[:3]])  # nothing verified / a kind missing
def test_an_empty_or_partial_table_reports_calendar_unknown(calendar) -> None:
    flag = risk.assess_macro(
        entry_day=date(2026, 11, 2), window_end=date(2026, 11, 13), calendar=calendar
    )
    assert _clear(flag) == (False, "calendar_unknown")
    assert "fomc" in flag["unknown_kinds"]


def test_the_real_tables_horizon_ends_in_calendar_unknown() -> None:
    late = risk._macro_for(date(2027, 12, 27), risk.HOLD_SESSIONS)  # runs to 2028-01-07
    assert (late["reason"], late["unknown_kinds"]) == ("calendar_unknown", ["cpi", "ppi", "jobs"])
    year_end = risk._macro_for(date(2027, 12, 20), risk.HOLD_SESSIONS)  # to 12-31: still known
    assert (year_end["reason"], year_end["unknown_kinds"]) == ("no_macro_in_window", [])
    assert risk._macro_for(date(2028, 2, 1), 1)["reason"] == "calendar_unknown"
    assert risk._macro_for(date(2026, 9, 30), 1)["reason"] == "calendar_unknown"
    assert risk._macro_for(date(2027, 12, 16), 1)["reason"] == "no_macro_in_window"


def test_a_broken_table_is_an_error_row_never_raised(monkeypatch) -> None:
    monkeypatch.setattr(macro, "scheduled", lambda *a, **k: 1 / 0)
    flag = risk._macro_for(date(2026, 11, 2), 10)
    assert (flag["would_block"], flag["reason"], flag["events"]) == (False, "error", [])
    assert "division" in flag["error"]


# --------------------------------------------------------------------------- the switches


def test_the_sub_switch_is_on_by_default_only_with_the_event_risk_switch(tmp_path) -> None:
    from app.automation.policy_snapshot import effective_execution_policy
    from app.runtime_settings import AppSettings

    assert AppSettings.model_fields["event_risk_macro_shadow_enabled"].default is True
    on = make_settings(tmp_path, event_risk_shadow_enabled=True)
    assert risk.macro_enabled(on) is True
    assert effective_execution_policy(on)["event_risk_macro_shadow_enabled"] is True
    sub_off = make_settings(
        tmp_path, event_risk_shadow_enabled=True, event_risk_macro_shadow_enabled=False
    )
    assert risk.macro_enabled(sub_off) is False
    assert effective_execution_policy(sub_off)["event_risk_macro_shadow_enabled"] is False
    assert risk.macro_enabled(make_settings(tmp_path)) is False  # the event-risk switch off


# --------------------------------------------------------------------------- (a) the deep scan


def test_long_items_carry_the_macro_block_and_nothing_else_changes(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(risk, "utc_now", lambda: scan_tests.MONDAY_0835_NY)
    off = short_tests._scan(tmp_path / "off", monkeypatch, short_verdicts=True)
    view_off = short_tests._long_scan_view(off)
    cal._publish(er_tests._cache())
    on = short_tests._scan(
        tmp_path / "on", monkeypatch, short_verdicts=True, event_risk_shadow_enabled=True
    )
    view_on = short_tests._long_scan_view(on)
    assert er_tests._strip(view_on) == view_off  # ranking, tradeable, coverage, logs: identical
    assert on.notifier.messages == off.notifier.messages
    signals = view_on[0]["signals"]
    assert signals and all("macro" in item["event_risk"] for item in signals)
    # Entry Monday 10-12, window to Friday 10-23: the 10-14 CPI and the 10-15 PPI.
    expected = {
        "would_block": True,
        "reason": "macro_within_hold",
        "events": [{"date": "2026-10-14", "kind": "cpi"}, {"date": "2026-10-15", "kind": "ppi"}],
        "unknown_kinds": [],
    }
    assert all(item["event_risk"]["macro"] == expected for item in signals)
    by_symbol = {item["symbol"]: item["event_risk"] for item in signals}
    assert by_symbol["S00"]["reason"] == "earnings_within_hold"  # the earnings part unchanged
    assert not any("event_risk" in item for item in short_tests._shorts(on))  # long items only

    sub_off = short_tests._scan(
        tmp_path / "sub_off",
        monkeypatch,
        short_verdicts=True,
        event_risk_shadow_enabled=True,
        event_risk_macro_shadow_enabled=False,
    )
    items = short_tests._long_scan_view(sub_off)[0]["signals"]
    assert items and not any("macro" in item["event_risk"] for item in items)


def test_with_no_earnings_cached_the_hint_carries_the_macro_block(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(risk, "utc_now", lambda: scan_tests.MONDAY_0835_NY)
    off = short_tests._scan(tmp_path / "off", monkeypatch, short_verdicts=True)
    never = short_tests._scan(
        tmp_path / "never", monkeypatch, short_verdicts=True, event_risk_shadow_enabled=True
    )
    view = short_tests._long_scan_view(never)
    assert er_tests._strip(view) == short_tests._long_scan_view(off)
    assert never.runtime_state.get(cal.CACHE_KEY) is None
    for item in view[0]["signals"]:
        flag = item["event_risk"]
        earnings = (flag["would_block"], flag["earnings_date"], flag["reason"])
        assert earnings == (False, None, "no_cache")
        assert flag["window_end"] == "2026-10-23" and flag["macro"]["reason"] == "macro_within_hold"


def test_an_unreadable_earnings_copy_still_gives_the_macro_hint(tmp_path, monkeypatch) -> None:
    class _Down(FakeState):
        def get(self, key):
            raise RuntimeError("pool timeout")

    monkeypatch.setattr(risk, "utc_now", lambda: scan_tests.MONDAY_0835_NY)
    settings = make_settings(tmp_path, event_risk_shadow_enabled=True)
    hint = risk.watchlist_hint(SimpleNamespace(settings=settings, runtime_state=_Down()))
    flag = hint("S00", "2026-10-09")
    assert (flag["reason"], flag["macro"]["would_block"]) == ("no_cache", True)
    assert hint("SPY", None)["macro"]["would_block"] is True  # funds too: CPI moves the index
    assert hint("BTC/USD", None)["macro"]["reason"] == "macro_within_hold"


# --------------------------------------------------------------------------- (b) proposals


def test_each_proposal_log_carries_the_macro_block_on_the_same_row(tmp_path, monkeypatch) -> None:
    cal._publish(
        {
            "day": "2026-10-19",
            "symbols": {
                "NVDA": {"status": "ok", "upcoming": ["2026-11-19"], "fetched_on": "2026-10-19"},
                "AMD": {"status": "ok", "upcoming": ["2027-01-27"], "fetched_on": "2026-10-19"},
            },
        }
    )
    er_tests._no_fetch(monkeypatch)
    on = er_tests._propose(tmp_path / "on", monkeypatch)
    sub_off = er_tests._propose(
        tmp_path / "off", monkeypatch, event_risk_macro_shadow_enabled=False
    )
    assert on[0] == sub_off[0] == 2 and on[3].approve_calls == sub_off[3].approve_calls == 2
    assert [r.model_dump() for r in on[2].created] == [r.model_dump() for r in sub_off[2].created]
    rows_on = [p for e, p in on[1].run_logs.items if e == risk.EVENT]
    rows_off = [p for e, p in sub_off[1].run_logs.items if e == risk.EVENT]
    assert len(on[1].run_logs.items) == len(sub_off[1].run_logs.items)  # no extra row
    assert [{k: v for k, v in r.items() if k != "macro"} for r in rows_on] == rows_off
    assert not any("macro" in r for r in rows_off)
    # Monday 10-19 10:00 New York, a daily entry: the window runs to 10-30, the FOMC on 10-28.
    for row in rows_on:
        assert row["macro"] == {
            "would_block": True,
            "reason": "macro_within_hold",
            "events": [{"date": "2026-10-28", "kind": "fomc"}],
            "unknown_kinds": [],
        }
        assert row["would_block"] is False and row["reason"] == "no_earnings_in_window"
    assert on[1].runtime_state.reads == sub_off[1].runtime_state.reads  # no database read added


def test_the_proposal_macro_block_uses_the_entry_session_and_its_hold(
    tmp_path, monkeypatch
) -> None:
    service = SimpleNamespace(
        settings=make_settings(tmp_path, event_risk_shadow_enabled=True), run_logs=FakeLogs()
    )

    def candidate(symbol, timeframe="1d"):
        return SimpleNamespace(symbol=symbol, timeframe=timeframe, strategy_name="s")

    fomc_day = er_tests.MONDAY_1000_NY + timedelta(days=9)  # Wednesday 10-28, 10:00 New York
    monkeypatch.setattr(risk, "utc_now", lambda: fomc_day)
    created = [("p1", candidate("NVDA", "5m")), ("p2", candidate("SPY"))]
    created.append(("p3", candidate("BTC/USD")))
    risk.log_proposal_event_risk(service, created)
    rows = [payload for _, payload in service.run_logs.items]
    assert [(r["proposal_id"], r["macro"]["reason"]) for r in rows] == [
        ("p1", "macro_on_entry_day"),
        ("p2", "macro_on_entry_day"),  # a fund is assessed: the decision moves the index
        ("p3", "macro_on_entry_day"),
    ]
    assert rows[1]["reason"] == "no_cache"  # the earnings part is separate (nothing cached)
    monkeypatch.setattr(risk, "utc_now", lambda: fomc_day + timedelta(hours=6, minutes=5))
    risk.log_proposal_event_risk(service, [("p4", candidate("NVDA", "5m"))])
    late = service.run_logs.items[-1][1]  # 16:05: enters on Thursday's session, flat by its close
    assert (late["entry_day"], late["macro"]["would_block"], late["macro"]["reason"]) == (
        "2026-10-29",
        False,
        "no_macro_in_window",
    )
    risk.log_proposal_event_risk(service, [("p5", candidate("NVDA"))])  # a daily one: 10 sessions
    daily = service.run_logs.items[-1][1]
    assert [e["kind"] for e in daily["macro"]["events"]] == ["jobs", "cpi"]  # 11-06, 11-10


def test_a_candidate_that_breaks_the_earnings_part_still_gets_a_macro_block(
    tmp_path, monkeypatch
) -> None:
    class _Odd:
        symbol = "NVDA"

        @property
        def timeframe(self):
            raise RuntimeError("odd candidate")

    monkeypatch.setattr(risk, "utc_now", lambda: er_tests.MONDAY_1000_NY)
    service = SimpleNamespace(
        settings=make_settings(tmp_path, event_risk_shadow_enabled=True), run_logs=FakeLogs()
    )
    risk.log_proposal_event_risk(service, [("p1", _Odd())])
    row = service.run_logs.items[0][1]
    assert row["reason"] == "error" and row["macro"]["reason"] == "macro_within_hold"


def test_the_macro_part_reads_no_network_or_database_on_the_proposal_path() -> None:
    for fn in (risk.assess_macro, risk._macro_for, risk.log_proposal_event_risk):
        source = inspect.getsource(fn)
        for token in ("runtime_state", "yahoo", "refresh_earnings_cache", "snapshot_for_scan"):
            assert token not in source, (fn.__name__, token)


# --------------------------------------------------------------------------- (c) the reports


def _flag(earnings=None, macro_block=None):
    out = {"would_block": False, "earnings_date": None, "reason": "no_earnings_in_window"}
    out.update(earnings or {})
    if macro_block is not None:
        out["macro"] = macro_block
    return out


HIT = {"would_block": True, "reason": "macro_within_hold", "events": [{"date": "x", "kind": "cpi"}]}
CLEAR = {"would_block": False, "reason": "no_macro_in_window", "events": []}
UNKNOWN = {"would_block": False, "reason": "calendar_unknown", "events": []}
EARNINGS_HIT = {"would_block": True, "earnings_date": "2026-10-14"}
EARNINGS_HIT["reason"] = "earnings_within_hold"


def test_the_shadow_ledger_keeps_the_macro_block_and_reports_its_cohorts(
    tmp_path, monkeypatch
) -> None:
    service = ledger_tests._service(tmp_path, monkeypatch)
    hit = {**_flag(macro_block=HIT), "window_end": "2026-10-23"}
    signals = [
        {**ledger_tests._signal("S00"), "event_risk": hit},
        {**ledger_tests._signal("S01", stop=99.5), "event_risk": _flag(macro_block=CLEAR)},
    ]
    service.runtime_state.set(
        "premarket:watchlist:2026-10-12", json.dumps({"status": "complete", "signals": signals})
    )
    ledger_tests.refresh_shadow_ledger(service)
    entries = json.loads(service.runtime_state.get(ledger_tests.LEDGER_KEY))["entries"]
    kept = entries["2026-10-12|S00|trend_following:1d"]["event_risk"]["macro"]
    assert kept == {**HIT, "unknown_kinds": None}  # the block's own keys only
    report = risk.event_risk_report(service.runtime_state)
    cohorts = report["macro"]["shadow_ledger"]
    assert cohorts["flagged"]["taken"] == 1 and cohorts["flagged"]["open"] == 1
    assert cohorts["unflagged"]["taken"] == 1 and cohorts["unflagged"]["stop_target"]["closed"] == 1
    assert report["macro"]["rule"].startswith("would block") and report["shadow_only"]
    assert report["macro"]["calendar"] == macro.describe()
    assert report["any_event"]["shadow_ledger"]["flagged"]["taken"] == 1
    assert report["shadow_ledger"]["unflagged"]["taken"] == 2  # the earnings split: both clear


@pytest.mark.parametrize(
    "flag, macro_cohort, any_cohort",
    [
        (_flag(macro_block=HIT), "flagged", "flagged"),
        (_flag(macro_block=CLEAR), "unflagged", "unflagged"),
        (_flag(macro_block=UNKNOWN), "no_data", "no_data"),  # calendar_unknown is never clear
        (_flag(macro_block={**CLEAR, "reason": "error"}), "no_data", "no_data"),
        (_flag(EARNINGS_HIT, CLEAR), "unflagged", "flagged"),  # either rule flags
        (_flag(EARNINGS_HIT, UNKNOWN), "no_data", "flagged"),
        (_flag({"reason": "no_cache"}, CLEAR), "unflagged", "no_data"),  # earnings unknown
        (_flag({"reason": "not_applicable"}, CLEAR), "unflagged", "unflagged"),  # a fund
        (_flag(), "not_assessed", "no_data"),  # before the macro part, or its switch off
        (None, "not_assessed", "not_assessed"),
        ({"macro": "garbage"}, "not_assessed", "no_data"),
    ],
)
def test_the_cohorts(flag, macro_cohort, any_cohort) -> None:
    assert (risk._macro_cohort(flag), risk._any_cohort(flag)) == (macro_cohort, any_cohort)


def test_entries_on_an_event_day_are_compared_with_the_other_assessed_ones() -> None:
    on_day = {**HIT, "reason": "macro_on_entry_day"}
    cases = [(on_day, "flagged"), (HIT, "unflagged"), (CLEAR, "unflagged"), (UNKNOWN, "no_data")]
    for block, cohort in cases:
        assert risk._entry_day_cohort(_flag(macro_block=block)) == cohort, block
    assert risk._entry_day_cohort(_flag()) == risk._entry_day_cohort(None) == "not_assessed"
    logs = [
        {"payload": {"proposal_id": "p1", **_flag(macro_block=on_day)}},
        {"payload": {"proposal_id": "p2", **_flag(macro_block=HIT)}},
        {"payload": {"proposal_id": "p3", **_flag(macro_block=CLEAR)}},
    ]
    trades = [
        {"proposal_id": "p1", "status": "closed", "realized_r": -1.0, "pnl_usd": -10.0},
        {"proposal_id": "p2", "status": "closed", "realized_r": 2.0, "pnl_usd": 20.0},
        {"proposal_id": "p3", "status": "closed", "realized_r": 1.0, "pnl_usd": 10.0},
    ]
    report = risk.event_risk_report(FakeState(), trades=trades, proposal_logs=logs)["macro"]
    live = report["entry_day"]["live_trades"]
    assert (live["flagged"]["trades"], live["flagged"]["avg_realized_r"]) == (1, -1.0)
    assert (live["unflagged"]["trades"], live["unflagged"]["avg_realized_r"]) == (2, 1.5)
    assert report["live_trades"]["flagged"]["trades"] == 2  # the whole rule: p1 and p2
    assert set(report["entry_day"]["shadow_ledger"]) == set(risk.COHORTS)


def test_live_trades_are_split_by_their_proposal_logs_macro_block() -> None:
    logs = [
        {"payload": {"proposal_id": "p1", "symbol": "AMD", "entry_day": "2026-10-07",
                     **_flag(macro_block=HIT)}},
        {"payload": {"proposal_id": "p1", **_flag(macro_block=CLEAR)}},  # older: ignored
        {"payload": {"proposal_id": "p2", **_flag(macro_block=CLEAR)}},
        {"payload": {"proposal_id": "p3", **_flag(EARNINGS_HIT, UNKNOWN)}},
        {"payload": {"proposal_id": "p4", **_flag()}},
    ]
    trades = [
        {"proposal_id": "p1", "status": "closed", "realized_r": -1.0, "pnl_usd": -155.5},
        {"proposal_id": "p2", "status": "closed", "realized_r": 1.3, "pnl_usd": 36.3},
        {"proposal_id": "p3", "status": "open"},
        {"proposal_id": "p4", "status": "open"},
        {"proposal_id": "p9", "status": "open"},
    ]
    report = risk.event_risk_report(FakeState(), trades=trades, proposal_logs=logs)
    live = report["macro"]["live_trades"]
    assert (live["flagged"]["trades"], live["flagged"]["avg_realized_r"]) == (1, -1.0)
    assert (live["unflagged"]["trades"], live["unflagged"]["avg_realized_r"]) == (1, 1.3)
    assert live["no_data"]["trades"] == 1 and live["not_assessed"]["trades"] == 2  # p4, p9
    assert report["macro"]["proposals_flagged"] == 1
    assert report["macro"]["recent_flagged"] == [
        {
            "symbol": "AMD",
            "strategy": None,
            "proposal_id": "p1",
            "entry_day": "2026-10-07",
            "reason": "macro_within_hold",
            "events": HIT["events"],
        }
    ]
    either = report["any_event"]
    assert either["live_trades"]["flagged"]["trades"] == 2  # p1 (macro), p3 (earnings)
    assert either["proposals_flagged"] == 2 and either["rule"].startswith("flagged by either")
    assert report["live_trades"]["flagged"]["trades"] == 1  # the earnings split: p3 alone


def test_the_live_scorecard_carries_the_macro_section(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.storage.repositories import RunLogRepository
    from tests.conftest import MockBroker

    app = create_app(
        make_settings(tmp_path, control_api_token="secret"),
        broker=MockBroker(),
        enable_background_jobs=False,
    )
    payload = {"proposal_id": "p1", "symbol": "AMD", **_flag(macro_block=HIT), "shadow_only": True}
    RunLogRepository(app.state.db).log(risk.EVENT, payload)
    body = (
        TestClient(app)
        .get("/performance/live-scorecard", headers={"X-Control-Token": "secret"})
        .json()
    )
    section = body["event_risk_shadow"]
    assert section["macro"]["proposals_flagged"] == 1 and section["proposals_flagged"] == 0
    assert section["macro"]["calendar"]["checked_on"] == macro.CHECKED_ON
    assert section["any_event"]["proposals_flagged"] == 1


# --------------------------------------------------------------------------- wiring


def test_nothing_that_decides_a_trade_reads_the_macro_flag() -> None:
    app_root = Path(risk.__file__).resolve().parents[1]
    deciding = ("broker/", "automation/", "risk/", "execution/", "approvals/", "screener/")
    deciding += ("signals/", "paper/", "strategies/", "workflow/service.py")
    for path in app_root.rglob("*.py"):
        rel = path.relative_to(app_root).as_posix()
        if rel.startswith(deciding) and rel != "automation/policy_snapshot.py":  # boot log
            text = path.read_text()
            assert "macro_calendar" not in text and "assess_macro" not in text, rel
    scan = Path(app_root / "workflow" / "premarket_deep_scan.py").read_text()
    rank = scan[scan.index("def rank_key") : scan.index("def run_premarket_deep_scan")]
    assert "macro" not in rank
