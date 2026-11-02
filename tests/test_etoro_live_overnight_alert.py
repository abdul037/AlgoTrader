"""Overnight adverse-move alert for eToro LIVE positions (operator 2026-10-18): outside US
regular hours, Telegram the operator when a live position gaps down, nears its stop, or a
tracked signal on the symbol hits its own stop. Alert only: nothing is closed or blocked.

All fakes: clock (``now=``), rates (runtime_state marks), daily bars, tracked signals,
notifier, dedupe state and the eToro client. No network.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pandas as pd
import pytest

from app.automation.service import LIVE_OPERATOR_ACKNOWLEDGEMENT
from app.broker import etoro_live_overnight_alert as overnight
from app.broker import etoro_rate_limit
from app.broker.etoro_live_backup_stop import MARKS_KEY, STOPS_KEY, check_backup_stops
from app.broker.etoro_live_guard import EtoroLiveGuard, attach_live_guard, run_live_checks
from app.broker.etoro_live_mirror import STATE_KEY, EtoroLiveMirrorService, build_live_client
from app.broker.etoro_live_overnight_alert import (
    ALERT_STATE_KEY,
    MAX_QUOTE_READS_PER_DAY,
    MAX_SEND_ATTEMPTS,
    OvernightAdverseMoveAlert,
    alert_day,
    in_alert_window,
    last_session_date,
    reference_from_bars,
    signal_window_start,
    stop_hit_reader,
)
from app.broker.etoro_live_room import ROOM_REASONS, room_verdict
from app.broker.etoro_live_scorecard import SCORECARD_KEY
from app.broker.etoro_live_test_order import TEST_STATE_KEY
from app.models.trade import TradeOrder
from app.performance.strategy_evidence import VERDICTS_KEY
from app.storage.db import Database
from app.storage.repositories import TrackedSignalRepository
from app.storage.tracked_signal_stops import recent_stop_hits
from tests.conftest import make_settings
from tests.test_workflow_service import _snapshot

# Thu 15 Oct 2026, 12:11 UTC = 08:11 New York (pre-market) = 16:11 Dubai.
AMD_NOW = datetime.fromisoformat("2026-10-15T12:11:00+00:00")
AMD_ID = 1832


@pytest.fixture(autouse=True)
def _isolated_rate_limiter(monkeypatch):
    monkeypatch.setattr(etoro_rate_limit, "_state", {})


class _State(dict):
    def __init__(self, *args, fail_get=(), fail_set=()):
        super().__init__(*args)
        self.gets: list[str] = []
        self.fail_get, self.fail_set = set(fail_get), set(fail_set)

    def get(self, key, default=None):
        self.gets.append(key)
        if key in self.fail_get:
            raise RuntimeError("database unavailable")
        return super().get(key, default)

    def set(self, key, value):
        if key in self.fail_set:
            raise RuntimeError("database unavailable")
        self[key] = value


class _Logs:
    def __init__(self):
        self.events = []

    def log(self, event, payload):
        self.events.append((event, payload))

    def named(self, event):
        return [p for e, p in self.events if e == event]


class _Notifier:
    def __init__(self, ok=True, enabled=True, raises=False):
        self.sent: list[str] = []
        self.ok, self.enabled, self.raises = ok, enabled, raises

    def send_text(self, text):
        self.sent.append(text)
        if self.raises:
            raise RuntimeError("telegram down")
        return self.ok


class _Client:
    """eToro live client fake: only the rates read is allowed."""

    def __init__(self, quotes=None, fail=False):
        self.settings = SimpleNamespace(
            broker_simulation_enabled=False, etoro_api_key="overnight-alert-test-key"
        )
        self.quotes = {AMD_ID: (547.49, 595.77)} if quotes is None else quotes
        self.fail = fail
        self.requests = []

    def _request(self, method, path, *, params=None, json_body=None, cooldown_on_rate_limit=True):
        self.requests.append((method, path, dict(params or {})))
        assert method == "GET" and path == "/market-data/instruments/rates"
        assert cooldown_on_rate_limit is False  # a 429 here must not blind the backup stop
        if self.fail:
            raise RuntimeError("Broker request failed with status 500")
        ids = [int(i) for i in params["instrumentIds"].split(",")]
        return {
            "rates": [
                {"instrumentID": i, "bid": self.quotes[i][0], "ask": self.quotes[i][1]}
                for i in ids
                if i in self.quotes
            ]
        }

    def fetch_raw_portfolio(self):
        raise AssertionError("the alert never reads the portfolio")

    def close_position_by_id(self, position_id, instrument_id):
        raise AssertionError("the alert never closes anything")

    def open_market_order_by_amount(self, order, *, client_order_id=None):
        raise AssertionError("the alert never opens anything")


class _Bars:
    def __init__(self, frame=None, fail=False):
        self.frame = frame if frame is not None else _frame()
        self.fail = fail
        self.calls = []

    def __call__(self, symbol, *, timeframe, start, end):
        self.calls.append((symbol, timeframe, start, end))
        if self.fail:
            raise RuntimeError("alpaca data unavailable")
        return self.frame


class _Signals:
    """``stop_hits`` reader fake: returns its rows unfiltered (the alert re-filters)."""

    def __init__(self, records=None, fail=False):
        self.records = [_signal()] if records is None else records
        self.fail = fail
        self.calls = []

    def __call__(self, symbols, since_iso):
        self.calls.append((list(symbols), since_iso))
        if self.fail:
            raise RuntimeError("database unavailable")
        return self.records


def _frame(last_close=570.82, atr=23.4, last_day="2026-10-14", days=20, partial=None):
    """Weekday daily bars stamped at midnight New York as Alpaca does (04:00 UTC in summer
    time, 05:00 UTC in winter time), constant close, true range = ``atr``."""

    stamps = pd.bdate_range(end=last_day, periods=days, tz="America/New_York")
    rows = [
        {
            "timestamp": ts.tz_convert("UTC"),
            "open": last_close,
            "high": last_close + atr / 2,
            "low": last_close - atr / 2,
            "close": last_close,
            "volume": 1_000,
        }
        for ts in stamps
    ]
    if partial is not None:  # the next session's unfinished pre-market bar
        nxt = (stamps[-1] + pd.offsets.BDay(1)).tz_convert("UTC")
        rows.append({**rows[-1], "timestamp": nxt, "close": partial})
    return pd.DataFrame(rows)


def _signal(symbol="AMD", closed_at="2026-10-15T12:10:05+00:00", role="entry_long", stop=556.10):
    """A ``recent_stop_hits`` row."""

    return {
        "symbol": symbol,
        "strategy_name": "momentum_breakout",
        "timeframe": "1d",
        "closed_at": closed_at,
        "stop_loss": stop,
        "last_price": 551.34,
        "signal_role": role,
        "current_bid": 547.49,
        "current_ask": 595.77,
    }


def _card(symbol="AMD", open_rate=642.0, stop=541.53, broker_stop=541.53, instrument_id=AMD_ID):
    return {
        "symbol": symbol,
        "status": "open",
        "open_rate": open_rate,
        "signal_price": 643.18,
        "stop_loss": stop,
        "broker_stop": broker_stop,
        "instrument_id": instrument_id,
    }


def _state(cards=None, marks=None, **kwargs):
    state = _State(**kwargs)
    cards = {"et1": _card()} if cards is None else cards
    marks = {"AMD": {"rate": 551.34, "at": "2026-10-15T12:10:30+00:00"}} if marks is None else marks
    dict.__setitem__(state, SCORECARD_KEY, json.dumps(cards))
    dict.__setitem__(state, MARKS_KEY, json.dumps(marks))
    return state


def _mark(state, now, symbol="AMD", rate=551.34):
    """The backup stop's fresh eToro rate for this tick."""

    state[MARKS_KEY] = json.dumps({symbol: {"rate": rate, "at": now.isoformat()}})


def _alert(
    tmp_path, *, state=None, notifier=None, client=None, bars=None, signals=None, **settings
):
    values = dict(
        etoro_live_mirror_enabled=True, etoro_live_acknowledgement=LIVE_OPERATOR_ACKNOWLEDGEMENT
    )
    values.update(settings)
    logs = _Logs()
    mirror = EtoroLiveMirrorService(
        settings=make_settings(tmp_path, **values),
        client=client if client is not None else _Client(),
        runtime_state=state if state is not None else _state(),
        run_logs=logs,
        notifier=notifier if notifier is not None else _Notifier(),
    )
    alert = OvernightAdverseMoveAlert(
        mirror=mirror,
        bars=bars if bars is not None else _Bars(),
        stop_hits=signals if signals is not None else _Signals(),
    )
    return alert, mirror, logs


