"""Inputs of the 2026-10-18 eToro copy rules, and proof that paper does not see them.

* Evidence streaks: ``refresh_verdicts`` keeps a ``passing_since`` per verdict key in the
  verdicts cache (runtime_state, so a deploy keeps it); a failed refresh ends the run, a gap
  restarts it. The verdicts, the refresh log and the paper gate are unchanged.
* Extension: ``entry_extension`` measures the 20-session rise, RSI(14) and 252-session closing
  highs on the signal bar; the screener nests it in the snapshot's metadata, where no score,
  gate or learning feature reads it.
"""

from __future__ import annotations

import json
from datetime import timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

import app.performance.strategy_evidence as evidence
from app.performance.strategy_evidence import (
    EVIDENCE_STREAK_MAX_GAP_HOURS,
    VERDICTS_KEY,
    compute_verdicts,
    evidence_blocker,
    passing_hours,
    refresh_verdicts,
)
from app.screener.extension import EXTENSION_FIELD, entry_extension
from app.storage.repositories import RunLogRepository, RuntimeStateRepository
from app.utils.time import utc_now
from tests.conftest import make_settings
from tests.test_phase_gates import _backtest, _db

# -- evidence streaks --------------------------------------------------------------------


class _Clock:
    def __init__(self, monkeypatch) -> None:
        self.now = utc_now()
        monkeypatch.setattr(evidence, "utc_now", lambda: self.now)

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta)


def _good(db, strategy="vwap_reclaim", expectancy=5.0, *, age_days=0.0):
    _backtest(
        db,
        symbol="AAPL",
        strategy=strategy,
        tf="1d",
        trades=50,
        expectancy=expectancy,
        holdout_trades=12,
        holdout_expectancy=2.0,
        age_days=age_days,
    )


def _since(state, key="vwap_reclaim:1d"):
    return json.loads(state.get(VERDICTS_KEY))["passing_since"].get(key)


def _hours(state, clock, strategy="vwap_reclaim"):
    return passing_hours(state.get(VERDICTS_KEY), strategy=strategy, timeframe="1d", now=clock.now)


def test_a_passing_key_keeps_its_start_across_refreshes(tmp_path, monkeypatch) -> None:
    clock = _Clock(monkeypatch)
    db = _db(tmp_path)
    state = RuntimeStateRepository(db)
    _good(db)
    _good(db, "losing", expectancy=-5.0)
    refresh_verdicts(db, SimpleNamespace(), state)
    start = clock.now.isoformat()
    assert _since(state) == start
    assert _since(state, "losing:1d") is None  # a failing key has no run
    for _ in range(48):  # 24 h of 30-min refreshes
        clock.advance(minutes=30)
        refresh_verdicts(db, SimpleNamespace(), state)
    assert _since(state) == start
    assert _hours(state, clock) == pytest.approx(24.0)
    assert _hours(state, clock, "losing") == 0.0


def test_a_restart_keeps_the_streak(tmp_path, monkeypatch) -> None:
    clock = _Clock(monkeypatch)
    settings = make_settings(tmp_path)
    db = _db(tmp_path)
    _good(db)
    refresh_verdicts(db, SimpleNamespace(), RuntimeStateRepository(db))
    start = clock.now.isoformat()
    clock.advance(minutes=40)  # a deploy: new process, new objects, same database
    from app.storage.db import Database

    fresh_db = Database(settings)
    fresh_db.initialize()
    fresh_state = RuntimeStateRepository(fresh_db)
    refresh_verdicts(fresh_db, SimpleNamespace(), fresh_state)
    assert _since(fresh_state) == start


