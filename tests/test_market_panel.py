"""Market panel for the pre-market deep scan (operator 2026-10-18, post-mortem item 8).

Shadow only: once today's deep scan is final, one Yahoo daily history per panel symbol and a
logged-only regime label. Never a proposal, order, scan decision, ranking, alert or Telegram
message; never in the deep scan's ticks; each symbol fetched once a day. No network here: the
provider is a fake, the clocks are injected.
"""

from __future__ import annotations

import json
import math
import sys
import threading
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

import app.workflow.market_panel as panel
import app.workflow.shadow_ledger as ledger
from tests.conftest import make_settings
from tests.test_workflow_service import FakeLogs, FakeState

DAY = "2026-10-15"  # a Thursday
MORNING = datetime(2026, 10, 15, 12, 35, tzinfo=UTC)  # 08:35 EDT: after the 08:30 deep scan
SCAN_DONE = datetime(2026, 10, 15, 12, 33, tzinfo=UTC).isoformat()
KEY = panel.PANEL_KEY.format(date=DAY)
# Yahoo's daily closes, fetched 2026-10-19 (the post-mortem's episode); a morning reads the
# closes before it.
SMH = [
    ("2026-09-14", 541.5), ("2026-09-15", 542.11), ("2026-09-16", 545.56), ("2026-09-17", 560.61),
    ("2026-09-18", 573.0), ("2026-09-21", 596.03), ("2026-09-22", 607.46), ("2026-09-23", 601.41),
    ("2026-09-24", 600.52), ("2026-09-25", 606.56), ("2026-09-28", 600.01), ("2026-09-29", 606.9),
    ("2026-09-30", 609.0), ("2026-10-01", 617.81), ("2026-10-02", 630.6), ("2026-10-05", 633.9),
    ("2026-10-06", 632.5), ("2026-10-07", 625.03), ("2026-10-08", 607.27), ("2026-10-09", 603.33),
    ("2026-10-12", 590.99), ("2026-10-13", 588.12), ("2026-10-14", 564.74), ("2026-10-15", 558.84),
    ("2026-10-16", 539.79),
]  # fmt: skip
XLP = [
    ("2026-09-14", 84.42), ("2026-09-15", 83.73), ("2026-09-16", 83.33), ("2026-09-17", 83.49),
    ("2026-09-18", 82.8), ("2026-09-21", 81.92), ("2026-09-22", 82.73), ("2026-09-23", 82.43),
    ("2026-09-24", 81.7), ("2026-09-25", 82.06), ("2026-09-28", 82.28), ("2026-09-29", 81.85),
    ("2026-09-30", 80.6), ("2026-10-01", 80.33), ("2026-10-02", 80.53), ("2026-10-05", 81.04),
    ("2026-10-06", 81.8), ("2026-10-07", 81.7), ("2026-10-08", 83.42), ("2026-10-09", 83.43),
    ("2026-10-12", 84.41), ("2026-10-13", 84.19), ("2026-10-14", 86.06), ("2026-10-15", 86.68),
    ("2026-10-16", 87.62),
]  # fmt: skip
TNX = [
    ("2026-09-14", 4.961), ("2026-09-15", 4.996), ("2026-09-16", 5.006), ("2026-09-17", 4.947),
    ("2026-09-18", 4.998), ("2026-09-21", 4.963), ("2026-09-22", 4.968), ("2026-09-23", 5.114),
    ("2026-09-24", 5.162), ("2026-09-25", 5.184), ("2026-09-28", 5.24), ("2026-09-29", 5.255),
    ("2026-09-30", 5.293), ("2026-10-01", 5.237), ("2026-10-02", 5.277), ("2026-10-05", 5.311),
    ("2026-10-06", 5.269), ("2026-10-07", 5.277), ("2026-10-08", 5.231), ("2026-10-09", 5.244),
    ("2026-10-12", 5.24), ("2026-10-13", 5.217), ("2026-10-14", 5.19), ("2026-10-15", 5.089),
    ("2026-10-16", 5.098),
]  # fmt: skip
# The 15 Oct morning: Asia's sessions of that date (Kospi -7.6%, Nikkei -4.9%; the reader takes
# that close from Yahoo's quote, the daily bar has none yet) and a stand-in 08:35 futures and
# oil print (Yahoo's daily rows have only the close).
ROWS = {
    "^KS11": [("2026-10-14", 6008.81), ("2026-10-15", 5554.22)],
    "^N225": [("2026-10-14", 67744.289), ("2026-10-15", 64425.949)],
    "NQ=F": [("2026-10-14", 30182.75), ("2026-10-15", 29870.0)],
    "^TNX": TNX,
    "CL=F": [("2026-10-14", 95.09), ("2026-10-15", 96.2)],
    "SMH": SMH,
    "XLP": XLP,
}


class _State(FakeState):
    """A runtime state that counts its round trips like the repository's."""

    def __init__(self):
        super().__init__()
        self.reads: list = []
        self.fail_set = False

    def get(self, key):
        self.reads.append(key)
        return super().get(key)

    def get_many(self, keys):
        self.reads.append(tuple(keys))
        return {key: self.values.get(key) for key in keys}

    def set(self, key, value):
        if self.fail_set:
            raise RuntimeError("database down")
        super().set(key, value)


class _Provider:
    def __init__(self, env, rows=None):
        self.env, self.rows, self.calls = env, dict(ROWS if rows is None else rows), []

    def __call__(self, symbol):
        self.calls.append(symbol)
        self.env.mono["t"] += self.env.seconds_per_fetch
        value = self.rows.get(symbol)
        if isinstance(value, Exception):
            raise value
        return list(value or [])