def _errors(logs):
    return [p["step"] for p in logs.named("etoro_live_overnight_alert_error")]


# -- the AMD case -------------------------------------------------------------------------
def test_amd_replay_alerts_once_with_all_three_reasons(tmp_path) -> None:
    alert, mirror, logs = _alert(tmp_path)
    assert alert.run(now=AMD_NOW) == 1
    [text] = mirror.notifier.sent
    assert text.splitlines()[0] == "eToro LIVE overnight alert: AMD (pre-market)"
    assert "- down 3.4% from the last close 570.82 (2026-10-14)" in text
    assert "- 0.42 average daily moves above the stop (ATR 23.40)" in text
    assert "- tracked signal momentum_breakout 1d hit its own stop 556.10 at 12:10 UTC" in text
    assert "bid 547.49 / ask 595.77, spread 8.4% THIN QUOTE" in text
    assert "Now 551.34 (eToro rate), last close 570.82 (-3.41%)" in text
    assert "Entry 642.00 (-14.12% vs entry) | stop 541.53 (+1.81% vs stop)" in text
    assert "12:11 UTC / 16:11 Dubai, Thu 15 Oct" in text
    assert "Alert only: nothing was closed or changed" in text
    saved = json.loads(mirror.state[ALERT_STATE_KEY])
    assert saved["dubai_date"] == "2026-10-15"
    assert set(saved["sent"]["AMD"]) == {"gap_down", "near_stop", "signal_stop"}
    assert saved["signal_seen"] == {"AMD": "2026-10-15T12:10:05+00:00"}  # newest hit alerted
    [sent] = logs.named("etoro_live_overnight_alert_sent")
    assert sent["kinds"] == ["gap_down", "near_stop", "signal_stop"]
    assert sent["bid"] == 547.49 and sent["ask"] == 595.77 and sent["signal_stops"] == 1
    assert mirror.client.requests == [
        ("GET", "/market-data/instruments/rates", {"instrumentIds": str(AMD_ID)})
    ]
    assert _errors(logs) == []


def test_at_most_one_alert_per_symbol_per_kind_per_day(tmp_path) -> None:
    state = _state()
    alert, mirror, _ = _alert(tmp_path, state=state)
    assert alert.run(now=AMD_NOW) == 1
    state.gets.clear()
    assert alert.run(now=AMD_NOW + timedelta(minutes=1)) == 0
    assert ALERT_STATE_KEY not in state.gets  # the in-process memo answers, no extra read
    # A restart (new instance) reads the stored dedupe instead.
    again, _, _ = _alert(tmp_path, state=state, notifier=mirror.notifier, client=mirror.client)
    assert again.run(now=AMD_NOW + timedelta(minutes=2)) == 0
    assert len(mirror.notifier.sent) == 1 and len(mirror.client.requests) == 1
    # The next night (16 Oct in Dubai): the 15 Oct bar is now complete (close 551.00), so no
    # gap; the signal stop was before the 15 Oct 16:00 close; only the near-stop alert is new.
    bars = _Bars(_frame(partial=551.0))
    nxt, _, _ = _alert(tmp_path, state=state, notifier=mirror.notifier, bars=bars)
    next_day = datetime.fromisoformat("2026-10-16T12:00:00+00:00")
    _mark(state, next_day)
    assert nxt.run(now=next_day) == 1
    text = mirror.notifier.sent[-1]
    assert "average daily moves above the stop" in text
    assert "from the last close" not in text.split("\n")[1] and "tracked signal" not in text
    assert set(json.loads(state[ALERT_STATE_KEY])["sent"]["AMD"]) == {"near_stop"}


# -- when it runs ---------------------------------------------------------------------------
def test_regular_hours_read_nothing_and_send_nothing(tmp_path) -> None:
    state = _state()
    alert, mirror, _ = _alert(tmp_path, state=state)
    for when in (
        "2026-10-15T13:30:00+00:00",
        "2026-10-15T16:00:00+00:00",
        "2026-10-15T19:59:00+00:00",
    ):
        assert alert.run(now=datetime.fromisoformat(when)) == 0
    assert state.gets == [] and mirror.notifier.sent == []


def test_alert_window_edges() -> None:
    def at(text):
        return datetime.fromisoformat(text)

    assert in_alert_window(at("2026-10-15T13:29:00+00:00"), crypto=False)  # 09:29 NY
    assert not in_alert_window(at("2026-10-15T13:30:00+00:00"), crypto=False)  # open
    assert not in_alert_window(at("2026-10-15T19:59:00+00:00"), crypto=False)
    assert in_alert_window(at("2026-10-15T20:00:00+00:00"), crypto=False)  # 16:00 close
    assert not in_alert_window(at("2026-10-17T15:00:00+00:00"), crypto=False)  # Saturday
    assert in_alert_window(at("2026-10-17T15:00:00+00:00"), crypto=True)
    assert not in_alert_window(at("2026-10-15T15:00:00+00:00"), crypto=True)  # weekday hours
    assert last_session_date(at("2026-10-15T12:00:00+00:00")).isoformat() == "2026-10-14"
    assert last_session_date(at("2026-10-15T20:00:00+00:00")).isoformat() == "2026-10-15"
    assert last_session_date(at("2026-10-19T12:00:00+00:00")).isoformat() == "2026-10-16"  # Mon


def test_weekend_skips_stocks_but_checks_the_crypto_test(tmp_path) -> None:
    saturday = datetime.fromisoformat("2026-10-17T10:00:00+00:00")
    state = _state(marks={"AMD": {"rate": 500.0, "at": saturday.isoformat()}})
    alert, mirror, _ = _alert(tmp_path, state=state, signals=_Signals([]))
    assert alert.run(now=saturday) == 0
    assert MARKS_KEY not in state.gets  # no stock is in the window: marks are not read
    state[TEST_STATE_KEY] = json.dumps(
        {
            "status": "open",
            "symbol": "ETH",
            "position_id": 9,
            "instrument_id": 100001,
            "open_rate": 2_698.16,
            "stop_loss": 2_581.80,
            "last_rate": 2_590.0,
            "last_rate_at": saturday.isoformat(),
        }
    )
    eth_bars = _Bars(_frame(last_close=2_700.0, atr=60.0, last_day="2026-10-16"))
    alert, mirror, _ = _alert(tmp_path, state=state, bars=eth_bars, signals=_Signals([]))
    assert alert.run(now=saturday) == 1
    assert eth_bars.calls[0][0] == "ETH/USD"
    [text] = mirror.notifier.sent
    assert text.startswith("eToro LIVE overnight alert: ETH (weekend)")
    assert "- down 4.1% from the last close 2,700.00" in text and "AMD" not in text


def test_crypto_on_a_weekday_follows_us_hours(tmp_path) -> None:
    now = datetime.fromisoformat("2026-10-15T15:00:00+00:00")  # 11:00 NY
    state = _state(cards={})
    state[TEST_STATE_KEY] = json.dumps(
        {
            "status": "open",
            "symbol": "ETH",
            "position_id": 9,
            "open_rate": 2_698.16,
            "stop_loss": 2_581.80,
            "last_rate": 2_590.0,
            "last_rate_at": now.isoformat(),
        }
    )
    alert, mirror, _ = _alert(tmp_path, state=state)
    assert alert.run(now=now) == 0 and mirror.notifier.sent == []


def test_crypto_test_order_not_yet_filled_is_not_alerted(tmp_path) -> None:
    saturday = datetime.fromisoformat("2026-10-17T10:00:00+00:00")
    state = _state(cards={})
    test_order = {
        "status": "open",
        "symbol": "ETH",
        "position_id": None,  # order sent, no position seen yet
        "open_rate": 2_698.16,
        "stop_loss": 2_581.80,
        "last_rate": 2_590.0,
        "last_rate_at": saturday.isoformat(),
    }
    state[TEST_STATE_KEY] = json.dumps(test_order)
    eth_bars = _Bars(_frame(last_close=2_700.0, atr=60.0, last_day="2026-10-16"))
    alert, mirror, _ = _alert(tmp_path, state=state, bars=eth_bars, signals=_Signals([]))
    assert alert.run(now=saturday) == 0 and mirror.notifier.sent == []
    state[TEST_STATE_KEY] = json.dumps({**test_order, "position_id": 9})
    assert alert.run(now=saturday) == 1


