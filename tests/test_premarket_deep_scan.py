"""Shadow pre-market deep scan (operator-approved 2026-10-10, phase 1).

Every allowed equity x every daily strategy on the last COMPLETED bar, ranked into a
watchlist; never trades, never writes scan decisions, alerts or proposals.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

import app.workflow.premarket_deep_scan as deep
from app.backtesting.strategy_selection import strategy_specs_for
from app.broker.etoro_rate_limit import EToroRateLimitError
from app.performance.strategy_evidence import VERDICTS_KEY
from app.strategies import get_strategy
from tests.conftest import make_settings
from tests.test_workflow_service import FakeLogs, FakeState

NY = ZoneInfo("America/New_York")
MONDAY_0835_NY = datetime(2026, 10, 12, 12, 35, tzinfo=UTC)  # 08:35 EDT
SYMBOLS = [f"S{i:02d}" for i in range(25)]
STATE_KEY = "workflow:last_premarket_scan_at"
DAILY_SPECS = 19  # every daily strategy except pairs_stat_arb


def _completed(seed: int, n: int = 400, *, stamp: str = "04:00") -> pd.DataFrame:
    """Daily bars stamped at ``stamp`` UTC (Alpaca: 04:00, New York midnight); the last bar
    is Friday 2026-10-09."""

    rng = np.random.default_rng(seed)
    close = 100 * np.exp(np.cumsum(rng.normal(0.0008, 0.015, n)))
    high = close * (1 + np.abs(rng.normal(0, 0.006, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.006, n)))
    open_ = np.r_[close[0], close[:-1]] * (1 + rng.normal(0, 0.003, n))
    volume = rng.integers(2_000_000, 6_000_000, n).astype(float)
    return pd.DataFrame(
        {
            "timestamp": pd.date_range(end=f"2026-10-09 {stamp}", periods=n, freq="B", tz="UTC"),
            "open": open_,
            "high": np.maximum.reduce([high, open_, close]),
            "low": np.minimum.reduce([low, open_, close]),
            "close": close,
            "volume": volume,
        }
    )


def _frame(seed: int, *, stamp: str = "04:00") -> pd.DataFrame:
    """The completed bars plus Monday's forming bar: a 15% gap down that must be ignored."""

    completed = _completed(seed, stamp=stamp)
    close = float(completed["close"].iloc[-1]) * 0.85
    forming = {
        "timestamp": pd.Timestamp(f"2026-10-12 {stamp}", tz="UTC"),
        "open": close,
        "high": close * 1.01,
        "low": close * 0.99,
        "close": close,
        "volume": 9_000_000.0,
    }
    return pd.concat([completed, pd.DataFrame([forming])], ignore_index=True)


def _direct_buys(frame: pd.DataFrame, specs, build=lambda spec: get_strategy(spec.name)) -> dict:
    out = {}
    for spec in specs:
        signal = build(spec).generate_signal(frame.copy(), "S00")
        if (
            signal is not None
            and str(getattr(signal.action, "value", signal.action)) == "buy"
            and signal.stop_loss
            and signal.take_profit
            and signal.stop_loss < signal.price < signal.take_profit
        ):
            out[spec.name] = signal
    return out


class _Screener:
    def __init__(self, specs):
        self.specs = list(specs)

    def _strategy_specs_for_timeframe(self, timeframe, strategy_spec_keys=None):
        return [spec for spec in self.specs if spec.timeframe == timeframe]

    def _build_strategy(self, spec):
        return get_strategy(spec.name)

    @staticmethod
    def _bars_for_timeframe(timeframe):
        return 400

    def scan_universe(self, **kwargs):  # the old rotating premarket scan must not run
        raise AssertionError("scan_universe called")


class _MarketData:
    """``fail``: symbols that never have data (dead tickers); ``down_from``: the feed goes
    down for every symbol once this one is requested, until ``down`` is cleared."""

    def __init__(
        self, *, fail=(), rate_limited=(), stale=(), down_from=None, down_empty=False, stamp="04:00"
    ):
        self.calls: list[str] = []
        self.fail, self.rate_limited, self.stale = set(fail), set(rate_limited), set(stale)
        self.down_from, self.down, self.down_empty, self.stamp = down_from, False, down_empty, stamp

    def get_history(self, symbol, *, timeframe="1d", bars=250, force_refresh=False):
        self.calls.append(symbol)
        if symbol == self.down_from:
            self.down_from, self.down = None, True
        if symbol in self.rate_limited:
            raise EToroRateLimitError("eToro cooldown")
        if self.down and self.down_empty:  # some feeds answer an outage with no rows
            return _frame(1, stamp=self.stamp).iloc[0:0]
        if self.down or symbol in self.fail:
            raise RuntimeError("feed down")
        frame = _frame(1, stamp=self.stamp)
        if symbol in self.stale:  # a halted ticker: its last bar is weeks old
            return frame[frame["timestamp"] < pd.Timestamp("2026-09-01", tz="UTC")]
        return frame


class _Boom:
    def __getattr__(self, name):
        raise AssertionError(f"shadow scan touched {name}")


class _Notifier:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def send_text(self, message, **kwargs):
        self.messages.append(message)
        return True


class _StopAfter:
    """A scan deadline that passes after ``n`` symbols."""

    def __init__(self, n: int) -> None:
        self.n, self.calls = n, 0

    def is_set(self) -> bool:
        self.calls += 1
        return self.calls > self.n


def _specs(settings):
    return list(strategy_specs_for(settings, timeframe="1d", regime=None))