def _env(tmp_path, monkeypatch, *, rows=None, scan_done=SCAN_DONE, **overrides):
    now = {"t": MORNING}
    monkeypatch.setattr(panel, "utc_now", lambda: now["t"])
    values = dict(premarket_market_panel_enabled=True, premarket_deep_scan_enabled=True)
    values.update(overrides)
    state = _State()
    if scan_done:
        state.values[panel.SCAN_DONE_KEY] = scan_done
    # Only what the panel may touch: no screener, market data, proposals, orders or notifier.
    service = SimpleNamespace(
        settings=make_settings(tmp_path, **values), runtime_state=state, run_logs=FakeLogs()
    )
    env = SimpleNamespace(now=now, mono={"t": 1_000.0}, seconds_per_fetch=0.0, service=service)
    env.provider = _Provider(env, rows)

    def run(*, started_at=None, deferred=()):
        start = env.mono["t"] if started_at is None else started_at
        return panel.run_market_panel(
            service,
            started_at=start,
            deferred=deferred,
            clock=lambda: env.mono["t"],
            provider=env.provider,
        )

    env.run = run
    env.record = lambda: json.loads(state.values.get(KEY) or "null")
    return env


def _morning_panel(morning: str) -> dict:
    today = date.fromisoformat(morning)
    symbols = {
        symbol: {"status": "ok", "reading": panel.read_rows(panel.SYMBOLS[symbol], rows, today)}
        for symbol, rows in (("SMH", SMH), ("XLP", XLP), ("^TNX", TNX))
    }
    return panel.build_panel(symbols)


# --------------------------------------------------------------------------- the readings


def test_a_session_reading_is_the_latest_row_against_the_one_before() -> None:
    rows = [("2026-10-13", 100.0), ("2026-10-15", 90.0), ("2026-10-14", 80.0)]
    reading = panel.read_rows("session", rows, date(2026, 10, 15))
    assert reading == {
        "as_of": "2026-10-15",
        "current": True,
        "last": 90.0,
        "prior_close": 80.0,
        "prior_date": "2026-10-14",
        "change_pct": 12.5,
    }
    # a holiday in Seoul: the last session is an older one, not today's
    older = panel.read_rows("session", rows[:1] + rows[2:], date(2026, 10, 15))
    assert older["as_of"] == "2026-10-14" and older["current"] is False
    # a date listed twice keeps its last row; a row after today is never read
    twice = [("2026-10-14", 80.0), ("2026-10-15", 1.0), ("2026-10-15", 88.0), ("2026-10-16", 5.0)]
    assert panel.read_rows("session", twice, date(2026, 10, 15))["last"] == 88.0
    for bad in ([], [("2026-10-15", 90.0)], [("2026-10-14", float("nan")), ("2026-10-15", 1.0)]):
        with pytest.raises(ValueError):
            panel.read_rows("session", bad, date(2026, 10, 15))


def test_the_yield_reads_completed_sessions_and_shows_today_apart() -> None:
    reading = panel.read_rows("yield", TNX, date(2026, 10, 15))
    # the 14 Oct close against 13 Oct and 7 Oct; 15 Oct's row is today's, never in the level
    assert reading == {
        "as_of": "2026-10-14",
        "level_pct": 5.19,
        "change_1d_bp": -2.7,
        "change_5d_bp": -8.7,
        "today_level_pct": 5.089,
    }
    assert "today_level_pct" not in panel.read_rows("yield", TNX[:-2], date(2026, 10, 15))
    tenfold = [(day, value * 10) for day, value in TNX]  # an index quoted at 10x the yield
    assert panel.read_rows("yield", tenfold, date(2026, 10, 15))["change_5d_bp"] == -8.7
    assert panel.read_rows("yield", TNX[:3], date(2026, 9, 17))["change_5d_bp"] is None
    with pytest.raises(ValueError):
        panel.read_rows("yield", TNX[:1], date(2026, 9, 15))


def test_a_fund_reads_completed_sessions_only() -> None:
    reading = panel.read_rows("fund", SMH, date(2026, 10, 15))
    assert reading["as_of"] == "2026-10-14" and reading["close"] == 564.74
    assert reading["return_5d_pct"] == round((564.74 / 625.03 - 1) * 100, 2)  # from 7 Oct
    assert reading["return_20d_pct"] == round((564.74 / 545.56 - 1) * 100, 2)  # from 16 Sep
    short = panel.read_rows("fund", SMH[:8], date(2026, 9, 25))
    assert short["return_20d_pct"] is None and short["return_5d_pct"] is not None
    with pytest.raises(ValueError):
        panel.read_rows("fund", SMH[:6], date(2026, 9, 21))  # five completed sessions only


# --------------------------------------------------------------------------- the label


@pytest.mark.parametrize(
    "morning, label",
    [
        ("2026-09-29", "rates_shock"),  # the run to the 24-year high: +27.7 bp
        ("2026-10-06", "neutral"),
        ("2026-10-07", "neutral"),  # AMD's entry day: nothing here would have flagged it
        ("2026-10-08", "neutral"),
        ("2026-10-09", "chips_weak"),  # -5.56 pp, the 10y flat
        ("2026-10-12", "chips_weak"),
        ("2026-10-13", "risk_off"),
        ("2026-10-14", "risk_off"),  # MSFT and SPY were copied that day
        ("2026-10-15", "risk_off"),
        ("2026-10-16", "risk_off"),
    ],
)
def test_the_episode_mornings_get_their_labels(morning, label) -> None:
    regime = panel.regime_label(_morning_panel(morning))
    assert regime["label"] == label and regime["risk_off"] is (label == "risk_off")