# -- time zones: New York summer time (EDT, UTC-4) and winter time (EST, UTC-5) ----------------
# US clocks go back on Sun 1 Nov 2026, so Mon 2 Nov is the first winter-time session; the AMD
# case (15 Oct) was summer time. In winter regular hours are 14:30-21:00 UTC, not 13:30-20:00.
# (Clocks went forward on Sun 8 Mar 2026 and go forward again on Sun 14 Mar 2027.)
def _at(text):
    return datetime.fromisoformat(text)


@pytest.mark.parametrize(
    ("when", "inside"),
    [
        ("2027-01-14T13:45:00+00:00", True),  # Thu 08:45 EST: the last pre-market hour
        ("2027-01-14T14:29:00+00:00", True),  # 09:29 EST
        ("2027-01-14T14:30:00+00:00", False),  # 09:30 EST: the open
        ("2027-01-14T20:30:00+00:00", False),  # 15:30 EST: still regular hours
        ("2027-01-14T20:59:00+00:00", False),  # 15:59 EST
        ("2027-01-14T21:00:00+00:00", True),  # 16:00 EST: the close
        ("2027-01-16T04:59:00+00:00", True),  # Fri 23:59 EST
        ("2027-01-16T05:00:00+00:00", False),  # Sat 00:00 EST: weekend, stocks are not checked
        # The switch: Fri 30 Oct 2026 is summer time, Mon 2 Nov winter time.
        ("2026-10-30T13:29:00+00:00", True),  # 09:29 EDT
        ("2026-10-30T13:30:00+00:00", False),  # 09:30 EDT: the open
        ("2026-10-30T19:59:00+00:00", False),  # 15:59 EDT
        ("2026-10-30T20:00:00+00:00", True),  # 16:00 EDT: the close
        ("2026-11-02T04:30:00+00:00", False),  # Sun 1 Nov 23:30 EST: still the weekend
        ("2026-11-02T05:00:00+00:00", True),  # Mon 00:00 EST
        ("2026-11-02T13:30:00+00:00", True),  # 08:30 EST (the open a week earlier)
        ("2026-11-02T14:29:00+00:00", True),  # 09:29 EST
        ("2026-11-02T14:30:00+00:00", False),  # 09:30 EST: the open
        ("2026-11-02T20:00:00+00:00", False),  # 15:00 EST (the close a week earlier)
        ("2026-11-02T20:59:00+00:00", False),  # 15:59 EST
        ("2026-11-02T21:00:00+00:00", True),  # 16:00 EST: the close
        # The spring switch: Fri 6 Mar 2026 is winter time, clocks go forward Sun 8 Mar.
        ("2026-03-06T14:29:00+00:00", True),  # 09:29 EST
        ("2026-03-06T14:30:00+00:00", False),  # 09:30 EST: the open
        ("2026-03-06T20:59:00+00:00", False),  # 15:59 EST
        ("2026-03-06T21:00:00+00:00", True),  # 16:00 EST: the close
        ("2026-03-09T03:30:00+00:00", False),  # Sun 8 Mar 23:30 EDT: still the weekend
        ("2026-03-09T04:00:00+00:00", True),  # Mon 00:00 EDT
        ("2026-03-09T13:29:00+00:00", True),  # 09:29 EDT
        ("2026-03-09T13:30:00+00:00", False),  # 09:30 EDT: the open (14:30 UTC a week earlier)
        ("2026-03-09T19:59:00+00:00", False),  # 15:59 EDT
        ("2026-03-09T20:00:00+00:00", True),  # 16:00 EDT: the close (21:00 UTC a week earlier)
    ],
)
def test_alert_window_follows_new_york_summer_and_winter_time(when, inside) -> None:
    assert in_alert_window(_at(when), crypto=False) is inside


def test_bar_dates_follow_winter_time() -> None:
    # Alpaca stamps a daily bar at midnight New York: 05:00 UTC in winter time. Before the
    # Tue 15 Dec open the newest completed session is Mon 14 Dec; after the close it is 15 Dec.
    frame = _frame(last_close=100.0, atr=2.0, last_day="2026-12-14", partial=90.0)
    assert pd.to_datetime(frame["timestamp"].iloc[-1]).isoformat() == "2026-12-15T05:00:00+00:00"
    pre_market = reference_from_bars(frame, crypto=False, now=_at("2026-12-15T13:11:00+00:00"))
    assert pre_market["prev_close"] == 100.0 and pre_market["bar_date"] == "2026-12-14"
    still_open = reference_from_bars(frame, crypto=False, now=_at("2026-12-15T20:59:00+00:00"))
    assert still_open["bar_date"] == "2026-12-14"  # 15:59 EST: 15 Dec has not closed
    closed = reference_from_bars(frame, crypto=False, now=_at("2026-12-15T21:00:00+00:00"))
    assert closed["prev_close"] == 90.0 and closed["bar_date"] == "2026-12-15"


def test_amd_style_replay_in_winter_time(tmp_path) -> None:
    now = _at("2026-12-15T13:11:00+00:00")  # Tue 08:11 EST, as AMD's 08:11 EDT on 15 Oct
    state = _state(marks={"AMD": {"rate": 551.34, "at": "2026-12-15T13:10:30+00:00"}})
    bars = _Bars(_frame(last_day="2026-12-14", partial=600.0))
    signals = _Signals([_signal(closed_at="2026-12-15T13:10:05+00:00")])
    alert, mirror, logs = _alert(tmp_path, state=state, bars=bars, signals=signals)
    assert alert.run(now=now) == 1
    [text] = mirror.notifier.sent
    assert text.splitlines()[0] == "eToro LIVE overnight alert: AMD (pre-market)"
    assert "- down 3.4% from the last close 570.82 (2026-12-14)" in text
    assert "- 0.42 average daily moves above the stop (ATR 23.40)" in text
    assert "- tracked signal momentum_breakout 1d hit its own stop 556.10 at 13:10 UTC" in text
    assert "13:11 UTC / 17:11 Dubai, Tue 15 Dec" in text
    assert _errors(logs) == []


def test_last_session_and_signal_window_follow_winter_time() -> None:
    def session(text):
        return last_session_date(_at(text)).isoformat()

    def start(text):
        return signal_window_start(_at(text)).astimezone(UTC).isoformat()

    assert session("2027-01-14T20:59:00+00:00") == "2027-01-13"  # 15:59 EST: not closed yet
    assert session("2027-01-14T21:00:00+00:00") == "2027-01-14"  # 16:00 EST close
    assert session("2026-11-02T20:30:00+00:00") == "2026-10-30"  # Mon 15:30 EST: Friday's
    assert start("2027-01-14T21:30:00+00:00") == "2027-01-14T21:00:00+00:00"  # the EST close
    assert start("2027-01-15T12:00:00+00:00") == "2027-01-14T21:00:00+00:00"  # next morning
    # Mon 2 Nov, the first EST morning: the window began at Friday's 16:00 EDT close.
    assert start("2026-11-02T12:00:00+00:00") == "2026-10-30T20:00:00+00:00"
    assert start("2026-11-02T21:00:00+00:00") == "2026-11-02T21:00:00+00:00"  # 16:00 EST


@pytest.mark.parametrize(
    ("day", "prev"), [("2026-11-02", "2026-10-30"), ("2027-01-14", "2027-01-13")]
)
def test_winter_time_alerts_before_the_open_and_reads_nothing_in_regular_hours(
    tmp_path, day, prev
) -> None:
    regular = _at(f"{day}T20:30:00+00:00")  # 15:30 EST (16:30 in summer time)
    state = _state(marks={"AMD": {"rate": 551.34, "at": regular.isoformat()}})
    bars = _Bars(_frame(last_day=prev, partial=600.0))
    alert, mirror, _ = _alert(tmp_path, state=state, bars=bars, signals=_Signals([]))
    assert alert.run(now=regular) == 0
    assert state.gets == [] and bars.calls == [] and mirror.notifier.sent == []
    pre_market = _at(f"{day}T14:00:00+00:00")  # 09:00 EST (10:00 in summer time)
    state = _state(marks={"AMD": {"rate": 551.34, "at": pre_market.isoformat()}})
    alert, mirror, _ = _alert(tmp_path, state=state, bars=bars, signals=_Signals([]))
    assert alert.run(now=pre_market) == 1
    [text] = mirror.notifier.sent
    assert text.splitlines()[0] == "eToro LIVE overnight alert: AMD (pre-market)"
    # The unfinished bar of ``day`` (Alpaca stamps it 05:00 UTC) is not the previous close.
    assert f"- down 3.4% from the last close 570.82 ({prev})" in text
    assert "14:00 UTC / 18:00 Dubai" in text
    assert json.loads(state[ALERT_STATE_KEY])["dubai_date"] == day  # 18:00 Dubai, same date