def _service(tmp_path, monkeypatch, *, now=MONDAY_0835_NY, verdicts=None, market=None, **overrides):
    import app.performance.strategy_evidence as evidence

    monkeypatch.setattr(deep, "utc_now", lambda: now)
    monkeypatch.setattr(evidence, "utc_now", lambda: now)  # the 24 h staleness clock
    values = dict(
        market_universe_symbols=[*SYMBOLS, "BTC/USD"],
        market_universe_limit=200,
        allowed_instruments=SYMBOLS,
    )
    values.update(overrides)
    settings = make_settings(tmp_path, **values)
    state = FakeState()
    computed_at = (now if verdicts != "stale" else datetime(2026, 10, 1, tzinfo=UTC)).isoformat()
    passing = {"trend_following:1d": {"passed": True, "oos_expectancy_r": 0.046, "oos_trades": 300}}
    state.set(
        VERDICTS_KEY,
        json.dumps(
            {
                "computed_at": computed_at,
                "verdicts": passing if verdicts in (None, "stale") else verdicts,
            }
        ),
    )
    return SimpleNamespace(
        settings=settings,
        market_screener=_Screener(_specs(settings)),
        market_data=market or _MarketData(),
        runtime_state=state,
        run_logs=FakeLogs(),
        proposal_service=None,
        reconciliation=None,
        notifier=_Notifier(),
        auto_trading=_Boom(),
        tracked_signals=_Boom(),
        alert_history=_Boom(),
    )


def _run(service, **kwargs):
    return deep.run_premarket_deep_scan(service, state_key=STATE_KEY, force_refresh=True, **kwargs)


def _watchlist(service, date="2026-10-12"):
    return deep.load_watchlist(service.runtime_state, date)


def _trend(watchlist) -> dict[str, dict]:
    """The evidence-passing strategy's signal per symbol (seed 1 gives one on every symbol)."""

    return {i["symbol"]: i for i in watchlist["signals"] if i["strategy"] == "trend_following"}


def _clock(monkeypatch, start: datetime, step: timedelta) -> None:
    moment = {"t": start}

    def now():
        moment["t"] += step
        return moment["t"]

    monkeypatch.setattr(deep, "utc_now", now)


def test_scans_every_allowed_equity_with_every_daily_strategy(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch)
    result = _run(service)
    watchlist = _watchlist(service)
    specs = [s for s in _specs(service.settings) if s.name != "pairs_stat_arb"]
    assert result.status == "ok" and watchlist["status"] == "complete"
    assert service.market_data.calls == SYMBOLS  # all 25, never the crypto pair
    assert len(specs) == DAILY_SPECS and watchlist["evaluated_runs"] == 25 * DAILY_SPECS
    assert watchlist["errors"] == [] and len(watchlist["signals"]) > 25
    assert service.runtime_state.get(STATE_KEY) is not None  # done for the day
    assert [e for e, _ in service.run_logs.items] == ["premarket_deep_scan_completed"]
    assert service.run_logs.items[0][1]["shadow_only"] is True


@pytest.mark.parametrize("stamp", ["04:00Z", "05:00Z", "00:00Z", "13:30Z", "20:00Z", "00:00"])
def test_only_todays_bar_is_dropped_whatever_the_feed_stamps(stamp) -> None:
    # Alpaca and yfinance: New York midnight (04:00Z / 05:00Z); other feeds 00:00Z, the open,
    # the close, or naive. The 2026-10-10 review found 00:00Z kept today's forming bar.
    now = datetime(2026, 10, 12, 8, 35, tzinfo=NY)
    frame = pd.DataFrame(
        {"timestamp": [f"2026-10-0{d}T{stamp}" for d in (8, 9)] + [f"2026-10-12T{stamp}"]}
    )
    kept = deep.completed_daily_bars(frame, now)
    assert [str(t)[:10] for t in kept["timestamp"]] == ["2026-10-08", "2026-10-09"]
    before_any_bar_today = frame.iloc[:2]
    assert len(deep.completed_daily_bars(before_any_bar_today, now)) == 2


@pytest.mark.parametrize("stamp", ["04:00", "00:00"])
def test_signals_are_the_strategy_decision_on_the_completed_bar(
    tmp_path, monkeypatch, stamp
) -> None:
    # The backtest engine decides bar N with generate_signal on the frame ending at bar N.
    service = _service(tmp_path, monkeypatch, market=_MarketData(stamp=stamp))
    _run(service)
    signals = [i for i in _watchlist(service)["signals"] if i["symbol"] == "S00"]
    specs = [s for s in _specs(service.settings) if s.name != "pairs_stat_arb"]
    direct = _direct_buys(_completed(1, stamp=stamp), specs)
    assert len(direct) >= 3, "seed 1 gives several daily buys, so parity is not vacuous"
    assert {i["strategy"] for i in signals} == set(direct)  # nothing missing, nothing extra
    for item in signals:
        signal = direct[item["strategy"]]
        assert (item["entry"], item["stop"], item["target"]) == (
            round(signal.price, 4),
            round(signal.stop_loss, 4),
            round(signal.take_profit, 4),
        )
        assert item["signal_bar_ts"].startswith("2026-10-09")  # Friday, not Monday's forming bar
    assert (
        _direct_buys(_frame(1, stamp=stamp), specs).keys() != direct.keys()
    )  # the gap bar matters