def test_a_failed_refresh_ends_the_run(tmp_path, monkeypatch) -> None:
    clock = _Clock(monkeypatch)
    db = _db(tmp_path)
    state = RuntimeStateRepository(db)
    _good(db)
    refresh_verdicts(db, SimpleNamespace(), state)
    clock.advance(hours=30)
    for _ in range(3):  # keep the refreshes continuous
        refresh_verdicts(db, SimpleNamespace(), state)
    clock.advance(minutes=30)
    _good(db, expectancy=-9.0, age_days=-0.001)  # the newest row now loses: the key fails
    refresh_verdicts(db, SimpleNamespace(), state)
    assert _since(state) is None and _hours(state, clock) == 0.0
    clock.advance(minutes=30)
    _good(db, expectancy=9.0, age_days=-0.002)  # passes again: a new run from now
    refresh_verdicts(db, SimpleNamespace(), state)
    assert _since(state) == clock.now.isoformat() and _hours(state, clock) == 0.0


def test_a_refresh_gap_restarts_every_run(tmp_path, monkeypatch) -> None:
    clock = _Clock(monkeypatch)
    db = _db(tmp_path)
    state = RuntimeStateRepository(db)
    _good(db)
    refresh_verdicts(db, SimpleNamespace(), state)
    clock.advance(hours=EVIDENCE_STREAK_MAX_GAP_HOURS)  # at the limit: still continuous
    refresh_verdicts(db, SimpleNamespace(), state)
    start = _since(state)
    assert start != clock.now.isoformat()
    clock.advance(hours=EVIDENCE_STREAK_MAX_GAP_HOURS, seconds=1)  # nobody checked in between
    assert _hours(state, clock) is None  # the history is stale until the next refresh
    refresh_verdicts(db, SimpleNamespace(), state)
    assert _since(state) == clock.now.isoformat()


def test_an_old_or_unreadable_cache_restarts_without_failing_the_refresh(
    tmp_path, monkeypatch
) -> None:
    clock = _Clock(monkeypatch)
    db = _db(tmp_path)
    state = RuntimeStateRepository(db)
    _good(db)
    old = {"computed_at": (clock.now - timedelta(minutes=30)).isoformat(), "verdicts": {}}
    for previous in ("not json", json.dumps(old), json.dumps([1, 2])):
        state.set(VERDICTS_KEY, previous)
        verdicts = refresh_verdicts(db, SimpleNamespace(), state)
        assert verdicts["vwap_reclaim:1d"]["passed"]
        assert _since(state) == clock.now.isoformat()


def test_the_verdicts_log_and_paper_gate_are_unchanged(tmp_path, monkeypatch) -> None:
    _Clock(monkeypatch)
    db = _db(tmp_path)
    state = RuntimeStateRepository(db)
    logs = RunLogRepository(db)
    _good(db)
    _good(db, "losing", expectancy=-5.0)
    verdicts = refresh_verdicts(db, SimpleNamespace(), state, logs)
    cached = json.loads(state.get(VERDICTS_KEY))
    assert set(cached) == {"computed_at", "verdicts", "passing_since"}
    assert cached["verdicts"] == verdicts == compute_verdicts(db, SimpleNamespace())
    logged = logs.list_by_event("strategy_evidence_refreshed", limit=1)[0]["payload"]
    assert logged == {"passed": ["vwap_reclaim:1d"], "evaluated": 2, "enforced": False}
    paper = SimpleNamespace(require_strategy_oos_evidence=True)
    without = {VERDICTS_KEY: json.dumps({k: v for k, v in cached.items() if k != "passing_since"})}
    for strategy in ("vwap_reclaim", "losing", "never_tested"):
        kwargs = dict(strategy=strategy, timeframe="1d")
        assert evidence_blocker(paper, state, **kwargs) == evidence_blocker(
            paper, without, **kwargs
        )