# -- one alert per kind per day: the day is the operator's Dubai date ---------------------------
# A weekday off-hours window (16:00 New York to the next 09:30) is 00:00-17:30 Dubai in summer
# time and 01:00-18:30 Dubai in winter time, so neither UTC nor New York midnight splits it.
def _window_minutes(close_utc, open_utc):
    now, end = _at(close_utc), _at(open_utc)
    while now < end:
        yield now
        now += timedelta(minutes=1)


@pytest.mark.parametrize(
    ("close_utc", "open_utc", "day"),
    [
        ("2026-10-14T20:00:00+00:00", "2026-10-15T13:30:00+00:00", "2026-10-15"),  # EDT
        ("2027-01-14T21:00:00+00:00", "2027-01-15T14:30:00+00:00", "2027-01-15"),  # EST
        ("2026-03-09T20:00:00+00:00", "2026-03-10T13:30:00+00:00", "2026-03-10"),  # first EDT
        ("2026-11-02T21:00:00+00:00", "2026-11-03T14:30:00+00:00", "2026-11-03"),  # first EST
    ],
)
def test_a_weekday_off_hours_window_is_one_dubai_date(close_utc, open_utc, day) -> None:
    minutes = list(_window_minutes(close_utc, open_utc))
    assert all(in_alert_window(m, crypto=False) for m in minutes)
    assert not in_alert_window(_at(close_utc) - timedelta(minutes=1), crypto=False)
    assert not in_alert_window(_at(open_utc), crypto=False)
    assert {alert_day(m).isoformat() for m in minutes} == {day}
    assert len({m.astimezone(overnight.NEW_YORK).date() for m in minutes}) == 2  # NY splits it


def test_the_weekend_gives_friday_evening_and_monday_morning_their_own_day() -> None:
    friday = [_at("2026-10-30T20:00:00+00:00"), _at("2026-10-31T03:59:00+00:00")]  # EDT
    monday = [_at("2026-11-02T05:00:00+00:00"), _at("2026-11-02T14:29:00+00:00")]  # EST
    assert {alert_day(m).isoformat() for m in friday} == {"2026-10-31"}  # Saturday in Dubai
    assert {alert_day(m).isoformat() for m in monday} == {"2026-11-02"}


@pytest.mark.parametrize(
    ("close_utc", "open_utc", "session"),
    [
        ("2026-10-14T20:00:00+00:00", "2026-10-15T13:30:00+00:00", "2026-10-14"),  # EDT
        ("2027-01-14T21:00:00+00:00", "2027-01-15T14:30:00+00:00", "2027-01-14"),  # EST
    ],
)
def test_a_gap_that_lasts_all_night_is_alerted_once(tmp_path, close_utc, open_utc, session) -> None:
    # 550 is 3.6% under the 570.82 close and 10 (0.43 ATR) above the 540 stop: both kinds,
    # all night. Checked every minute from the close to the open, across UTC and New York
    # midnight: one message.
    state = _state(cards={"et1": _card(stop=540.0, broker_stop=540.0)})
    bars = _Bars(_frame(last_day=session))
    signals = _Signals(fail=True)  # a lasting failure: one run-log row a day, not two
    alert, mirror, logs = _alert(tmp_path, state=state, bars=bars, signals=signals)
    sent = 0
    for minute in _window_minutes(close_utc, open_utc):
        _mark(state, minute, rate=550.0)
        sent += alert.run(now=minute)
    assert sent == 1 and len(mirror.notifier.sent) == 1
    assert len(signals.calls) > 200 and _errors(logs) == ["tracked_signals"]
    text = mirror.notifier.sent[0]
    assert "- down 3.6% from the last close 570.82" in text
    assert "- 0.43 average daily moves above the stop" in text
    assert text.splitlines()[0].endswith("AMD (after hours)")


@pytest.mark.parametrize(
    ("evening", "after_midnight", "session", "day"),
    [
        # 17:00 EDT Wed 14 Oct, then 00:05 EDT Thu 15 Oct (UTC midnight is in between too).
        ("2026-10-14T21:00:00+00:00", "2026-10-15T04:05:00+00:00", "2026-10-14", "2026-10-15"),
        # 17:00 EST Thu 14 Jan, then 00:05 EST Fri 15 Jan.
        ("2027-01-14T22:00:00+00:00", "2027-01-15T05:05:00+00:00", "2027-01-14", "2027-01-15"),
    ],
)
def test_no_resend_after_new_york_midnight_in_process_or_after_a_restart(
    tmp_path, evening, after_midnight, session, day
) -> None:
    evening, after_midnight = _at(evening), _at(after_midnight)
    state = _state(cards={"et1": _card(stop=400.0, broker_stop=400.0)})
    bars = _Bars(_frame(last_day=session))
    _mark(state, evening, rate=550.0)
    alert, mirror, logs = _alert(tmp_path, state=state, bars=bars, signals=_Signals([]))
    assert alert.run(now=evening) == 1
    saved = json.loads(state[ALERT_STATE_KEY])
    assert saved["dubai_date"] == day and set(saved["sent"]["AMD"]) == {"gap_down"}
    assert logs.named("etoro_live_overnight_alert_sent")[0]["dubai_date"] == day
    _mark(state, after_midnight, rate=550.0)
    assert alert.run(now=after_midnight) == 0  # the in-process memo
    restarted, _, _ = _alert(
        tmp_path,
        state=state,
        notifier=mirror.notifier,
        client=mirror.client,
        bars=bars,
        signals=_Signals([]),
    )
    assert restarted.run(now=after_midnight) == 0  # a restart: the stored dedupe
    assert len(mirror.notifier.sent) == 1
    # The next night is the next Dubai date: a gap below the next close may be alerted again.
    next_night = evening + timedelta(days=1)
    _mark(state, next_night, rate=550.0)
    restarted.bars = _Bars(_frame(last_day=next_night.date().isoformat()))
    assert restarted.run(now=next_night) == 1


def test_crypto_on_the_weekend_gets_one_alert_per_kind_per_dubai_date(tmp_path) -> None:
    test_order = {"status": "open", "symbol": "ETH", "position_id": 9, "stop_loss": 2_000.0}
    state = _state(cards={})
    eth_bars = _Bars(_frame(last_close=2_700.0, atr=60.0, last_day="2026-10-16"))
    alert, mirror, _ = _alert(tmp_path, state=state, bars=eth_bars, signals=_Signals([]))
    for when, sends in (
        ("2026-10-17T10:00:00+00:00", 1),  # Sat 14:00 Dubai
        ("2026-10-17T19:59:00+00:00", 0),  # Sat 23:59 Dubai: the same day
        ("2026-10-17T20:00:00+00:00", 1),  # Sun 00:00 Dubai: a new day
        ("2026-10-18T19:00:00+00:00", 0),  # Sun 23:00 Dubai
    ):
        state[TEST_STATE_KEY] = json.dumps({**test_order, "last_rate": 2_590, "last_rate_at": when})
        assert alert.run(now=_at(when)) == sends, when
    assert len(mirror.notifier.sent) == 2
    assert {key[0] for key in alert._done} == {"2026-10-18"}  # the memo keeps only today


# -- thresholds ------------------------------------------------------------------------------
@pytest.mark.parametrize(("drop_pct", "fires"), [(2.9, False), (3.0, True), (3.1, True)])
def test_gap_down_threshold(tmp_path, drop_pct, fires) -> None:
    rate = round(570.82 * (1 - drop_pct / 100), 4)
    state = _state(
        cards={"et1": _card(stop=400.0, broker_stop=400.0)},
        marks={"AMD": {"rate": rate, "at": AMD_NOW.isoformat()}},
    )
    alert, mirror, _ = _alert(tmp_path, state=state, signals=_Signals([]))
    assert alert.run(now=AMD_NOW) == int(fires)
    assert any("from the last close" in t for t in mirror.notifier.sent) is fires