def test_evidence_is_the_etoro_mirror_rule_even_with_the_paper_gate_off(
    tmp_path, monkeypatch
) -> None:
    # The live eToro mirror always requires evidence, whatever require_strategy_oos_evidence says.
    service = _service(tmp_path, monkeypatch, require_strategy_oos_evidence=False)
    _run(service)
    watchlist = _watchlist(service)
    by_strategy = {i["strategy"]: i for i in watchlist["signals"]}
    assert by_strategy["trend_following"]["evidence_passing"] is True
    assert by_strategy["trend_following"]["oos_expectancy_r"] == 0.046
    assert by_strategy["ema_trend_stack"]["evidence_passing"] is False  # no verdict
    assert {i["strategy"] for i in watchlist["items"]} == {"trend_following"}
    stale = _service(tmp_path / "s", monkeypatch, verdicts="stale")
    _run(stale)
    signals = _watchlist(stale)["signals"]
    assert signals and not any(i["evidence_passing"] for i in signals)  # a day-old pass fails


def test_a_symbol_the_bot_may_not_trade_is_never_tradeable(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch, allowed_instruments=SYMBOLS[1:])
    _run(service)
    trend = _trend(_watchlist(service))
    assert not trend["S00"]["allowed"] and not trend["S00"]["tradeable"]
    assert trend["S01"]["allowed"] and trend["S01"]["tradeable"]
    assert all(i["symbol"] != "S00" for i in _watchlist(service)["items"])


def test_a_blacklisted_symbol_is_flagged_and_not_tradeable(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch)
    service.reconciliation = SimpleNamespace(
        broker_positions=SimpleNamespace(list_active=lambda: []),
        safety=SimpleNamespace(list_blacklist=lambda: [{"symbol": "s04", "active": 1}]),
    )
    _run(service)
    trend = _trend(_watchlist(service))
    assert trend["S04"]["blacklisted"] and not trend["S04"]["tradeable"]
    assert not trend["S05"]["blacklisted"] and trend["S05"]["tradeable"]


def test_weak_signals_are_listed_but_never_tradeable(tmp_path, monkeypatch) -> None:
    # Weak-valid signals need the operator's approval live, so they never rank as tradeable.
    service = _service(tmp_path, monkeypatch)
    real_build = service.market_screener._build_strategy

    def build(spec):
        strategy = real_build(spec)
        if spec.name != "trend_following":
            return strategy

        def weak(frame, symbol):
            signal = strategy.generate_signal(frame, symbol)
            signal.metadata = {**(signal.metadata or {}), "weak_signal_kind": "pullback_watch"}
            return signal

        return SimpleNamespace(generate_signal=weak)

    monkeypatch.setattr(service.market_screener, "_build_strategy", build)
    _run(service)
    watchlist = _watchlist(service)
    trend = list(_trend(watchlist).values())
    assert trend and all(i["kind"] == "weak" and i["evidence_passing"] for i in trend)
    assert not any(i["tradeable"] for i in trend) and watchlist["items"] == []
    kinds = [i["kind"] for i in sorted(watchlist["signals"], key=deep.rank_key)]
    assert "weak" in kinds and kinds == sorted(kinds, key=lambda k: k != "strict")  # strict first


def test_rank_order() -> None:
    rows = [  # symbol, tradeable, kind, expectancy, rr
        ("D", False, "strict", 0.9, 3),
        ("C", True, "weak", 0.9, 3),
        ("B", True, "strict", 0.01, 3),
        ("A", True, "strict", 0.05, 1),
        ("E", True, "strict", 0.05, 2),
        ("F", True, "strict", None, 9),
    ]
    items = [
        dict(symbol=s, spec_key="a", tradeable=t, kind=k, oos_expectancy_r=e, rr=r)
        for s, t, k, e, r in rows
    ]
    assert [i["symbol"] for i in sorted(items, key=deep.rank_key)] == ["E", "A", "B", "F", "C", "D"]


def test_a_budget_stop_saves_a_cursor_and_the_next_tick_resumes(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch)
    first = _run(service, stop=_StopAfter(10))
    partial = _watchlist(service)
    assert first.status == "ok" and partial["status"] == "partial" and partial["cursor"] == 10
    assert service.runtime_state.get(STATE_KEY) is None  # the bucket stays due
    assert json.loads(service.runtime_state.get(deep.COVERAGE_KEY))["deadline_exceeded"] is True
    _run(service)
    done = _watchlist(service)
    assert done["status"] == "complete" and service.market_data.calls == SYMBOLS  # no repeats
    keys = [(i["symbol"], i["spec_key"]) for i in done["signals"]]
    assert len(keys) == len(set(keys)) and done["evaluated_runs"] == 25 * DAILY_SPECS


def test_without_a_budget_stop_the_scan_is_still_bounded(tmp_path, monkeypatch) -> None:
    # Manual runs and the legacy budget pass no stop; the old scan was capped by
    # screener_batch_deadline_seconds inside scan_universe, so this scan is too.
    service = _service(
        tmp_path,
        monkeypatch,
        screener_market_data_timeout_seconds=0,
        screener_batch_deadline_seconds=60,
    )
    seconds = {"t": 1000.0}

    def monotonic():
        seconds["t"] += 10.0
        return seconds["t"]

    monkeypatch.setattr(deep.time, "monotonic", monotonic)
    assert _run(service).status == "ok"
    partial = _watchlist(service)
    assert partial["status"] == "partial" and 0 < partial["cursor"] < 25
    assert service.market_data.calls == SYMBOLS[: partial["cursor"]]