def test_the_rule_the_10y_rose_and_chips_trail_never_fired_in_the_episode() -> None:
    for morning in ("2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-12",
                    "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16"):  # fmt: skip
        inputs = panel.regime_label(_morning_panel(morning))["inputs"]
        rose = inputs["us10y_change_5d_bp"] > 0
        assert not (rose and inputs["chips_vs_staples_5d_pp"] <= panel.CHIPS_LAG_PP), morning


def _regime(rates, chips, *, kospi=None, nikkei=None, futures=None, current=True):
    def session(change):
        return None if change is None else {"change_pct": change, "current": current}

    return panel.regime_label(
        {
            "us_10y_yield": None if rates is None else {"change_5d_bp": rates},
            "chips_vs_staples": {"relative_5d_pp": chips},
            "asia": {"kospi": session(kospi), "nikkei": session(nikkei)},
            "nasdaq_100_futures": session(futures),
        }
    )


def test_the_thresholds_are_inclusive_and_rates_shock_comes_first() -> None:
    assert _regime(-5.0, -5.0)["label"] == "risk_off"
    assert _regime(-4.9, -5.0)["label"] == "chips_weak"
    assert _regime(-5.0, -4.99)["label"] == "neutral"
    assert _regime(15.0, 3.0)["label"] == "rates_shock"
    assert _regime(14.9, 3.0)["label"] == "neutral"
    assert _regime(15.0, -9.0)["label"] == "rates_shock"  # the yield rising: never risk_off
    assert _regime(15.0, None)["label"] == "rates_shock"  # decided without the chips
    assert _regime(14.9, None)["label"] == "unknown"
    assert _regime(None, -9.0)["label"] == "unknown"
    components = _regime(-7.1, -10.9)["components"]
    assert components == {
        "rates_shock": False,
        "bonds_bid": True,
        "chips_lag": True,
        "asia_selloff": None,
        "futures_down": None,
    }
    assert _regime(None, None)["components"]["bonds_bid"] is None


def test_overnight_components_beside_the_label_read_only_todays_sessions() -> None:
    crash = _regime(-8.7, -15.0, kospi=-7.57, nikkei=-4.9, futures=-1.04)
    assert crash["label"] == "risk_off"
    assert crash["components"]["asia_selloff"] is True
    assert crash["components"]["futures_down"] is True
    calm = _regime(1.0, 1.0, kospi=-1.99, nikkei=0.5, futures=-0.99)
    assert calm["components"]["asia_selloff"] is False
    assert calm["components"]["futures_down"] is False
    assert _regime(1.0, 1.0, nikkei=-2.0)["components"]["asia_selloff"] is True
    assert _regime(1.0, 1.0, futures=-1.0)["components"]["futures_down"] is True
    stale = _regime(1.0, 1.0, kospi=-7.0, futures=-3.0, current=False)  # yesterday's sessions
    assert stale["components"]["asia_selloff"] is None
    assert stale["components"]["futures_down"] is None
    assert stale["label"] == "neutral"  # never in the label either way


# --------------------------------------------------------------------------- the run


def test_once_the_deep_scan_is_final_it_records_the_panel_once_a_day(tmp_path, monkeypatch) -> None:
    env = _env(tmp_path, monkeypatch)
    state = env.service.runtime_state
    before = dict(state.values)
    summary = env.run()
    assert summary["label"] == "risk_off" and summary["status"] == "complete"
    assert summary["shadow_only"] is True and summary["missing"] == []
    assert env.provider.calls == list(panel.SYMBOLS)  # one fetch per symbol
    record = env.record()
    assert record["session_date"] == DAY and record["source"] == "yfinance"
    view = record["panel"]
    assert view["asia"]["kospi"]["change_pct"] == -7.57 and view["asia"]["kospi"]["current"]
    assert view["asia"]["nikkei"]["change_pct"] == -4.9
    assert view["nasdaq_100_futures"]["change_pct"] == -1.04
    assert view["us_10y_yield"]["level_pct"] == 5.19
    assert view["us_10y_yield"]["change_5d_bp"] == -8.7
    assert view["wti_oil"]["last"] == 96.2 and view["wti_oil"]["change_pct"] == 1.17
    chips = view["chips_vs_staples"]
    assert chips["relative_5d_pp"] == -14.99 and chips["relative_20d_pp"] == 0.24
    assert record["economic_events"] is None and record["economic_events_note"]
    assert record["regime"]["components"]["asia_selloff"] is True
    assert record["regime"]["components"]["futures_down"] is True
    # it writes only its own key and one run log
    assert {k for k in state.values if state.values[k] != before.get(k)} == {KEY}
    assert env.service.run_logs.items == [(panel.EVENT, summary)]
    assert state.reads == [(KEY, panel.SCAN_DONE_KEY)]  # one round trip before the fetches
    # done for the day: no fetch, no write and no database read on the later ticks
    assert env.run() is None and env.run() is None
    assert len(env.provider.calls) == len(panel.SYMBOLS) and len(state.reads) == 1