def test_passing_hours_fails_closed_on_a_bad_history() -> None:
    now = utc_now()
    good = {
        "computed_at": now.isoformat(),
        "verdicts": {"s:1d": {"passed": True}},
        "passing_since": {"s:1d": (now - timedelta(hours=30)).isoformat()},
    }
    assert passing_hours(good, strategy="s", timeframe="1D", now=now) == pytest.approx(30.0)
    assert passing_hours(json.dumps(good), strategy="s", timeframe="1d", now=now) == pytest.approx(
        30.0
    )
    naive = {
        **good,
        "passing_since": {"s:1d": (now - timedelta(hours=30)).replace(tzinfo=None).isoformat()},
    }
    future = {**good, "passing_since": {"s:1d": (now + timedelta(hours=1)).isoformat()}}
    for bad in (
        None,
        "",
        "{",
        [],
        {**good, "computed_at": None},
        {**good, "computed_at": (now + timedelta(minutes=10)).isoformat()},  # from the future
        {**good, "passing_since": None},
        {**good, "verdicts": None},
        naive,
        future,
    ):
        assert passing_hours(bad, strategy="s", timeframe="1d", now=now) is None, bad
    assert passing_hours(good, strategy="other", timeframe="1d", now=now) == 0.0


def test_passing_hours_counts_only_up_to_the_last_refresh() -> None:
    # Review of 1400c62: nobody checked the evidence after the last refresh, so that time is
    # not part of the run (it was, up to EVIDENCE_STREAK_MAX_GAP_HOURS of it).
    now = utc_now()

    def payload(computed_at, since):
        return {
            "computed_at": computed_at.isoformat(),
            "verdicts": {"s:1d": {"passed": True}},
            "passing_since": {"s:1d": since.isoformat()},
        }

    refreshed = now - timedelta(minutes=110)
    late = payload(refreshed, refreshed - timedelta(hours=20))
    assert passing_hours(late, strategy="s", timeframe="1d", now=now) == pytest.approx(20.0)
    # A refresh stamped up to the clock skew "ahead" counts no further than now.
    ahead = payload(now + timedelta(minutes=3), now - timedelta(hours=30))
    assert passing_hours(ahead, strategy="s", timeframe="1d", now=now) == pytest.approx(30.0)


# -- extension inputs ----------------------------------------------------------------------


def _bars(closes, *, start="2025-01-01") -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    return pd.DataFrame(
        {
            "timestamp": pd.date_range(start=start, periods=len(closes), freq="B", tz="UTC"),
            "open": closes,
            "high": closes * 1.01,
            "low": closes * 0.99,
            "close": closes,
            "volume": np.full(len(closes), 1_000_000.0),
        }
    )


def _walk(n=400, seed=7):
    rng = np.random.default_rng(seed)
    return 100 * np.exp(np.cumsum(rng.normal(0.0, 0.015, n)))


def test_the_20_session_rise_and_rsi_match_the_strategy_math() -> None:
    from app.indicators import enrich_technical_indicators

    closes = _walk()
    frame = _bars(closes)
    ext = entry_extension(frame, timeframe="1d")
    rise = (closes[-1] / closes[-21] - 1) * 100
    assert ext["return_20d_pct"] == pytest.approx(rise, abs=1e-4)
    roc = frame["close"].pct_change(20).iloc[-1] * 100  # the rotation strategy's own measure
    assert ext["return_20d_pct"] == pytest.approx(roc, abs=1e-4)
    rsi = enrich_technical_indicators(frame, timeframe="1d")["rsi_14"].iloc[-1]
    assert ext["rsi_14"] == pytest.approx(rsi, abs=1e-4)
    assert ext["timeframe"] == "1d" and ext["bars"] == 400
    assert ext["bar_timestamp"] == str(frame["timestamp"].iloc[-1])


def test_rsi_edges() -> None:
    assert entry_extension(_bars(np.linspace(100, 150, 60)), timeframe="1d")["rsi_14"] == 100.0
    assert entry_extension(_bars(np.full(60, 100.0)), timeframe="1d")["rsi_14"] is None
    assert entry_extension(_bars(_walk(14)), timeframe="1d")["rsi_14"] is None  # 13 changes