def test_past_the_cutoff_with_no_list_it_records_a_missed_day(tmp_path, monkeypatch) -> None:
    late = datetime(2026, 10, 12, 13, 20, tzinfo=UTC)  # 09:20 EDT: the bot was down at 08:30
    service = _service(tmp_path, monkeypatch, now=late)
    result = _run(service)
    assert result.status == "ok" and service.market_data.calls == []  # never runs into the session
    assert _watchlist(service)["status"] == "missed_cutoff"
    assert json.loads(service.runtime_state.get(deep.COVERAGE_KEY))["status"] == "missed_cutoff"
    assert service.runtime_state.get(STATE_KEY) is not None
    assert [e for e, _ in service.run_logs.items] == ["premarket_deep_scan_skipped"]
    assert _run(service).skipped and service.market_data.calls == []


def test_a_partial_list_resumed_after_the_cutoff_is_finished_as_is(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch)
    _run(service, stop=_StopAfter(10))
    monkeypatch.setattr(deep, "utc_now", lambda: datetime(2026, 10, 12, 13, 25, tzinfo=UTC))
    assert _run(service).status == "ok"
    watchlist = _watchlist(service)
    assert watchlist["status"] == "incomplete_at_cutoff" and watchlist["cursor"] == 10
    assert service.market_data.calls == SYMBOLS[:10]  # nothing fetched after 09:20
    assert {i["symbol"] for i in watchlist["items"]} <= set(SYMBOLS[:10])
    assert service.runtime_state.get(STATE_KEY) is not None


def test_a_scan_that_crosses_the_cutoff_stops_there(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch)
    _clock(monkeypatch, datetime(2026, 10, 12, 13, 14, tzinfo=UTC), timedelta(minutes=1))
    _run(service)
    watchlist = _watchlist(service)
    assert watchlist["status"] == "incomplete_at_cutoff" and 0 < watchlist["cursor"] < 25
    assert service.market_data.calls == SYMBOLS[: watchlist["cursor"]]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("09:20", (9, 20)),
        ("08:45", (8, 45)),
        ("09:20:00", (9, 20)),
        ("10:15", (9, 30)),  # never into the session
        ("0920", (9, 20)),  # mistyped: the default, not an error every tick
        ("9.20", (9, 20)),
        ("24:00", (9, 20)),
        ("", (9, 20)),
    ],
)
def test_cutoff_setting_is_parsed_safely(tmp_path, raw, expected) -> None:
    settings = make_settings(tmp_path, premarket_deep_scan_cutoff_local=raw)
    cutoff = deep._cutoff(datetime(2026, 10, 12, 8, 35, tzinfo=NY), settings)
    assert (cutoff.hour, cutoff.minute) == expected


def test_one_symbol_or_strategy_failure_is_isolated(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch, market=_MarketData(fail={"S03"}))
    real_build = service.market_screener._build_strategy

    def build(spec):
        if spec.name == "ema_trend_stack":
            raise RuntimeError("boom")
        return real_build(spec)

    monkeypatch.setattr(service.market_screener, "_build_strategy", build)
    _run(service)
    watchlist = _watchlist(service)
    assert watchlist["status"] == "complete_with_gaps" and watchlist["failed_symbols"] == ["S03"]
    assert any(err.startswith("S03: history") for err in watchlist["errors"])
    assert any(err.startswith("S00 ema_trend_stack:1d: boom") for err in watchlist["errors"])
    assert watchlist["evaluated_runs"] == 24 * DAILY_SPECS
    assert set(_trend(watchlist)) == set(SYMBOLS) - {"S03"}  # the other strategies still ran


def test_an_etoro_rate_limit_pauses_instead_of_burning_the_universe(tmp_path, monkeypatch) -> None:
    market = _MarketData(rate_limited={"S03"})
    service = _service(tmp_path, monkeypatch, market=market)
    assert _run(service).status == "ok"
    partial = _watchlist(service)
    assert partial["status"] == "partial" and partial["cursor"] == 3
    assert partial["paused_reason"] == "etoro_rate_limit" and partial["errors"] == []
    assert service.runtime_state.get(STATE_KEY) is None  # retried next tick
    market.rate_limited.clear()  # the cooldown ends
    _run(service)
    done = _watchlist(service)
    assert done["status"] == "complete" and "paused_reason" not in done
    assert market.calls == [*SYMBOLS[:4], *SYMBOLS[3:]]  # S03 retried once, nothing skipped


def test_a_strategy_dropped_between_ticks_is_recorded(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch)
    _run(service, stop=_StopAfter(5))
    screener = service.market_screener
    screener.specs = [s for s in screener.specs if s.name != "ma_crossover"]  # e.g. demoted
    _run(service)
    watchlist = _watchlist(service)
    assert watchlist["notes"] == ["spec_unavailable_on_resume: ma_crossover:1d"]
    assert watchlist["evaluated_runs"] == 5 * DAILY_SPECS + 20 * (DAILY_SPECS - 1)


def test_an_overlapping_run_never_replaces_a_finished_list(tmp_path, monkeypatch) -> None:
    # A manual run can slip past the workflow lock's read-then-write (review 2026-10-10).
    service = _service(tmp_path, monkeypatch)
    real_scan = deep._scan_symbol
    nested = {"done": False}

    def scan(service_, watchlist, symbol, *args):
        if symbol == "S02" and not nested["done"]:
            nested["done"] = True
            _run(service_)  # run B starts after A loaded the (empty) list and finishes it
        return real_scan(service_, watchlist, symbol, *args)

    monkeypatch.setattr(deep, "_scan_symbol", scan)
    _run(service, stop=_StopAfter(5))  # run A then pauses and tries to save its partial
    watchlist = _watchlist(service)
    assert watchlist["status"] == "complete" and watchlist["cursor"] == 25
    assert len(watchlist["items"]) == 25  # B's list, untouched by A's later save
    assert [e for e, _ in service.run_logs.items] == ["premarket_deep_scan_completed"]