def test_a_restart_reads_the_finished_panel_and_never_fetches_again(tmp_path, monkeypatch) -> None:
    env = _env(tmp_path, monkeypatch)
    env.run()
    for attribute in ("_market_panel_done", "_market_panel_symbols"):
        delattr(env.service, attribute)  # a new process
    calls = len(env.provider.calls)
    assert env.run() is None and len(env.provider.calls) == calls
    assert env.run() is None and len(env.service.runtime_state.reads) == 2  # then from memory


@pytest.mark.parametrize(
    "scan_done",
    [
        None,
        datetime(2026, 10, 14, 12, 40, tzinfo=UTC).isoformat(),  # yesterday's scan
        "not a time",
        "2026-10-15T12:33:00",  # no time zone: only a hand edit writes one
    ],
)
def test_it_waits_for_todays_deep_scan(tmp_path, monkeypatch, scan_done) -> None:
    env = _env(tmp_path, monkeypatch, scan_done=scan_done)
    assert env.run() is None
    assert env.provider.calls == [] and env.record() is None


def test_the_deep_scans_ticks_are_never_shared(tmp_path, monkeypatch) -> None:
    env = _env(tmp_path, monkeypatch)
    job = env.service.settings.scheduler_job_timeout_seconds
    # The deep scan ran until the scan stop: less than the margin is left, no read, no fetch.
    used = job - 60.0 - panel.MIN_START_SECONDS + 0.1
    assert env.run(started_at=env.mono["t"] - used) is None
    assert env.service.runtime_state.reads == [] and env.provider.calls == []
    assert env.run(deferred=["swing_hourly"]) is None  # a deferred bucket waits for nothing
    assert env.service.runtime_state.reads == []
    assert env.run(started_at=env.mono["t"] - used + 0.2)["status"] == "complete"


@pytest.mark.parametrize(
    "moment, overrides",
    [
        (datetime(2026, 10, 15, 12, 29, tzinfo=UTC), {}),  # 08:29: before the deep scan's time
        (datetime(2026, 10, 15, 13, 29, 31, tzinfo=UTC), {}),  # 09:29:31: 30 s would pass 09:30
        (datetime(2026, 10, 15, 13, 5, tzinfo=UTC), {"market_open_scan_time_local": "09:05"}),
        (datetime(2026, 10, 17, 12, 35, tzinfo=UTC), {}),  # Saturday
        (MORNING, {"premarket_market_panel_enabled": False}),
        (MORNING, {"premarket_deep_scan_enabled": False}),
    ],
)
def test_it_runs_only_in_its_window_and_with_both_switches(
    tmp_path, monkeypatch, moment, overrides
) -> None:
    env = _env(tmp_path, monkeypatch, **overrides)
    env.now["t"] = moment
    env.service.runtime_state.values[panel.SCAN_DONE_KEY] = moment.isoformat()
    assert env.run() is None
    assert env.service.runtime_state.reads == [] and env.provider.calls == []


def test_the_last_start_of_the_window(tmp_path, monkeypatch) -> None:
    env = _env(tmp_path, monkeypatch)
    env.now["t"] = datetime(2026, 10, 15, 13, 29, 30, tzinfo=UTC)  # 09:29:30 + 30 s = 09:30
    env.service.runtime_state.values[panel.SCAN_DONE_KEY] = SCAN_DONE
    assert env.run()["status"] == "complete"


def test_a_tick_stops_at_its_budget_and_the_next_one_resumes(tmp_path, monkeypatch) -> None:
    env = _env(tmp_path, monkeypatch)
    env.seconds_per_fetch = 9.0  # each fetch leaves less than a full timeout after three
    first = env.run()
    assert first == {"status": "partial", "fetched": 3}
    record = env.record()
    assert record["status"] == "partial" and record["not_fetched"] == list(panel.SYMBOLS)[3:]
    assert env.service.run_logs.items == []
    env.run()
    third = env.run()
    assert third["status"] == "complete" and third["ticks"] == 3
    assert env.provider.calls == list(panel.SYMBOLS)  # nothing fetched twice


def test_a_missing_symbol_is_recorded_and_never_fetched_again(tmp_path, monkeypatch) -> None:
    rows = dict(ROWS, SMH=RuntimeError("Too Many Requests"), XLP=[])
    env = _env(tmp_path, monkeypatch, rows=rows)
    summary = env.run()
    assert summary["status"] == "complete_with_gaps" and summary["missing"] == ["SMH", "XLP"]
    assert summary["label"] == "unknown"  # no chips-vs-staples reading: the 10y fell 8.7 bp
    symbols = env.record()["symbols"]
    assert symbols["SMH"]["error"] == "RuntimeError: Too Many Requests"
    assert symbols["XLP"]["error"] == "ValueError: no_data"
    assert env.record()["panel"]["chips_vs_staples"]["relative_5d_pp"] is None
    assert env.run() is None and env.provider.calls.count("SMH") == 1


def test_a_fetch_that_overruns_its_timeout_is_a_missing_symbol(tmp_path, monkeypatch) -> None:
    release = threading.Event()
    env = _env(tmp_path, monkeypatch)
    fake = env.provider

    def stalled(symbol):
        if symbol == "^N225":
            release.wait(5.0)  # a stalled socket: never answers before the timeout
        return fake(symbol)

    monkeypatch.setattr(panel, "FETCH_TIMEOUT_SECONDS", 0.05)
    env.provider = stalled
    try:
        summary = panel.run_market_panel(
            env.service, started_at=env.mono["t"], clock=lambda: env.mono["t"], provider=stalled
        )
    finally:
        release.set()
    assert summary["missing"] == ["^N225"]
    assert env.record()["symbols"]["^N225"]["error"].startswith("TimeoutError")