def test_252_session_closing_highs() -> None:
    base = list(_walk(300, seed=3) * 0.5 + 50)  # well below the levels set next
    top = max(base)
    signal = entry_extension(_bars([*base[:-1], top * 1.2]), timeframe="1d")
    assert (signal["new_high_252_signal_bar"], signal["new_high_252_recent"]) == (True, True)
    prior = entry_extension(_bars([*base[:-2], top * 1.2, top * 1.1]), timeframe="1d")
    assert prior["new_high_252_signal_bar"] is False
    assert (prior["new_high_252_prior_bar"], prior["new_high_252_recent"]) == (True, True)
    calm = entry_extension(_bars([*base[:-2], top * 0.5, top * 0.5]), timeframe="1d")
    assert (calm["new_high_252_signal_bar"], calm["new_high_252_prior_bar"]) == (False, False)
    assert calm["new_high_252_recent"] is False
    tie = entry_extension(_bars([50.0] * 260 + [80.0] + [50.0] * 5 + [80.0]), timeframe="1d")
    assert tie["new_high_252_signal_bar"] is True  # equal to the window's high: counts
    # An old high outside the 252-session window does not count against a new one.
    old = entry_extension(_bars([top * 3, *base[-260:-1], top * 1.2]), timeframe="1d")
    assert old["new_high_252_signal_bar"] is True


def test_short_or_broken_history_reads_unknown() -> None:
    closes = list(_walk(252) * 0.5 + 50)
    just = entry_extension(_bars([*closes[:-1], max(closes) * 0.4]), timeframe="1d")
    assert just["new_high_252_signal_bar"] is False and just["new_high_252_prior_bar"] is None
    assert just["new_high_252_recent"] is None  # one bar short for the prior bar: unknown
    up = entry_extension(_bars([*closes[:-1], max(closes) * 2]), timeframe="1d")
    assert up["new_high_252_recent"] is True  # a known high is enough to block
    short = entry_extension(_bars(_walk(20)), timeframe="1d")
    assert short["return_20d_pct"] is None and short["new_high_252_recent"] is None
    gap = list(_walk(400))
    gap[-5] = float("nan")
    broken = entry_extension(_bars(gap), timeframe="1d")
    assert broken["return_20d_pct"] is not None  # its two closes are fine
    assert broken["rsi_14"] is None and broken["new_high_252_recent"] is None
    for bad in (0.0, -5.0):  # a non-positive close is bad data, never a -105% "rise"
        series = list(_walk(400))
        series[-21] = bad
        ext = entry_extension(_bars(series), timeframe="1d")
        assert ext["return_20d_pct"] is None and ext["rsi_14"] is None, bad
        assert ext["new_high_252_recent"] is None, bad


@pytest.mark.parametrize(
    "frame",
    [None, pd.DataFrame(), pd.DataFrame({"close": ["a", "b"]}), "bars", {"close": [1, 2]}],
)
def test_entry_extension_never_raises(frame) -> None:
    ext = entry_extension(frame, timeframe="1d")
    assert ext["return_20d_pct"] is None and ext["rsi_14"] is None
    assert ext["new_high_252_recent"] is None


def test_an_intraday_frame_has_no_session_inputs() -> None:
    for timeframe in ("15m", "1h", "1w", "", None):
        ext = entry_extension(_bars(_walk()), timeframe=timeframe)
        assert (ext["return_20d_pct"], ext["rsi_14"], ext["new_high_252_recent"]) == (
            None,
            None,
            None,
        ), timeframe


# -- the screener stores the inputs, nested, and decides nothing on them ------------------