def test_flags_held_and_pending_symbols(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch)
    service.reconciliation = SimpleNamespace(
        broker_positions=SimpleNamespace(list_active=lambda: [{"symbol": "S00"}])
    )
    pending = SimpleNamespace(
        order=SimpleNamespace(symbol="S01"), expires_at="2026-10-12T20:00:00+00:00"
    )
    lapsed = SimpleNamespace(
        order=SimpleNamespace(symbol="S05"), expires_at="2026-10-12T12:00:00+00:00"
    )
    service.proposal_service = SimpleNamespace(
        proposals=SimpleNamespace(list=lambda status: [pending, lapsed]),  # read-only repository
        list_proposals=_Boom(),  # writes expiries on read
    )
    _run(service)
    trend = _trend(_watchlist(service))
    assert trend["S00"]["held_paper"] and not trend["S00"]["tradeable"]
    assert trend["S01"]["pending_proposal"] and not trend["S01"]["tradeable"]
    assert not trend["S05"]["pending_proposal"] and trend["S05"]["tradeable"]  # it expired
    assert trend["S04"]["tradeable"]


def test_etoro_room_hints(tmp_path, monkeypatch) -> None:
    from app.broker.etoro_live_mirror import STATE_KEY as ETORO_STATE

    tickers = ["NVDA", "AMD", "MSFT", "META", "GOOGL", "JPM"]
    service = _service(
        tmp_path, monkeypatch, market_universe_symbols=tickers, allowed_instruments=tickers
    )
    service.runtime_state.set(ETORO_STATE, json.dumps({"open_symbols": tickers[:4]}))  # tech 4/4
    _run(service)
    trend = _trend(_watchlist(service))
    assert trend["NVDA"]["etoro_symbol_open"] and not trend["GOOGL"]["etoro_symbol_open"]
    assert trend["GOOGL"]["etoro_bucket_full"] and not trend["JPM"]["etoro_bucket_full"]
    assert trend["GOOGL"]["tradeable"]  # a hint: paper may still trade it, eToro will not copy


def test_the_saved_watchlist_is_strict_json(tmp_path, monkeypatch) -> None:
    # The scorecard casts the list to jsonb, which rejects NaN and Infinity.
    nan_verdict = {
        "trend_following:1d": {
            "passed": True,
            "oos_expectancy_r": float("nan"),
            "oos_trades": float("nan"),  # copied as-is: only the save's cleaner can catch it
        }
    }
    service = _service(tmp_path, monkeypatch, verdicts=nan_verdict)
    real_build = service.market_screener._build_strategy
    endless = SimpleNamespace(
        generate_signal=lambda frame, symbol: SimpleNamespace(
            action="buy", price=10.0, stop_loss=9.0, take_profit=float("inf"), metadata={}
        )
    )
    monkeypatch.setattr(
        service.market_screener,
        "_build_strategy",
        lambda spec: endless if spec.name == "ma_crossover" else real_build(spec),
    )
    _run(service)
    raw = service.runtime_state.get(deep.WATCHLIST_KEY.format(date="2026-10-12"))

    def refuse(token):
        raise ValueError(token)

    watchlist = json.loads(raw, parse_constant=refuse)
    assert watchlist["status"] == "complete"
    trend = next(i for i in watchlist["items"] if i["strategy"] == "trend_following")
    assert trend["evidence_passing"] is True and trend["oos_expectancy_r"] is None
    assert trend["oos_trades"] is None
    assert not any(i["strategy"] == "ma_crossover" for i in watchlist["signals"])  # infinite target


def test_coverage_record_keeps_the_health_page_current(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch)
    _run(service)
    coverage = json.loads(service.runtime_state.get(deep.COVERAGE_KEY))
    assert coverage["mode"] == "premarket_deep_scan_shadow" and coverage["status"] == "complete"
    assert (coverage["symbols_requested"], coverage["symbols_evaluated"]) == (25, 25)
    assert (
        coverage["evaluated_strategy_runs"]
        == coverage["expected_strategy_runs"]
        == 25 * DAILY_SPECS
    )
    assert coverage["deadline_exceeded"] is False and coverage["proposals_created"] == 0


def _workflow(tmp_path, enabled: bool):
    from app.live_signal_schema import MarketQuote
    from app.workflow.service import SignalWorkflowService
    from tests.test_workflow_service import (
        FakeAlertHistory,
        FakeMarketDataEngine,
        FakeMarketScreener,
        FakeNotifier,
        FakeTrackedSignals,
    )

    screener = FakeMarketScreener([], spec_keys=["trend_following:1d", "momentum_breakout:1h"])
    state = FakeState()
    flow = SignalWorkflowService(
        settings=make_settings(tmp_path, premarket_deep_scan_enabled=enabled),
        market_screener=screener,
        market_data_engine=FakeMarketDataEngine(MarketQuote(symbol="NVDA", last_execution=1.0)),
        notifier=FakeNotifier(),
        tracked_signals=FakeTrackedSignals(),
        alert_history=FakeAlertHistory(),
        runtime_state=state,
        run_logs=FakeLogs(),
    )
    return flow, screener, state


def _record_deep_runs(monkeypatch) -> list[dict]:
    ran: list[dict] = []
    monkeypatch.setattr(
        deep,
        "run_premarket_deep_scan",
        lambda service, **kw: ran.append(kw) or deep._response("ok", "x"),
    )
    return ran