def test_a_failure_never_raises_and_is_logged_once_a_day(tmp_path, monkeypatch) -> None:
    env = _env(tmp_path, monkeypatch)
    env.service.runtime_state.fail_set = True
    assert env.run() is None and env.run() is None
    errors = [e for e, _ in env.service.run_logs.items]
    assert errors == ["premarket_market_panel_error"]
    assert env.provider.calls == list(panel.SYMBOLS)  # a failed save never costs a refetch
    env.service.runtime_state.fail_set = False
    assert env.run()["status"] == "complete" and env.provider.calls == list(panel.SYMBOLS)


def test_an_unreadable_record_starts_over(tmp_path, monkeypatch) -> None:
    env = _env(tmp_path, monkeypatch)
    partial = {"schema_version": 1, "session_date": DAY, "status": "partial"}
    edited = [
        "{not json",
        json.dumps({**partial, "symbols": ["x"]}),
        json.dumps({**partial, "symbols": {"SMH": "an edited value"}}),
        json.dumps({**partial, "schema_version": 2, "symbols": {"SMH": {"status": "ok"}}}),
    ]
    for stored in edited:
        env.service.runtime_state.values[KEY] = stored
        for attribute in ("_market_panel_done", "_market_panel_symbols"):
            if hasattr(env.service, attribute):
                delattr(env.service, attribute)
        assert env.run()["status"] == "complete"


def test_yesterdays_record_is_not_today_s(tmp_path, monkeypatch) -> None:
    env = _env(tmp_path, monkeypatch)
    env.run()
    env.now["t"] = MORNING + timedelta(days=1)  # Friday
    env.service.runtime_state.values[panel.SCAN_DONE_KEY] = (
        MORNING + timedelta(days=1)
    ).isoformat()
    summary = env.run()
    assert summary["session_date"] == "2026-10-16" and len(env.provider.calls) == 14
    assert json.loads(env.service.runtime_state.values[KEY])["status"] == "complete"  # kept


def test_the_yahoo_reader_keeps_the_exchanges_own_dates(monkeypatch) -> None:
    import pandas as pd

    calls = []

    class _Ticker:
        def __init__(self, symbol):
            self.symbol = symbol

        def history(self, **kwargs):
            calls.append((self.symbol, kwargs))
            index = pd.DatetimeIndex(["2026-10-14", "2026-10-15"]).tz_localize("Asia/Seoul")
            return pd.DataFrame({"Close": [6008.81, 5554.22]}, index=index)

    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(Ticker=_Ticker))
    rows = panel.yahoo_daily_closes("^KS11")
    # 15 Oct in Seoul is 14 Oct 15:00 UTC: the session date is Seoul's
    assert rows == [("2026-10-14", 6008.81), ("2026-10-15", 5554.22)]
    kwargs = {"period": "3mo", "interval": "1d", "auto_adjust": True, "keepna": True}
    assert calls == [("^KS11", kwargs)]  # keepna: a bar without a close is still seen

    class _Empty(_Ticker):
        def history(self, **kwargs):
            return pd.DataFrame()

    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(Ticker=_Empty))
    assert panel.yahoo_daily_closes("SMH") == []


def _yahoo(monkeypatch, zone, bars, meta):
    """A fake yfinance whose history() returns ``bars`` as yfinance 0.2.66 shapes Yahoo's chart
    (auto_adjust leaves a bar without a close all-NaN but its volume) and keeps the response's
    metadata where yfinance does; ``meta`` None: no metadata there."""

    import pandas as pd

    class _Ticker:
        def __init__(self, symbol):
            self.symbol = symbol

        def history(self, **kwargs):
            if meta is not None:
                self._price_history = SimpleNamespace(_history_metadata=dict(meta))
            nan = float("nan")
            index = pd.DatetimeIndex([day for day, _ in bars]).tz_localize(zone)
            closes = [nan if close is None else close for _, close in bars]
            opens = [nan if close is None else close for _, close in bars]
            return pd.DataFrame({"Open": opens, "Close": closes, "Volume": 292781}, index=index)

    monkeypatch.setitem(sys.modules, "yfinance", SimpleNamespace(Ticker=_Ticker))


def _epoch(*moment) -> int:
    return int(datetime(*moment, tzinfo=UTC).timestamp())


# Kospi on the 15 Oct morning: Seoul closed at 15:30 KST (06:30 UTC), its bar has no close.
KOSPI_BARS = [("2026-10-13", 6035.35), ("2026-10-14", 6008.81), ("2026-10-15", None)]
KOSPI_QUOTE = {"regularMarketPrice": 5554.22, "regularMarketTime": _epoch(2026, 10, 15, 6, 30)}