@pytest.mark.parametrize(
    ("atrs_above", "fires"), [(1.05, False), (1.0, True), (0.95, True), (-0.1, True)]
)
def test_near_stop_threshold(tmp_path, atrs_above, fires) -> None:
    rate = 570.82  # flat vs the close: only the stop distance matters
    stop = round(rate - atrs_above * 23.4, 4)
    state = _state(
        cards={"et1": _card(stop=stop, broker_stop=stop)},
        marks={"AMD": {"rate": rate, "at": AMD_NOW.isoformat()}},
    )
    alert, mirror, _ = _alert(tmp_path, state=state, signals=_Signals([]))
    assert alert.run(now=AMD_NOW) == int(fires)
    if atrs_above < 0:
        assert "at or below the stop" in mirror.notifier.sent[0]


def test_reference_uses_completed_sessions_only() -> None:
    frame = _frame(last_close=100.0, atr=2.0, last_day="2026-10-14", partial=90.0)
    pre_market = reference_from_bars(frame, crypto=False, now=AMD_NOW)
    assert pre_market["prev_close"] == 100.0 and pre_market["bar_date"] == "2026-10-14"
    assert pre_market["atr"] == pytest.approx(2.0)
    after_close = datetime.fromisoformat("2026-10-15T20:05:00+00:00")
    assert reference_from_bars(frame, crypto=False, now=after_close)["prev_close"] == 90.0
    # Crypto: only bars that started at least a day ago are complete.
    crypto_frame = _frame(last_close=50.0, atr=1.0, last_day="2026-10-15")
    assert reference_from_bars(crypto_frame, crypto=True, now=AMD_NOW)["bar_date"] == "2026-10-14"
    assert reference_from_bars(_frame(days=5), crypto=False, now=AMD_NOW)["atr"] is None
    assert reference_from_bars(pd.DataFrame(), crypto=False, now=AMD_NOW) is None


# -- missing inputs and failures ---------------------------------------------------------------
def test_stale_rate_skips_price_reasons_but_signal_stop_still_alerts(tmp_path) -> None:
    old = (AMD_NOW - timedelta(minutes=6)).isoformat()
    state = _state(marks={"AMD": {"rate": 551.34, "at": old}})
    alert, mirror, logs = _alert(tmp_path, state=state)
    assert alert.run(now=AMD_NOW) == 1
    [text] = mirror.notifier.sent
    assert "tracked signal" in text and "from the last close" not in text
    assert "Now n/a (no fresh eToro rate)" in text and "average daily moves" not in text
    assert _errors(logs) == ["rate_unavailable:AMD"]
    assert alert.bars.calls == []  # no rate: no bar read either


def test_bar_failure_is_logged_once_and_retried_after_the_backoff(tmp_path) -> None:
    bars = _Bars(fail=True)
    state = _state()
    alert, mirror, logs = _alert(tmp_path, bars=bars, state=state)
    assert alert.run(now=AMD_NOW) == 1  # the tracked-signal reason still goes out
    assert "tracked signal" in mirror.notifier.sent[0]
    assert "from the last close" not in mirror.notifier.sent[0]
    later = AMD_NOW + timedelta(minutes=5)
    _mark(state, later)
    assert alert.run(now=later) == 0
    assert len(bars.calls) == 1
    bars.fail = False
    later = AMD_NOW + timedelta(minutes=16)
    _mark(state, later)
    assert alert.run(now=later) == 1  # retried: now the price reasons
    assert "from the last close" in mirror.notifier.sent[-1]
    assert len(bars.calls) == 2
    assert _errors(logs) == ["bars:AMD"]


def test_bar_reads_are_bounded_per_tick_and_cached(tmp_path) -> None:
    symbols = ["AMD", "MSFT", "SPY", "COST", "XOM"]
    cards = {f"et{i}": _card(symbol=s, instrument_id=None) for i, s in enumerate(symbols)}
    marks = {s: {"rate": 900.0, "at": AMD_NOW.isoformat()} for s in symbols}
    bars = _Bars()
    alert, _, _ = _alert(tmp_path, state=_state(cards=cards, marks=marks), bars=bars)
    alert.run(now=AMD_NOW)
    assert len(bars.calls) == overnight.MAX_BAR_READS_PER_TICK
    alert.run(now=AMD_NOW + timedelta(minutes=1))
    assert len(bars.calls) == len(symbols)
    alert.run(now=AMD_NOW + timedelta(minutes=2))
    assert len(bars.calls) == len(symbols)  # one read per symbol per session


def test_send_failures_are_retried_at_most_three_times(tmp_path) -> None:
    notifier = _Notifier(ok=False)
    state = _state()
    alert, _, logs = _alert(tmp_path, state=state, notifier=notifier)
    for minute in range(6):
        assert alert.run(now=AMD_NOW + timedelta(minutes=minute)) == 0
    assert len(notifier.sent) == MAX_SEND_ATTEMPTS == 3
    assert len(logs.named("etoro_live_overnight_alert_send_failed")) == MAX_SEND_ATTEMPTS
    saved = json.loads(state[ALERT_STATE_KEY])
    assert saved["sent"] == {} and saved["failed"]["AMD:gap_down"] == MAX_SEND_ATTEMPTS


def test_a_raising_notifier_is_swallowed_and_logged_once_a_day(tmp_path) -> None:
    state = _state()
    alert, _, logs = _alert(tmp_path, state=state, notifier=_Notifier(raises=True))
    for minute in range(MAX_SEND_ATTEMPTS):
        later = AMD_NOW + timedelta(minutes=minute)
        _mark(state, later)
        assert alert.run(now=later) == 0
    assert _errors(logs) == ["send:AMD"]  # one run-log row per step per day, not per tick


def test_state_write_failure_does_not_cause_a_resend(tmp_path) -> None:
    state = _state(fail_set={ALERT_STATE_KEY})
    alert, mirror, logs = _alert(tmp_path, state=state)
    assert alert.run(now=AMD_NOW) == 1
    assert alert.run(now=AMD_NOW + timedelta(minutes=1)) == 0
    assert len(mirror.notifier.sent) == 1 and "state_write" in _errors(logs)


def test_unreadable_dedupe_state_skips_the_tick(tmp_path) -> None:
    state = _state(fail_get={ALERT_STATE_KEY})
    alert, mirror, logs = _alert(tmp_path, state=state)
    assert alert.run(now=AMD_NOW) == 0
    assert mirror.notifier.sent == [] and _errors(logs) == ["run"]


def test_off_switch_disabled_telegram_or_no_client_read_nothing(tmp_path) -> None:
    for kwargs in (
        {"etoro_live_overnight_alert_enabled": False},
        {"notifier": _Notifier(enabled=False)},
    ):
        state = _state()
        alert, mirror, _ = _alert(tmp_path, state=state, **kwargs)
        assert alert.run(now=AMD_NOW) == 0
        assert state.gets == [] and mirror.notifier.sent == []
    state = _state()
    alert, mirror, _ = _alert(tmp_path, state=state)
    mirror.client = None
    assert alert.run(now=AMD_NOW) == 0 and state.gets == []
    mirror.notifier = None
    assert alert.run(now=AMD_NOW) == 0


def test_setting_defaults_on(tmp_path) -> None:
    assert make_settings(tmp_path).etoro_live_overnight_alert_enabled is True


# -- the bid/ask read ---------------------------------------------------------------------------
def test_cooldown_skips_the_bid_ask_read(tmp_path) -> None:
    client = _Client()
    etoro_rate_limit.mark_etoro_rate_limited(client.settings, status_code=429, body="")
    alert, mirror, logs = _alert(tmp_path, client=client)
    assert alert.run(now=AMD_NOW) == 1
    assert client.requests == [] and "bid/ask n/a" in mirror.notifier.sent[0]
    assert "quotes_cooldown" in _errors(logs)