def test_workflow_premarket_bucket_runs_the_deep_scan_only_when_enabled(
    tmp_path, monkeypatch
) -> None:
    ran = _record_deep_runs(monkeypatch)
    flow, screener, state = _workflow(tmp_path, True)
    flow.run_premarket_scan(notify=False)
    assert len(ran) == 1 and ran[0]["stop"] is None and screener.calls == []  # manual run
    assert not any(k.startswith("workflow:spec_batch") for k in state.values)  # no cursor moved
    flow, screener, state = _workflow(tmp_path, False)
    flow.run_premarket_scan(notify=False)
    assert len(ran) == 1 and len(screener.calls) == 1  # flag off: the old scan, unchanged
    assert any(k.startswith("workflow:spec_batch") for k in state.values)  # ...which moves it


def test_the_cadence_budget_still_governs_the_deep_scan(tmp_path, monkeypatch) -> None:
    from app.workflow import cadence_budget as cb

    ran = _record_deep_runs(monkeypatch)
    now = {"t": 150.0}  # pre-work ran long: only 30 s left before the scan stop
    monkeypatch.setattr(cb.time, "monotonic", lambda: now["t"])
    flow, _, state = _workflow(tmp_path, True)
    budget = cb.CadenceBudget.for_tick(flow.settings, 0.0)
    with cb.tick_budget(budget):
        assert flow.run_premarket_scan(notify=False).status == "skipped"
    assert ran == [] and state.get(STATE_KEY) is None  # deferred, still due next tick
    now["t"] = 10.0
    with cb.tick_budget(budget):
        flow.run_premarket_scan(notify=False)
    assert isinstance(ran[0]["stop"], cb.ScanDeadline)
    assert ran[0]["stop"].stop_at == budget.scan_stop_at


@pytest.mark.parametrize("missing", [True, False])
def test_watchlist_route(tmp_path, missing) -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app
    from tests.conftest import MockBroker

    app = create_app(make_settings(tmp_path), broker=MockBroker(), enable_background_jobs=False)
    if not missing:
        app.state.workflow_service.runtime_state.set(
            deep.WATCHLIST_KEY.format(date="2026-10-12"),
            json.dumps({"status": "complete", "items": []}),
        )
    response = TestClient(app).get("/workflow/premarket-watchlist?date=2026-10-12")
    assert response.status_code == (404 if missing else 200)
    if not missing:
        assert response.json()["status"] == "complete"


def _table_rows(db_path) -> dict[str, list]:
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        return {
            table: sorted(repr(row) for row in conn.execute(f'SELECT * FROM "{table}"'))
            for table in tables
        }