def test_todays_asian_close_comes_from_the_quote_when_the_bar_has_none(monkeypatch) -> None:
    today = date(2026, 10, 15)
    _yahoo(monkeypatch, "Asia/Seoul", KOSPI_BARS, KOSPI_QUOTE)
    rows = panel.yahoo_daily_closes("^KS11")
    assert rows[-1] == ("2026-10-15", 5554.22) and len(rows) == 3
    reading = panel.read_rows("session", rows, today)
    assert reading["as_of"] == "2026-10-15" and reading["current"] is True
    assert reading["change_pct"] == -7.57  # not 14 Oct's -0.44% read as if Seoul were shut
    # Nikkei's bar, which reports no volume, is dropped by yfinance: the quote's session is added
    bars = [("2026-10-13", 67777.523), ("2026-10-14", 67744.289)]
    quote = {"regularMarketPrice": 64425.949, "regularMarketTime": _epoch(2026, 10, 15, 6, 45)}
    _yahoo(monkeypatch, "Asia/Tokyo", bars, quote)
    rows = panel.yahoo_daily_closes("^N225")
    assert rows[-1] == ("2026-10-15", 64425.949)
    nikkei = panel.read_rows("session", rows, today)
    assert nikkei["change_pct"] == -4.9 and nikkei["current"] is True
    symbols = {"^KS11": reading, "^N225": nikkei}
    view = panel.build_panel({s: {"status": "ok", "reading": r} for s, r in symbols.items()})
    assert panel.regime_label(view)["components"]["asia_selloff"] is True


def test_the_quote_is_dated_in_the_exchanges_zone_and_never_replaces_a_close(monkeypatch) -> None:
    # 14 Oct 16:00 UTC is 15 Oct 01:00 in Seoul: the quote belongs to the 15th's bar
    quote = dict(KOSPI_QUOTE, regularMarketTime=_epoch(2026, 10, 14, 16, 0))
    _yahoo(monkeypatch, "Asia/Seoul", KOSPI_BARS, quote)
    assert panel.yahoo_daily_closes("^KS11")[-1] == ("2026-10-15", 5554.22)
    # yfinance's formatted metadata carries an aware time instead of epoch seconds
    aware = dict(KOSPI_QUOTE, regularMarketTime=datetime(2026, 10, 15, 6, 30, tzinfo=UTC))
    _yahoo(monkeypatch, "Asia/Seoul", KOSPI_BARS, aware)
    assert panel.yahoo_daily_closes("^KS11")[-1] == ("2026-10-15", 5554.22)
    # a bar with its close keeps it (the futures, the 10y, oil, the funds)
    bars = [("2026-10-14", 30182.75), ("2026-10-15", 29870.0)]
    live = {"regularMarketPrice": 29900.0, "regularMarketTime": _epoch(2026, 10, 15, 12, 30)}
    _yahoo(monkeypatch, "America/New_York", bars, live)
    assert panel.yahoo_daily_closes("NQ=F") == bars
    # Seoul shut today: the quote is the last session's, which has its close; nothing added
    older = dict(
        KOSPI_QUOTE, regularMarketPrice=6000.0, regularMarketTime=_epoch(2026, 10, 14, 6, 30)
    )
    _yahoo(monkeypatch, "Asia/Seoul", KOSPI_BARS[:2], older)
    assert panel.yahoo_daily_closes("^KS11") == [("2026-10-13", 6035.35), ("2026-10-14", 6008.81)]


@pytest.mark.parametrize(
    "meta",
    [
        None,  # no metadata where yfinance keeps it
        dict(KOSPI_QUOTE, regularMarketTime=_epoch(2026, 10, 14, 6, 30)),  # the session before
        dict(KOSPI_QUOTE, regularMarketPrice=None),
        dict(KOSPI_QUOTE, regularMarketPrice=0.0),
        dict(KOSPI_QUOTE, regularMarketTime=None),
        dict(KOSPI_QUOTE, regularMarketTime="soon"),
        dict(KOSPI_QUOTE, regularMarketTime=datetime(2026, 10, 15, 6, 30)),  # no time zone
    ],
)
def test_without_a_quote_of_that_session_today_is_missing_not_the_day_before(
    monkeypatch, meta
) -> None:
    _yahoo(monkeypatch, "Asia/Seoul", KOSPI_BARS, meta)
    rows = panel.yahoo_daily_closes("^KS11")
    assert rows[-1][0] == "2026-10-15" and math.isnan(rows[-1][1])
    with pytest.raises(ValueError, match="today_close_unavailable"):
        panel.read_rows("session", rows, date(2026, 10, 15))


def test_a_close_missing_today_only_matters_to_a_session_reading() -> None:
    nan = float("nan")
    today = date(2026, 10, 15)
    for bad in ([("2026-10-14", 1.0), ("2026-10-15", nan)], [("2026-10-15", None)]):
        with pytest.raises(ValueError, match="today_close_unavailable"):
            panel.read_rows("session", bad, today)
    # yesterday's empty bar is skipped as before; the 10y and the funds read completed sessions
    rows = [("2026-10-12", 2.0), ("2026-10-13", nan), ("2026-10-14", 1.0)]
    assert panel.read_rows("session", rows, date(2026, 10, 14))["prior_date"] == "2026-10-12"
    level = panel.read_rows("yield", TNX[:-2] + [("2026-10-15", nan)], today)
    assert level["as_of"] == "2026-10-14" and "today_level_pct" not in level
    fund = panel.read_rows("fund", SMH[:-2] + [("2026-10-15", nan)], today)
    assert fund["as_of"] == "2026-10-14"


def test_a_session_without_todays_close_is_a_missing_symbol(tmp_path, monkeypatch) -> None:
    rows = dict(ROWS, **{"^KS11": [("2026-10-14", 6008.81), ("2026-10-15", float("nan"))]})
    env = _env(tmp_path, monkeypatch, rows=rows)
    summary = env.run()
    assert summary["status"] == "complete_with_gaps" and summary["missing"] == ["^KS11"]
    record = env.record()
    assert record["symbols"]["^KS11"]["error"] == "ValueError: today_close_unavailable"
    assert record["panel"]["asia"]["kospi"] is None
    assert summary["components"]["asia_selloff"] is True  # Nikkei's -4.9% still read