def test_a_429_on_the_bid_ask_read_never_blinds_the_backup_stop(tmp_path, monkeypatch) -> None:
    """A real live client over a mock transport (no network): eToro 429s the alert's rates
    read. The LIVE key's cooldown must stay shut so the next backup stop still reads."""

    paths = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/market-data/instruments/rates"):
            return httpx.Response(429, text="Too Many Requests")
        if request.url.path.endswith("/trading/info/portfolio"):
            position = {"positionID": 7, "instrumentID": AMD_ID, "amount": 500.0}
            return httpx.Response(
                200, json={"clientPortfolio": {"credit": 500.0, "positions": [position]}}
            )
        if request.url.path.endswith("/market-data/search"):
            return httpx.Response(
                200, json={"items": [{"internalSymbolFull": "AMD", "currentRate": 551.34}]}
            )
        return httpx.Response(404, text="not mocked")

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda *a, **k: real_client(*a, transport=httpx.MockTransport(handler), **k),
    )
    client = build_live_client(
        make_settings(
            tmp_path,
            etoro_live_api_key="live-api-key-fake",
            etoro_live_user_key="live-user-key-fake",
            etoro_request_min_interval_seconds=0.0,
            etoro_base_url="https://etoro.mock.invalid",  # served by the mock transport only
        )
    )
    state = _state()
    stop = {
        "stop": 541.53,
        "instrument_id": AMD_ID,
        "recorded_at": AMD_NOW.isoformat(),
        "seen": True,
    }
    dict.__setitem__(state, STOPS_KEY, json.dumps({"AMD": stop}))
    alert, mirror, logs = _alert(tmp_path, state=state, client=client)
    assert alert.run(now=AMD_NOW) == 1
    assert paths == ["/api/v1/market-data/instruments/rates"]
    assert "bid/ask n/a" in mirror.notifier.sent[0] and "quotes_rate_limited" in _errors(logs)
    assert etoro_rate_limit.etoro_cooldown_remaining(client.settings) == 0
    paths.clear()
    assert check_backup_stops(mirror) == []  # 551.34 is above the stop: nothing closes
    assert paths == ["/api/v1/trading/info/portfolio", "/api/v1/market-data/search"]
    paths.clear()  # no more bid/ask reads this Dubai day
    later, day = AMD_NOW + timedelta(minutes=1), alert_day(AMD_NOW).isoformat()
    assert alert._quotes([{"instrument_id": AMD_ID}], later, day) == {} and paths == []
    # Every other caller keeps the cooldown on a 429.
    with pytest.raises(etoro_rate_limit.EToroRateLimitError):
        client._request("GET", "/market-data/instruments/rates", params={"instrumentIds": "1"})
    assert etoro_rate_limit.etoro_cooldown_remaining(client.settings) > 250


def test_bid_ask_read_failure_still_alerts(tmp_path) -> None:
    alert, mirror, logs = _alert(tmp_path, client=_Client(fail=True))
    assert alert.run(now=AMD_NOW) == 1
    assert "bid/ask n/a" in mirror.notifier.sent[0] and "quotes" in _errors(logs)


def test_bid_ask_reads_are_capped_per_day(tmp_path) -> None:
    client = _Client()
    alert, mirror, _ = _alert(tmp_path, client=client)
    for minute in range(MAX_QUOTE_READS_PER_DAY + 3):
        alert._quotes([{"instrument_id": AMD_ID}], AMD_NOW + timedelta(minutes=minute), "d")
    assert len(client.requests) == MAX_QUOTE_READS_PER_DAY


def test_normal_spread_is_not_flagged_and_etoro_stop_shown_when_different(tmp_path) -> None:
    state = _state(cards={"et1": _card(broker_stop=513.6)})
    alert, mirror, _ = _alert(tmp_path, state=state, client=_Client({AMD_ID: (551.0, 551.6)}))
    assert alert.run(now=AMD_NOW) == 1
    text = mirror.notifier.sent[0]
    assert "spread 0.1%" in text and "THIN" not in text
    assert "eToro's own stop 513.60" in text


# -- tracked signals ----------------------------------------------------------------------------
def test_tracked_signals_are_read_at_most_every_five_minutes(tmp_path) -> None:
    signals = _Signals([])
    alert, _, _ = _alert(tmp_path, signals=signals)
    for minute in (0, 1, 4):
        alert.run(now=AMD_NOW + timedelta(minutes=minute))
    # Only the live symbols, only since the 14 Oct 16:00 close (20:00 UTC) that began the
    # window: no midnight floor, so a hit late on 14 Oct New York time still counts.
    assert signals.calls == [(["AMD"], "2026-10-14T20:00:00+00:00")]
    alert.run(now=AMD_NOW + timedelta(minutes=5))
    assert len(signals.calls) == 2


@pytest.mark.parametrize(
    "record",
    [
        _signal(role="entry_short"),  # a short's stop is above: not adverse for the long
        _signal(symbol="NVDA"),
        _signal(closed_at="2026-10-14T19:59:00+00:00"),  # before the 14 Oct 16:00 close
    ],
)
def test_signal_stop_needs_a_long_signal_on_the_symbol_since_the_close(tmp_path, record) -> None:
    state = _state(cards={"et1": _card(stop=400.0, broker_stop=400.0)}, marks={})
    alert, mirror, _ = _alert(tmp_path, state=state, signals=_Signals([record]))
    assert alert.run(now=AMD_NOW) == 0 and mirror.notifier.sent == []


def test_a_signal_stop_at_the_window_start_counts(tmp_path) -> None:
    state = _state(cards={"et1": _card(stop=400.0, broker_stop=400.0)}, marks={})
    record = _signal(closed_at="2026-10-14T20:00:00+00:00")  # the 14 Oct 16:00 NY close
    alert, mirror, _ = _alert(tmp_path, state=state, signals=_Signals([record]))
    assert alert.run(now=AMD_NOW) == 1
    assert "at 20:00 UTC Wed 14 Oct (its quote" in mirror.notifier.sent[0]  # not today's UTC date


def test_tracked_signal_read_failure_is_logged_and_price_reasons_still_work(tmp_path) -> None:
    alert, mirror, logs = _alert(tmp_path, signals=_Signals(fail=True))
    assert alert.run(now=AMD_NOW) == 1
    assert "tracked signal" not in mirror.notifier.sent[0]
    assert "from the last close" in mirror.notifier.sent[0]
    assert _errors(logs) == ["tracked_signals"]


# -- no trading change ----------------------------------------------------------------------------
def _proposal():
    order = TradeOrder(
        symbol="MSFT",
        amount_usd=12_500,
        leverage=1,
        proposed_price=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        strategy_name="momentum_breakout",
        side="buy",
        metadata={"timeframe": "1d"},
    )
    return SimpleNamespace(id="prop_MSFT", order=order, signal=None)


def test_alert_changes_no_trading_state_and_no_copy_decision(tmp_path) -> None:
    state = _state()
    verdict = {"passed": True, "oos_expectancy_r": 0.05}
    state[VERDICTS_KEY] = json.dumps({"verdicts": {"momentum_breakout:1d": verdict}})
    state[STATE_KEY] = json.dumps({"last_equity": 10_000.0, "last_cash": 9_000.0})
    alert, mirror, _ = _alert(tmp_path, state=state)
    before_blockers = mirror.blockers(_proposal(), "alpaca")
    before_room = room_verdict(mirror, _proposal().order)
    before = dict(state)
    assert alert.run(now=AMD_NOW) == 1  # the fake client raises on any order/close/portfolio
    changed = {k for k in set(before) | set(state) if before.get(k) != state.get(k)}
    assert changed == {ALERT_STATE_KEY}
    assert mirror.blockers(_proposal(), "alpaca") == before_blockers
    assert room_verdict(mirror, _proposal().order) == before_room
    assert not ROOM_REASONS & set(before_blockers)


# -- guard wiring ---------------------------------------------------------------------------------
def test_attach_wires_the_alert_and_the_guard_runs_it(tmp_path) -> None:
    alert, mirror, logs = _alert(tmp_path, state=_state(cards={}))
    bars, repo = _Bars(), TrackedSignalRepository(_db(tmp_path))
    coordinator = SimpleNamespace(etoro_live_mirror=mirror)
    attach_live_guard(
        coordinator,
        settings=SimpleNamespace(etoro_live_guard_interval_seconds=60),
        bars=bars,
        tracked_signals=repo,
    )
    wired = mirror.overnight_alert
    assert isinstance(wired, OvernightAdverseMoveAlert)
    assert wired.bars is bars and wired.mirror is mirror
    assert wired.stop_hits(["AMD"], "2026-10-14T20:00:00+00:00") == []  # reads the repo's db
    attach_live_guard(coordinator, settings=SimpleNamespace(), bars=None)
    assert mirror.overnight_alert.stop_hits is None and mirror.overnight_alert.bars is None
    runs = []
    mirror.overnight_alert = SimpleNamespace(run=lambda: runs.append("run") or 1)
    # Started on its own thread, not awaited: the guard step reports nothing itself.
    assert "etoro_live_overnight_alert" not in run_live_checks(mirror, None)
    mirror.overnight_alert.worker.join(timeout=5)
    assert runs == ["run"]