def _no_network(monkeypatch) -> None:
    import socket

    def refuse(*args, **kwargs):
        raise AssertionError("the test tried to reach the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)


def test_on_the_real_wiring_it_writes_only_the_watchlist(tmp_path, monkeypatch) -> None:
    # The real app: real screener specs and strategy builders, real repositories (SQLite).
    import sqlite3

    from app.main import create_app
    from tests.conftest import MockBroker

    _no_network(monkeypatch)
    monkeypatch.setattr(deep, "utc_now", lambda: MONDAY_0835_NY)
    symbols = SYMBOLS[:4]
    weak_allowed = ["ema_trend_stack", "etf_mega_cap_relative_strength_rotation"]
    settings = make_settings(
        tmp_path,
        premarket_deep_scan_enabled=True,
        market_universe_symbols=symbols,
        allowed_instruments=symbols,
        screener_scheduler_enabled=False,
        ledger_cycle_enabled=False,
        learning_worker_enabled=False,
        paper_position_refresh_enabled=False,
        # Weak-signal emission on, so the live builder differs from a bare get_strategy.
        paper_strategy_weak_signal_emission_enabled=True,
        paper_supervised_weak_valid_enabled=True,
        paper_strategy_weak_signal_allowed_strategies=weak_allowed,
    )
    app = create_app(settings, broker=MockBroker(), enable_background_jobs=False)
    monkeypatch.setattr(
        app.state.market_data_engine, "get_history", lambda symbol, **kw: _frame(17)
    )
    db_path = tmp_path / "test.db"
    before = _table_rows(db_path)
    with sqlite3.connect(db_path) as conn:
        last_log = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM run_logs").fetchone()[0]
        keys_before = {row[0] for row in conn.execute("SELECT state_key FROM runtime_state")}

    result = app.state.workflow_service.run_premarket_scan(notify=False)

    assert result.status == "ok", result.detail
    after = _table_rows(db_path)
    assert {t for t in after if after[t] != before.get(t)} <= {"runtime_state", "run_logs"}
    with sqlite3.connect(db_path) as conn:
        events = [
            row[0]
            for row in conn.execute("SELECT event_type FROM run_logs WHERE rowid > ?", (last_log,))
        ]
        new_keys = {
            row[0] for row in conn.execute("SELECT state_key FROM runtime_state")
        } - keys_before
    assert events == ["workflow_premarket_scan_started", "premarket_deep_scan_completed"]
    assert {"premarket:watchlist:2026-10-12", deep.COVERAGE_KEY} <= new_keys
    assert all(k.startswith(("premarket:", "workflow:")) for k in new_keys), new_keys
    watchlist = deep.load_watchlist(app.state.workflow_service.runtime_state, "2026-10-12")
    assert watchlist["status"] == "complete" and watchlist["universe"] == symbols
    assert "pairs_stat_arb:1d" not in watchlist["specs"]
    # Parity with the LIVE builder (default kwargs, liquidity floor, weak-signal emission).
    screener = app.state.market_screener_service
    specs = [s for s in screener._strategy_specs_for_timeframe("1d") if s.name != "pairs_stat_arb"]
    direct = _direct_buys(_completed(17), specs, build=screener._build_strategy)
    bare = _direct_buys(_completed(17), specs)
    assert set(direct) - set(bare), "the live builder emits buys a bare get_strategy never does"
    s00 = {i["strategy"]: i for i in watchlist["signals"] if i["symbol"] == "S00"}
    assert set(s00) == set(direct)
    assert all(s00[name]["entry"] == round(sig.price, 4) for name, sig in direct.items())
    assert all(s00[name]["kind"] == "weak" for name in set(direct) - set(bare))


@pytest.mark.parametrize("empty", [False, True])
def test_a_feed_outage_pauses_and_retries_instead_of_closing_the_day(
    tmp_path, monkeypatch, empty
) -> None:
    market = _MarketData(down_from="S03", down_empty=empty)
    service = _service(tmp_path, monkeypatch, market=market)
    _run(service)
    partial = _watchlist(service)
    assert partial["status"] == "partial" and partial["cursor"] == 3
    assert partial["paused_reason"] == "market_data_unavailable" and partial["outage_pauses"] == 1
    assert partial["errors"] == [] and partial["failed_symbols"] == []  # retried, not counted
    assert market.calls[-1] == "S02"  # the known-good probe failed too: a real outage
    assert service.runtime_state.get(STATE_KEY) is None
    market.down = False  # the feed comes back
    _run(service)
    done = _watchlist(service)
    assert done["status"] == "complete" and done["failed_symbols"] == []
    assert "paused_reason" not in done and done["evaluated_runs"] == 25 * DAILY_SPECS


def test_close_to_the_cutoff_missing_data_is_a_gap_not_a_retry(tmp_path, monkeypatch) -> None:
    late = datetime(2026, 10, 12, 13, 6, tzinfo=UTC)  # 09:06 EDT: under 15 min to the cutoff
    market = _MarketData(down_from="S22")
    service = _service(tmp_path, monkeypatch, now=late, market=market)
    _run(service)
    watchlist = _watchlist(service)
    assert watchlist["status"] == "complete_with_gaps"
    assert watchlist["failed_symbols"] == ["S22", "S23", "S24"]


def test_dead_tickers_side_by_side_are_gaps_not_an_outage(tmp_path, monkeypatch) -> None:
    # Review 2026-10-11: three bad symbols in a row stalled the scan until 09:05.
    market = _MarketData(fail={"S03", "S04", "S05"})
    service = _service(tmp_path, monkeypatch, market=market)
    _run(service)
    watchlist = _watchlist(service)
    assert watchlist["status"] == "complete_with_gaps" and "paused_reason" not in watchlist
    assert watchlist["failed_symbols"] == ["S03", "S04", "S05"]
    assert market.calls.count("S02") == 2  # scanned once, probed once: the feed is up


def test_an_outage_spanning_two_ticks_is_retried_whole(tmp_path, monkeypatch) -> None:
    market = _MarketData(down_from="S23")
    service = _service(tmp_path, monkeypatch, market=market)
    _run(service, stop=_StopAfter(24))  # S23 fails, then the budget stop ends the tick
    assert _watchlist(service)["no_data_streak"] == 1
    _run(service)  # S24 fails at the end of the list; the S22 probe fails: rewind both
    partial = _watchlist(service)
    assert partial["status"] == "partial" and partial["cursor"] == 23
    assert partial["failed_symbols"] == [] and partial["paused_reason"] == "market_data_unavailable"
    market.down = False
    _run(service)
    done = _watchlist(service)
    assert done["status"] == "complete" and done["failed_symbols"] == []


def test_a_halted_tickers_old_bars_are_a_gap_not_a_setup(tmp_path, monkeypatch) -> None:
    market = _MarketData(stale={"S07"})
    service = _service(tmp_path, monkeypatch, market=market)
    _run(service)
    watchlist = _watchlist(service)
    assert watchlist["status"] == "complete_with_gaps" and watchlist["failed_symbols"] == ["S07"]
    assert any(e.startswith("S07: stale_bars (last 2026-08-31)") for e in watchlist["errors"])
    assert not any(i["symbol"] == "S07" for i in watchlist["signals"])


def test_a_pause_that_lasts_to_the_cutoff_stays_on_the_record(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch, market=_MarketData(rate_limited={"S03"}))
    _run(service)
    monkeypatch.setattr(deep, "utc_now", lambda: datetime(2026, 10, 12, 13, 25, tzinfo=UTC))
    _run(service)
    watchlist = _watchlist(service)
    assert watchlist["status"] == "incomplete_at_cutoff"
    assert watchlist["paused_reason"] == "etoro_rate_limit"
    coverage = json.loads(service.runtime_state.get(deep.COVERAGE_KEY))
    assert coverage["paused_reason"] == "etoro_rate_limit" and coverage["deadline_exceeded"] is True


def test_a_dropped_strategy_note_survives_the_error_cap(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch)
    _run(service, stop=_StopAfter(5))
    screener = service.market_screener
    real_build = screener._build_strategy
    screener.specs = [s for s in screener.specs if s.name != "ma_crossover"]

    def build(spec):  # 20 more symbols x 1 failing strategy: 20 errors, the cap
        if spec.name == "ema_trend_stack":
            raise RuntimeError("boom")
        return real_build(spec)

    monkeypatch.setattr(screener, "_build_strategy", build)
    _run(service)
    watchlist = _watchlist(service)
    assert len(watchlist["errors"]) == 20
    assert watchlist["notes"] == ["spec_unavailable_on_resume: ma_crossover:1d"]


def test_a_stored_list_it_cannot_resume_starts_over(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch)
    service.runtime_state.set(
        deep.WATCHLIST_KEY.format(date="2026-10-12"),
        json.dumps({"status": "partial", "cursor": "7"}),
    )
    assert _run(service).status == "ok"
    assert _watchlist(service)["status"] == "complete" and service.market_data.calls == SYMBOLS


def _overlap(monkeypatch, at_symbol, nested_run):
    """Run ``nested_run`` once, as a second run that slipped past the lock, at ``at_symbol``."""

    real_scan = deep._scan_symbol
    fired = {"done": False}

    def scan(service_, watchlist, symbol, *args):
        if symbol == at_symbol and not fired["done"]:
            fired["done"] = True
            nested_run(service_)
        return real_scan(service_, watchlist, symbol, *args)

    monkeypatch.setattr(deep, "_scan_symbol", scan)


def test_a_run_behind_another_never_saves_over_its_progress(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch)
    _overlap(monkeypatch, "S02", lambda svc: _run(svc, stop=_StopAfter(10)))  # B saves at 10
    _run(service, stop=_StopAfter(5))  # A pauses at 5, behind B
    watchlist = _watchlist(service)
    assert watchlist["status"] == "partial" and watchlist["cursor"] == 10


def test_a_run_finishing_after_another_finished_keeps_the_first_list(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch)
    first = {}

    def run_b(svc):
        _run(svc)
        first["run_id"] = _watchlist(svc)["run_id"]

    _overlap(monkeypatch, "S02", run_b)
    result = _run(service)  # A finishes too, after B
    assert result.skipped and _watchlist(service)["run_id"] == first["run_id"]
    assert [e for e, _ in service.run_logs.items] == ["premarket_deep_scan_completed"]  # B's only


def test_at_the_cutoff_a_run_behind_finishes_the_further_list(tmp_path, monkeypatch) -> None:
    service = _service(tmp_path, monkeypatch)

    def run_b(svc):
        _run(svc, stop=_StopAfter(10))  # B saves a partial at 10...
        monkeypatch.setattr(deep, "utc_now", lambda: datetime(2026, 10, 12, 13, 21, tzinfo=UTC))

    _overlap(monkeypatch, "S02", run_b)  # ...then the cutoff passes while A is at S02
    _run(service)
    watchlist = _watchlist(service)
    assert watchlist["status"] == "incomplete_at_cutoff" and watchlist["cursor"] == 10
    assert service.runtime_state.get(STATE_KEY) is not None


def test_it_sends_telegram_only_when_asked(tmp_path, monkeypatch) -> None:
    quiet = _service(tmp_path, monkeypatch)
    _run(quiet)
    assert quiet.notifier.messages == []
    loud = _service(tmp_path / "n", monkeypatch, premarket_deep_scan_notify=True)
    _run(loud)
    assert len(loud.notifier.messages) == 1
    assert loud.notifier.messages[0].startswith("SHADOW pre-market setups 2026-10-12")


def test_a_dead_runs_lock_goes_stale_in_7_minutes_for_the_deep_scan(tmp_path, monkeypatch) -> None:
    # Review 2026-10-10: a deploy mid-scan left a 45-minute lock that outlived the cutoff.
    from app.workflow.cadence_budget import stale_lock_minutes

    on = make_settings(tmp_path, premarket_deep_scan_enabled=True, workflow_lock_timeout_minutes=45)
    off = make_settings(tmp_path, workflow_lock_timeout_minutes=45)
    slow = make_settings(
        tmp_path,
        premarket_deep_scan_enabled=True,
        scheduler_job_timeout_seconds=600,
        workflow_lock_timeout_minutes=45,
    )
    assert stale_lock_minutes(on, "premarket_scan") == 7  # 240 s timeout + 120 s grace, + 1 min
    assert stale_lock_minutes(slow, "premarket_scan") == 13  # follows a longer job timeout
    assert stale_lock_minutes(off, "premarket_scan") == 45  # flag off: exactly as before
    assert stale_lock_minutes(on, "swing_scan") == 45
    ran = _record_deep_runs(monkeypatch)
    flow, _, state = _workflow(tmp_path, True)
    lock = flow._lock_key("premarket_scan")
    state.set(lock, (datetime.now(UTC) - timedelta(minutes=6)).isoformat())
    assert flow.run_premarket_scan(notify=False).skipped and ran == []  # maybe still running
    state.set(lock, (datetime.now(UTC) - timedelta(minutes=8)).isoformat())
    flow.run_premarket_scan(notify=False)
    assert len(ran) == 1  # a dead run's lock no longer blocks the resume
    assert state.get(lock) == ""  # released after the run


def test_a_run_that_outlived_a_takeover_never_frees_the_newer_runs_lock(tmp_path) -> None:
    from app.workflow.cadence_budget import acquire_workflow_lock, release_workflow_lock

    state, key = FakeState(), "workflow:lock:premarket_scan"
    settings = make_settings(tmp_path, premarket_deep_scan_enabled=True)
    stamp_a = (datetime.now(UTC) - timedelta(minutes=8)).isoformat()  # run A, blocked 8 min
    state.set(key, stamp_a)
    stamp_b = acquire_workflow_lock(state, settings, "premarket_scan", key)  # B takes over
    assert stamp_b and acquire_workflow_lock(state, settings, "premarket_scan", key) is None
    release_workflow_lock(state, key, stamp_a)  # A finally finishes
    assert state.get(key) == stamp_b  # B's lock stands, so a third run stays out
    release_workflow_lock(state, key, stamp_b)
    assert state.get(key) == ""