# --------------------------------------------------------------------------- the wiring


def test_the_cadence_tick_runs_it_after_the_buckets(tmp_path, monkeypatch) -> None:
    import app.workflow.service as workflow_service
    from app.live_signal_schema import MarketQuote
    from tests.test_workflow_service import (
        FakeAlertHistory,
        FakeMarketDataEngine,
        FakeMarketScreener,
        FakeNotifier,
        FakeTrackedSignals,
    )

    order: list[str] = []
    monkeypatch.setattr(panel, "utc_now", lambda: MORNING)
    monkeypatch.setattr(
        panel, "yahoo_daily_closes", lambda symbol: order.append(symbol) or list(ROWS[symbol])
    )
    state = FakeState()
    state.set(panel.SCAN_DONE_KEY, SCAN_DONE)
    settings = make_settings(
        tmp_path,
        screener_scheduler_enabled=True,
        premarket_deep_scan_enabled=True,
        premarket_market_panel_enabled=True,
        premarket_open_shadow_enabled=False,
    )
    flow = workflow_service.SignalWorkflowService(
        settings=settings,
        market_screener=FakeMarketScreener([]),
        market_data_engine=FakeMarketDataEngine(MarketQuote(symbol="NVDA", last_execution=1.0)),
        notifier=FakeNotifier(),
        tracked_signals=FakeTrackedSignals(),
        alert_history=FakeAlertHistory(),
        runtime_state=state,
        run_logs=FakeLogs(),
    )
    monkeypatch.setattr(flow, "_bucket_due", lambda name: name == "crypto_rotation")
    monkeypatch.setattr(
        flow,
        "run_bucket",
        lambda name, **kw: order.append(name) or SimpleNamespace(status="ok", alerts_sent=0),
    )
    flow.run_scheduled_tasks()
    assert order == ["crypto_rotation", *panel.SYMBOLS]
    assert json.loads(state.get(KEY))["status"] == "complete"


def test_the_open_shadow_hook_passes_its_budget_on(tmp_path, monkeypatch) -> None:
    import app.workflow.open_shadow as shadow

    seen = []
    monkeypatch.setattr(panel, "run_market_panel", lambda service, **kw: seen.append(kw))
    service = SimpleNamespace(settings=make_settings(tmp_path), run_logs=FakeLogs())
    clock = lambda: 5.0  # noqa: E731
    assert shadow.run_open_shadow(service, started_at=1.0, deferred=["x"], clock=clock) is None
    assert seen == [{"started_at": 1.0, "deferred": ["x"], "clock": clock}]


def test_the_switch_defaults_on_and_is_in_the_boot_snapshot(tmp_path) -> None:
    from app.automation.policy_snapshot import effective_execution_policy
    from app.runtime_settings import AppSettings

    assert AppSettings.model_fields["premarket_market_panel_enabled"].default is True
    on = make_settings(tmp_path, premarket_market_panel_enabled=True)
    assert effective_execution_policy(on)["premarket_market_panel_enabled"] is True
    off = make_settings(tmp_path)  # the tests' default: off
    assert effective_execution_policy(off)["premarket_market_panel_enabled"] is False


# --------------------------------------------------------------------------- the ledger


def test_the_tag_a_setup_keeps_never_raises() -> None:
    regime = {"label": "risk_off", "inputs": {"a": 1}, "components": {"b": True}}
    good = {"schema_version": 1, "status": "complete", "regime": regime}
    assert panel.regime_tag(json.dumps(good)) == {
        "label": "risk_off",
        "panel_status": "complete",
        "inputs": {"a": 1},
        "components": {"b": True},
    }
    assert panel.regime_tag(json.dumps({**good, "schema_version": 2})) == {"label": "no_panel"}
    for raw in (None, "", "{broken", "[1]", json.dumps({"schema_version": 1, "regime": [1]})):
        assert panel.regime_tag(raw) == {"label": "no_panel"}
    odd = {**good, "regime": {"label": ["x"], "inputs": [1, 2], "components": "yes"}}
    assert panel.regime_tag(json.dumps(odd)) == {
        "label": "unknown",
        "panel_status": "complete",
        "inputs": {},
        "components": {},
    }


def _ledger_service(tmp_path, monkeypatch, state):
    monkeypatch.setattr(ledger, "utc_now", lambda: datetime(2026, 10, 16, 14, 0, tzinfo=UTC))
    return SimpleNamespace(
        settings=make_settings(tmp_path, premarket_deep_scan_enabled=True),
        market_data=SimpleNamespace(get_history=lambda *a, **k: None),
        market_screener=SimpleNamespace(_bars_for_timeframe=lambda timeframe: 400),
        runtime_state=state,
        run_logs=FakeLogs(),
    )


def _signal(symbol, bar):
    return {
        "symbol": symbol,
        "spec_key": "trend_following:1d",
        "kind": "strict",
        "signal_bar_ts": f"{bar} 04:00:00+00:00",
        "entry": 100.0,
        "stop": 95.0,
        "target": 110.0,
        "tradeable": True,
    }