def test_a_hung_telegram_send_never_delays_the_next_backup_stop(tmp_path, monkeypatch) -> None:
    from app.broker import etoro_live_guard

    monkeypatch.setattr(overnight, "utc_now", lambda: AMD_NOW)
    monkeypatch.setattr(etoro_live_guard, "maybe_send_daily_report", lambda m: False)
    monkeypatch.setattr(etoro_live_guard, "update_scorecard", lambda m, closes: 0)
    passes = []
    monkeypatch.setattr(etoro_live_guard, "check_backup_stops", lambda m: passes.append(m) or [])
    in_send, release = threading.Event(), threading.Event()

    class _HungNotifier(_Notifier):
        def send_text(self, text):
            in_send.set()
            release.wait(timeout=5)  # Telegram hanging until curl's --max-time
            return super().send_text(text)

    alert, mirror, _ = _alert(tmp_path, notifier=_HungNotifier())
    mirror.overnight_alert = alert
    guard = EtoroLiveGuard(mirror=mirror, tester=None)
    try:
        guard.tick()
        assert in_send.wait(timeout=5)  # the send is in flight on the alert's own thread
        first = alert.worker
        guard.tick()  # the next backup-stop pass runs at once; no second alert run starts
        assert len(passes) == 2 and alert.worker is first and first.is_alive()
        assert mirror.notifier.sent == []  # the send still has not finished
    finally:
        release.set()
    first.join(timeout=5)
    assert not first.is_alive() and len(mirror.notifier.sent) == 1


def test_a_broken_alert_never_stops_the_other_guard_steps(tmp_path, monkeypatch) -> None:
    from app.broker import etoro_live_guard

    _, mirror, logs = _alert(tmp_path, state=_state(cards={}))
    reports = []
    monkeypatch.setattr(
        etoro_live_guard, "maybe_send_daily_report", lambda m: reports.append(m) or False
    )

    class _NoThread:
        def __init__(self, **_):
            raise RuntimeError("can't start new thread")

    monkeypatch.setattr(etoro_live_guard, "Thread", _NoThread)
    mirror.overnight_alert = SimpleNamespace(run=lambda: 1)
    run_live_checks(mirror, None)
    assert reports == [mirror]
    assert [p["step"] for p in logs.named("etoro_live_guard_error")] == [
        "etoro_live_overnight_alert"
    ]
    # The real alert swallows its own failures: a broken scorecard read is logged, not raised.
    alert, _, logs = _alert(tmp_path, state=_state(fail_get={SCORECARD_KEY}))
    assert alert.run(now=AMD_NOW) == 0 and _errors(logs) == ["run"]


# -- the tracked-signal store (real SQLite) --------------------------------------------------------
def _db(tmp_path):
    db = Database(make_settings(tmp_path))
    db.initialize()
    return db


def _stored_signal(repo, *, symbol, strategy, status, closed_at, role="entry_long"):
    snapshot = _snapshot().model_copy(
        update={
            "symbol": symbol,
            "strategy_name": strategy,
            "signal_role": role,
            "stop_loss": 556.1,
            "current_bid": 547.49,
            "current_ask": 595.77,
        }
    )
    record = repo.upsert_open(snapshot, origin="swing_scan")
    if status != "open":
        repo.close(record.id, status=status, last_price=551.34, snapshot=snapshot)
        with repo.db.connect() as connection:
            connection.execute(
                "UPDATE tracked_signals SET closed_at = ? WHERE id = ?", (closed_at, record.id)
            )
    return record


def test_recent_stop_hits_reads_only_stop_hits_on_the_symbols_since_the_close(tmp_path) -> None:
    repo = TrackedSignalRepository(_db(tmp_path))
    since = "2026-10-14T20:00:00+00:00"
    hit = "2026-10-15T12:10:05.123456+00:00"
    _stored_signal(repo, symbol="AMD", strategy="a", status="stop_hit", closed_at=hit)
    _stored_signal(repo, symbol="AMD", strategy="b", status="target_hit", closed_at=hit)
    _stored_signal(
        repo, symbol="AMD", strategy="c", status="stop_hit", closed_at="2026-10-14T19:00:00+00:00"
    )
    _stored_signal(repo, symbol="NVDA", strategy="d", status="stop_hit", closed_at=hit)
    _stored_signal(repo, symbol="AMD", strategy="e", status="open", closed_at=None)
    later = "2026-10-15T12:20:00+00:00"
    _stored_signal(
        repo, symbol="AMD", strategy="f", status="stop_hit", closed_at=later, role="entry_short"
    )
    _stored_signal(repo, symbol="AMD", strategy="g", status="stop_hit", closed_at=since)
    rows = recent_stop_hits(repo.db, symbols=["amd"], since_iso=since)
    assert [r["strategy_name"] for r in rows] == ["f", "a", "g"]  # newest first, from ``since``
    assert rows[1]["signal_role"] == "entry_long" and rows[0]["signal_role"] == "entry_short"
    assert rows[1]["current_bid"] == 547.49 and rows[1]["current_ask"] == 595.77
    assert rows[1]["stop_loss"] == 556.1 and rows[1]["closed_at"] == hit
    assert len(recent_stop_hits(repo.db, symbols=["AMD"], since_iso=since, limit=1)) == 1
    assert recent_stop_hits(repo.db, symbols=[], since_iso=since) == []


def test_amd_replay_through_the_real_tracked_signal_store(tmp_path) -> None:
    repo = TrackedSignalRepository(_db(tmp_path))
    for strategy in ("momentum_breakout", "inside_bar", "rsi_reversal"):
        _stored_signal(
            repo,
            symbol="AMD",
            strategy=strategy,
            status="stop_hit",
            closed_at="2026-10-15T12:10:05+00:00",
        )
    alert, mirror, logs = _alert(tmp_path)
    alert.stop_hits = stop_hit_reader(repo)
    assert alert.run(now=AMD_NOW) == 1
    text = mirror.notifier.sent[0]
    assert text.count("tracked signal") == 3 and "hit its own stop 556.10 at 12:10 UTC" in text
    assert logs.named("etoro_live_overnight_alert_sent")[0]["signal_stops"] == 3


def test_signal_window_starts_at_the_close_that_began_the_window() -> None:
    def start(text):
        return signal_window_start(datetime.fromisoformat(text)).astimezone(UTC).isoformat()

    assert start("2026-10-14T21:00:00+00:00") == "2026-10-14T20:00:00+00:00"  # evening: close
    assert start("2026-10-15T12:11:00+00:00") == "2026-10-14T20:00:00+00:00"  # next morning
    assert start("2026-10-17T15:00:00+00:00") == "2026-10-16T20:00:00+00:00"  # Saturday
    assert start("2026-10-19T12:00:00+00:00") == "2026-10-16T20:00:00+00:00"  # Monday


@pytest.mark.parametrize(
    ("last_read", "hit", "after_midnight"),
    [
        # 23:55 EDT read finds nothing; the hit closes 23:57 EDT; the next read is 00:00:30.
        ("2026-10-15T03:55:00+00:00", "2026-10-15T03:57:00+00:00", "2026-10-15T04:00:30+00:00"),
        # The same in winter time: 23:55 / 23:57 / 00:00:30 EST.
        ("2027-01-15T04:55:00+00:00", "2027-01-15T04:57:00+00:00", "2027-01-15T05:00:30+00:00"),
    ],
)
def test_a_signal_stop_just_before_new_york_midnight_is_still_alerted(
    tmp_path, last_read, hit, after_midnight
) -> None:
    state = _state(cards={"et1": _card(stop=400.0, broker_stop=400.0)}, marks={})
    signals = _Signals([])
    alert, mirror, _ = _alert(tmp_path, state=state, signals=signals)
    assert alert.run(now=_at(last_read)) == 0
    signals.records = [_signal(closed_at=hit)]
    assert alert.run(now=_at(after_midnight)) == 1
    assert f"at {_at(hit):%H:%M} UTC (its quote" in mirror.notifier.sent[0]
    close = signal_window_start(_at(last_read)).astimezone(UTC).isoformat()
    assert [since for _, since in signals.calls] == [close, close]  # no midnight floor