def _snapshot(frame, *, timeframe="1d", extension=None, monkeypatch=None):
    from app.models.signal import Signal, SignalAction
    from app.screener.filters import FilterOutcome, build_market_context
    from app.screener.scan_promotion import _diagnostic_intelligence
    from app.screener.service_snapshots import snapshot_from_signal

    class _Status(dict):
        def __missing__(self, key):
            return None

    price = float(frame["close"].iloc[-1])
    quote = SimpleNamespace(last_execution=price, ask=price, bid=price, timestamp=None)
    signal = Signal(
        symbol="AAPL",
        strategy_name="trend_following",
        action=SignalAction.BUY,
        rationale="t",
        price=price,
        stop_loss=price * 0.95,
        take_profit=price * 1.1,
        metadata={"timeframe": timeframe, "risk_reward_ratio": 2.0, "style": "trend"},
    )
    context = build_market_context(frame, quote=quote, signal=signal)
    if extension is not None:
        context.extension = extension
    service = SimpleNamespace(
        _compute_risk_reward=lambda s: 2.0,
        _execution_blockers=lambda **kw: [],
        settings=SimpleNamespace(allowed_instruments=["AAPL"]),
    )
    return snapshot_from_signal(
        service,
        signal,
        quote=quote,
        timeframe=timeframe,
        context=context,
        intelligence=_diagnostic_intelligence(
            context, {"measurements": {"market_regime_score": 0.8}}
        ),
        market_data_status=_Status(verified=True),
        filter_outcome=FilterOutcome(passed=True),
        backtest_snapshot={"validated": False},
        ranking={
            "final_score": 90.0,
            "actionability": "alert",
            "direction_label": "buy",
            "confidence_label": "strong",
            "score_breakdown": {},
        },
        freshness="fresh",
    )


def test_the_snapshot_nests_the_signal_bars_extension(tmp_path) -> None:
    frame = _bars(_walk())
    snapshot = _snapshot(frame)
    assert snapshot.metadata[EXTENSION_FIELD] == entry_extension(frame, timeframe="1d")
    assert snapshot.metadata["trade_plan"]["timing_label"] == "immediate"
    assert snapshot.metadata["backtest_validated"] is False
    for key in ("return_20d_pct", "rsi_14", "new_high_252_recent"):
        assert key not in snapshot.metadata  # nested only


def test_an_intraday_snapshot_has_unknown_extension(tmp_path) -> None:
    snapshot = _snapshot(_bars(_walk()), timeframe="15m")
    ext = snapshot.metadata[EXTENSION_FIELD]
    assert ext["timeframe"] == "15m" and ext["return_20d_pct"] is None
    # A context measured on another timeframe is never passed off as this one's.
    mixed = _snapshot(
        _bars(_walk()), timeframe="15m", extension=entry_extension(_bars(_walk()), timeframe="1d")
    )
    assert mixed.metadata[EXTENSION_FIELD]["rsi_14"] is None


def test_the_extension_changes_no_decision_and_no_learning_feature(tmp_path) -> None:
    from app.learning.service import LearningService

    frame = _bars(_walk())
    chased = {
        "timeframe": "1d",
        "return_20d_pct": 40.0,
        "rsi_14": 90.0,
        "new_high_252_recent": True,
    }
    a = _snapshot(frame, extension=chased).model_dump()
    b = _snapshot(frame, extension={}).model_dump()
    for snap in (a, b):
        snap["metadata"].pop(EXTENSION_FIELD)
        for key in ("generated_at", "signal_generated_at"):
            snap.pop(key, None)
    assert a == b  # score, tradable, execution_ready, plan, blockers: all identical
    with_ext = _snapshot(frame).model_dump()
    without = _snapshot(frame).model_dump()
    without["metadata"].pop(EXTENSION_FIELD)
    features = LearningService._features_from_payload
    assert features(with_ext) == features(without)  # nested: never a model feature


def test_build_market_context_measures_the_extension_on_the_signals_timeframe() -> None:
    from app.models.signal import Signal, SignalAction
    from app.screener.filters import build_market_context

    frame = _bars(_walk())
    price = float(frame["close"].iloc[-1])
    quote = SimpleNamespace(last_execution=price, ask=price, bid=price)
    for timeframe, known in (("1d", True), ("15m", False)):
        signal = Signal(
            symbol="AAPL",
            strategy_name="x",
            action=SignalAction.BUY,
            rationale="t",
            metadata={"timeframe": timeframe},
        )
        context = build_market_context(frame, quote=quote, signal=signal)
        assert (context.extension["rsi_14"] is not None) is known
        assert "rsi_14" not in context.measurements  # the measurements (features) are as before