def test_each_ledger_setup_keeps_its_mornings_label(tmp_path, monkeypatch) -> None:
    env = _env(tmp_path, monkeypatch)
    env.run()  # 15 Oct's panel: risk_off
    state = env.service.runtime_state
    for day, symbol in (("2026-10-14", "AAA"), ("2026-10-15", "BBB"), ("2026-10-16", "CCC")):
        bar = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
        watchlist = {"status": "complete", "signals": [_signal(symbol, bar)]}
        state.values[f"premarket:watchlist:{day}"] = json.dumps(watchlist)
    state.values[panel.PANEL_KEY.format(date="2026-10-16")] = "{broken"
    state.values[ledger.STATE_KEY] = json.dumps({"ingested_through": "2026-10-13"})
    state.reads.clear()
    service = _ledger_service(tmp_path, monkeypatch, state)
    assert ledger.refresh_shadow_ledger(service)["added"] == 3
    entries = json.loads(state.values[ledger.LEDGER_KEY])["entries"]
    tags = {e["symbol"]: e["market_regime"] for e in entries.values()}
    assert tags["AAA"] == {"label": "no_panel"}
    assert tags["CCC"] == {"label": "no_panel"}  # an unreadable panel never stops the ledger
    assert tags["BBB"]["label"] == "risk_off" and tags["BBB"]["panel_status"] == "complete"
    assert tags["BBB"]["inputs"] == {"us10y_change_5d_bp": -8.7, "chips_vs_staples_5d_pp": -14.99}
    assert tags["BBB"]["components"]["bonds_bid"] is True
    # one round trip per watchlist day, the panel with it: no extra read on the ledger's run
    days = ("2026-10-14", "2026-10-15", "2026-10-16")
    assert [r for r in state.reads if isinstance(r, tuple)] == [
        (f"premarket:watchlist:{d}", panel.PANEL_KEY.format(date=d)) for d in days
    ]
    assert not any(str(r).startswith(("premarket:watchlist", "premarket:market_panel"))
                   for r in state.reads if isinstance(r, str))  # fmt: skip


def test_the_label_never_changes_the_ledgers_scoring(tmp_path, monkeypatch) -> None:
    watchlist = {"status": "complete", "signals": [_signal("AAA", "2026-10-14")]}
    outcomes = []
    for with_panel in (False, True):
        env = _env(tmp_path / str(with_panel), monkeypatch)
        if with_panel:
            env.run()
        state = env.service.runtime_state
        state.values["premarket:watchlist:2026-10-15"] = json.dumps(watchlist)
        state.values[ledger.STATE_KEY] = json.dumps({"ingested_through": "2026-10-14"})
        ledger.refresh_shadow_ledger(_ledger_service(tmp_path, monkeypatch, state))
        entry = next(iter(json.loads(state.values[ledger.LEDGER_KEY])["entries"].values()))
        outcomes.append({k: v for k, v in entry.items() if k != "market_regime"})
        assert entry["market_regime"]["label"] == ("risk_off" if with_panel else "no_panel")
    assert outcomes[0] == outcomes[1]


def test_the_report_groups_the_ledger_by_label(tmp_path, monkeypatch) -> None:
    env = _env(tmp_path, monkeypatch)
    env.run()
    state = env.service.runtime_state
    closed = {"status": "closed", "r": 1.0}
    lost = {"status": "closed", "r": -1.0}
    entries = {
        "a": {"status": "closed", "stop_target": lost, "time_14d": lost,
              "market_regime": {"label": "risk_off"}},
        "b": {"status": "closed", "stop_target": closed, "time_14d": closed,
              "market_regime": {"label": "neutral"}},
        "c": {"status": "closed", "stop_target": closed, "time_14d": closed},
        "d": {"status": "pending", "market_regime": {"label": "risk_off"}},
        "e": {"status": "closed", "stop_target": closed, "time_14d": closed,
              "market_regime": {"label": "something_else"}},
    }  # fmt: skip
    state.values[ledger.LEDGER_KEY] = json.dumps({"entries": entries})
    state.values[panel.PANEL_KEY.format(date="2026-10-09")] = json.dumps(
        {"schema_version": 1, "session_date": "2026-10-09", "status": "partial", "regime": {}}
    )
    state.reads.clear()
    report = panel.market_panel_report(state)
    assert len(state.reads) == 2  # the panels in one round trip, then the ledger
    assert report["latest"]["session_date"] == DAY and report["shadow_only"] is True
    assert [r["session_date"] for r in report["recent"]] == [DAY, "2026-10-09"]
    assert report["recent"][0]["label"] == "risk_off" and report["recent"][1]["label"] == "unknown"
    groups = report["shadow_ledger_by_regime"]
    assert set(groups) == {*panel.LABELS, "no_panel", "not_recorded"}
    assert groups["risk_off"]["taken"] == 1 and groups["risk_off"]["stop_target"]["mean_r"] == -1.0
    assert groups["neutral"]["taken"] == 1 and groups["not_recorded"]["taken"] == 1
    assert groups["unknown"]["taken"] == 1 and groups["no_panel"]["taken"] == 0
    assert report["economic_events"] == panel.EVENTS_NOTE
    empty = panel.market_panel_report(FakeState())
    assert empty["latest"] is None and empty["recent"] == []


def test_the_scorecard_endpoint_carries_the_panel(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.conftest import MockBroker

    app = create_app(
        make_settings(tmp_path, control_api_token="secret"),
        broker=MockBroker(),
        enable_background_jobs=False,
    )
    body = (
        TestClient(app)
        .get("/performance/live-scorecard", headers={"X-Control-Token": "secret"})
        .json()
    )
    section = body["market_panel"]
    assert section["shadow_only"] is True and section["rule"] == panel.REGIME_RULE
    assert set(section["shadow_ledger_by_regime"]) == {*panel.LABELS, "no_panel", "not_recorded"}