def test_the_weekend_signal_stops_are_alerted_once_across_days(tmp_path) -> None:
    friday = _at("2026-10-16T21:00:00+00:00")  # 17:00 EDT, Sat 01:00 Dubai
    last_friday_read = _at("2026-10-17T03:55:00+00:00")  # 23:55 EDT Friday
    monday = _at("2026-10-19T04:00:00+00:00")  # 00:00 EDT, Mon 08:00 Dubai
    early = {**_signal(closed_at="2026-10-16T20:30:00+00:00"), "strategy_name": "early"}
    mid = {**_signal(closed_at="2026-10-16T20:45:00+00:00"), "strategy_name": "mid"}
    late = {**_signal(closed_at="2026-10-17T03:57:00+00:00"), "strategy_name": "late"}
    for restart in (False, True):
        state = _state(cards={"et1": _card(stop=400.0, broker_stop=400.0)})
        bars = _Bars(_frame(last_day="2026-10-16"))
        signals = _Signals([mid, early])  # newest first, like the store
        alert, mirror, _ = _alert(tmp_path, state=state, bars=bars, signals=signals)
        _mark(state, friday, rate=550.0)
        assert alert.run(now=friday) == 1
        assert "tracked signal mid" in mirror.notifier.sent[0]
        assert "tracked signal early" in mirror.notifier.sent[0]
        assert "down 3.6%" in mirror.notifier.sent[0]
        _mark(state, last_friday_read, rate=550.0)
        assert alert.run(now=last_friday_read) == 0
        signals.records = [late, mid, early]
        if restart:
            alert, _, _ = _alert(
                tmp_path,
                state=state,
                notifier=mirror.notifier,
                client=mirror.client,
                bars=bars,
                signals=signals,
            )
        _mark(state, monday, rate=550.0)
        assert alert.run(now=monday) == 1  # a new Dubai day: the gap again, and only the new hit
        text = mirror.notifier.sent[1]
        assert "tracked signal late 1d hit its own stop 556.10 at 03:57 UTC Sat 17 Oct" in text
        assert "tracked signal early" not in text and "tracked signal mid" not in text
        assert "- down 3.6% from the last close 570.82 (2026-10-16)" in text
        saved = json.loads(state[ALERT_STATE_KEY])
        assert saved["dubai_date"] == "2026-10-19"
        assert saved["signal_seen"] == {"AMD": "2026-10-17T03:57:00+00:00"}
        assert alert.run(now=monday + timedelta(minutes=10)) == 0
        assert len(mirror.notifier.sent) == 2


def test_an_already_alerted_hit_costs_no_state_read_on_a_new_day(tmp_path) -> None:
    friday, monday = _at("2026-10-16T21:00:00+00:00"), _at("2026-10-19T04:00:00+00:00")
    state = _state(cards={"et1": _card(stop=400.0, broker_stop=400.0)}, marks={})
    signals = _Signals([_signal(closed_at="2026-10-16T20:30:00+00:00")])
    alert, mirror, _ = _alert(tmp_path, state=state, signals=signals)
    assert alert.run(now=friday) == 1
    state.gets.clear()
    assert alert.run(now=monday) == 0  # the same hit, a new Dubai day: nothing new
    assert len(signals.calls) == 2 and ALERT_STATE_KEY not in state.gets
    assert len(mirror.notifier.sent) == 1


def test_a_failed_state_write_never_rolls_the_hit_mark_back(tmp_path) -> None:
    friday, monday = _at("2026-10-16T21:00:00+00:00"), _at("2026-10-19T04:00:00+00:00")
    old, new = "2026-10-16T20:10:00+00:00", "2026-10-16T20:30:00+00:00"
    state = _state(cards={"et1": _card(stop=400.0, broker_stop=400.0)}, fail_set={ALERT_STATE_KEY})
    stored = {"dubai_date": "2026-10-17", "sent": {}, "failed": {}, "signal_seen": {"AMD": old}}
    dict.__setitem__(state, ALERT_STATE_KEY, json.dumps(stored))  # another caller alerted ``old``
    signals = _Signals([_signal(closed_at=new), _signal(closed_at=old)])
    bars = _Bars(_frame(last_day="2026-10-16"))
    alert, mirror, _ = _alert(tmp_path, state=state, bars=bars, signals=signals)
    _mark(state, friday, rate=571.0)  # flat: only the tracked signal
    assert alert.run(now=friday) == 1
    [first] = mirror.notifier.sent
    assert "at 20:30 UTC" in first and "at 20:10 UTC" not in first  # ``old`` was alerted before
    for minute in (0, 5, 10):  # the write keeps failing, so runtime_state still says ``old``
        _mark(state, monday + timedelta(minutes=minute), rate=550.0)
        alert.run(now=monday + timedelta(minutes=minute))
    assert len(mirror.notifier.sent) == 2 and "tracked signal" not in mirror.notifier.sent[1]


def test_a_failed_send_keeps_the_hit_for_the_retry(tmp_path) -> None:
    state = _state(cards={"et1": _card(stop=400.0, broker_stop=400.0)}, marks={})
    notifier = _Notifier(ok=False)
    alert, _, _ = _alert(tmp_path, state=state, notifier=notifier)
    assert alert.run(now=AMD_NOW) == 0
    notifier.ok = True
    assert alert.run(now=AMD_NOW + timedelta(minutes=1)) == 1
    assert len(notifier.sent) == 2 and "tracked signal" in notifier.sent[1]
    assert json.loads(state[ALERT_STATE_KEY])["signal_seen"] == {"AMD": "2026-10-15T12:10:05+00:00"}


def test_a_hit_mark_outlives_the_longest_window(tmp_path) -> None:
    # Friday 16:00 EDT to Monday 09:30 EDT is ~2.7 days: a hit just after Friday's close must
    # still be known on Monday morning, after a restart, or it would be alerted twice.
    friday, monday = _at("2026-10-16T20:01:00+00:00"), _at("2026-10-19T13:29:00+00:00")
    state = _state(cards={"et1": _card(stop=400.0, broker_stop=400.0)}, marks={})
    signals = _Signals([_signal(closed_at="2026-10-16T20:00:30+00:00")])
    alert, mirror, _ = _alert(tmp_path, state=state, signals=signals)
    assert alert.run(now=friday) == 1
    restarted, _, _ = _alert(
        tmp_path, state=state, notifier=mirror.notifier, client=mirror.client, signals=signals
    )
    _mark(state, monday, rate=550.0)  # a Monday gap reaches the dedupe state
    assert restarted.run(now=monday) == 1
    assert len(mirror.notifier.sent) == 2 and "tracked signal" not in mirror.notifier.sent[1]
    assert overnight.SIGNAL_SEEN_DAYS == 7  # a margin over the ~2.7-day weekend window


def test_signal_marks_older_than_a_week_are_dropped(tmp_path) -> None:
    state = _state()
    stale = (AMD_NOW - timedelta(days=7, minutes=1)).isoformat()
    kept = (AMD_NOW - timedelta(days=6)).isoformat()
    old = {"dubai_date": "2026-10-09", "sent": {"AMD": {"gap_down": stale}}, "failed": {}}
    state[ALERT_STATE_KEY] = json.dumps({**old, "signal_seen": {"NVDA": stale, "MSFT": kept}})
    alert, _, _ = _alert(tmp_path, state=state)
    assert alert.run(now=AMD_NOW) == 1  # the old day's sent list does not carry over
    saved = json.loads(state[ALERT_STATE_KEY])
    assert saved["dubai_date"] == "2026-10-15"
    assert saved["signal_seen"] == {"MSFT": kept, "AMD": "2026-10-15T12:10:05+00:00"}


def test_an_evening_signal_stop_is_not_resent_after_midnight_new_york(tmp_path) -> None:
    evening = datetime.fromisoformat("2026-10-14T23:00:00+00:00")  # 19:00 NY
    state = _state(cards={"et1": _card(stop=400.0, broker_stop=400.0)}, marks={})
    signals = _Signals([_signal(closed_at="2026-10-14T22:30:00+00:00")])
    alert, mirror, _ = _alert(tmp_path, state=state, signals=signals)
    assert alert.run(now=evening) == 1 and "tracked signal" in mirror.notifier.sent[0]
    after_midnight = datetime.fromisoformat("2026-10-15T04:10:00+00:00")  # 00:10 NY
    assert alert.run(now=after_midnight) == 0 and len(mirror.notifier.sent) == 1
