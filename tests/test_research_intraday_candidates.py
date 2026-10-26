"""Intraday research candidates (app/research/intraday): deterministic signal logic, the
session features they read, and the offline harness that judges them with the production
walk-forward and verdict code. Research only: none of them may reach the live registry."""

from __future__ import annotations

from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from app.models.signal import SignalAction
from app.research.intraday import CANDIDATE_VARIANTS, build_candidate
from app.research.intraday.harness import (
    pooled_verdict,
    production_defaults,
    resample_session_bars,
    run_symbol,
    trade_statistics,
)
from app.research.intraday.session import hold_bars_until, session_view
from app.strategies import STRATEGY_REGISTRY, STRATEGY_SPECS

NY = "America/New_York"


def _bars(day: str, rows: list[tuple[float, float, float, float, float]], *, minutes: int = 5, start: str = "09:30") -> pd.DataFrame:
    """Consecutive bars (open, high, low, close, volume) from ``start`` ET on ``day``."""

    first = pd.Timestamp(f"{day} {start}", tz=NY)
    stamps = [first + pd.Timedelta(minutes=minutes * i) for i in range(len(rows))]
    frame = pd.DataFrame(rows, columns=["open", "high", "low", "close", "volume"])
    frame.insert(0, "timestamp", pd.DatetimeIndex(stamps).tz_convert("UTC"))
    return frame


def _flat_session(day: str, price: float = 100.0, volume: float = 1_000.0, bars: int = 78) -> pd.DataFrame:
    return _bars(day, [(price, price + 0.05, price - 0.05, price, volume)] * bars)


def _history(days: list[str], price: float = 100.0) -> pd.DataFrame:
    return pd.concat([_flat_session(day, price) for day in days], ignore_index=True)


PRIOR_DAYS = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16"]  # Mon-Fri
TODAY = "2026-10-19"  # Monday: the previous weekday is Friday 10-16


def _with_today(today: pd.DataFrame, prior_price: float = 100.0) -> pd.DataFrame:
    return pd.concat([_history(PRIOR_DAYS, prior_price), today], ignore_index=True)


# --------------------------------------------------------------------------- session features


def test_session_view_ignores_extended_hours_bars_and_never_signals_on_one():
    pre = _bars(TODAY, [(100, 101, 99, 100, 5_000)] * 6, start="08:00")
    regular = _bars(TODAY, [(100, 100.2, 99.9, 100.1, 1_000)] * 3)
    post = _bars(TODAY, [(100, 100, 100, 100, 50)], start="16:00")
    frame = pd.concat([_history(PRIOR_DAYS), pre, regular], ignore_index=True)
    view = session_view(frame, bar_minutes=5)
    assert view is not None
    assert list(view.minute) == [570, 575, 580]  # pre-market bars are not part of the session
    assert len(view.prior) == 5
    assert session_view(pd.concat([frame, post], ignore_index=True), bar_minutes=5) is None


def test_adjacent_prior_close_requires_the_previous_weekday():
    monday = _with_today(_bars(TODAY, [(101, 101.5, 100.5, 101.2, 1_000)]))
    assert session_view(monday, bar_minutes=5).adjacent_prior_close == pytest.approx(100.0)
    # A walk-forward fold drops the embargo day: Thursday's frame then ends on Tuesday.
    gapped = pd.concat(
        [_history(["2026-10-12", "2026-10-13"]), _bars("2026-10-15", [(101, 102, 100, 101, 1_000)])],
        ignore_index=True,
    )
    assert session_view(gapped, bar_minutes=5).adjacent_prior_close is None


def test_relative_volume_is_time_of_day_aware():
    today = _bars(TODAY, [(100, 100.1, 99.9, 100, 2_000), (100, 100.1, 99.9, 100, 500)])
    view = session_view(_with_today(today), bar_minutes=5)
    rvol = view.cumulative_relative_volume()
    assert rvol[0] == pytest.approx(2.0)  # 2,000 against 1,000 through 09:30
    assert rvol[1] == pytest.approx(1.25)  # 2,500 against 2,000 through 09:35
    thin = session_view(pd.concat([_history(PRIOR_DAYS[-2:]), today], ignore_index=True), bar_minutes=5)
    assert np.isnan(thin.cumulative_relative_volume()).all()  # under 3 prior sessions


def test_vwap_and_sigma():
    today = _bars(TODAY, [(10, 12, 8, 10, 100), (20, 22, 18, 20, 100)])
    vwap, sigma = session_view(_with_today(today), bar_minutes=5).vwap_and_sigma()
    assert vwap.tolist() == pytest.approx([10.0, 15.0])
    assert sigma.tolist() == pytest.approx([0.0, 5.0])


@pytest.mark.parametrize(
    ("minute", "bar", "expected"),
    [(600, 5, 68), (600, 10, 33), (600, 15, 21), (930, 5, 2), (940, 5, 0)],
)
def test_hold_bars_until_reaches_the_1550_time_stop(minute, bar, expected):
    # Fill at signal + 2 bars (one bar late); exit at the open of the last bar starting <= 15:50.
    assert hold_bars_until(minute, bar_minutes=bar) == expected


# --------------------------------------------------------------------------- strategies


def _orb_day(*, range_volume: float = 1_500.0, breakout_close: float = 101.2) -> pd.DataFrame:
    opening = [(100.0, 101.0, 99.0, 100.5, range_volume)] * 6  # 09:30-09:55: range 99-101
    quiet = [(100.5, 100.8, 100.2, 100.6, 1_000.0)] * 2
    breakout = [(100.6, breakout_close + 0.1, 100.5, breakout_close, 2_000.0)]
    return _bars(TODAY, opening + quiet + breakout)


def test_orb_buys_the_first_close_above_the_range_with_the_far_side_as_stop():
    strategy = build_candidate("research_orb30", timeframe="5m")
    signal = strategy.generate_signal(_with_today(_orb_day()), "spy")
    assert signal is not None and signal.action == SignalAction.BUY
    assert signal.symbol == "SPY" and signal.strategy_name == "research_orb30"
    assert signal.price == pytest.approx(101.2)
    assert signal.stop_loss == pytest.approx(99.0)
    assert signal.take_profit == pytest.approx(101.2 + 2 * 2.2)
    assert signal.metadata["signal_role"] == "entry_long"
    assert signal.metadata["research_only"] is True
    assert signal.metadata["max_hold_bars"] == hold_bars_until(9 * 60 + 70, bar_minutes=5)


def test_orb_rejects_a_quiet_open_a_second_breakout_and_an_unfinished_range():
    strategy = build_candidate("research_orb30", timeframe="5m")
    assert strategy.generate_signal(_with_today(_orb_day(range_volume=900.0)), "SPY") is None
    again = pd.concat(
        [_orb_day(), _bars(TODAY, [(101.2, 101.6, 101.1, 101.5, 1_000.0)], start="10:15")],
        ignore_index=True,
    )
    assert strategy.generate_signal(_with_today(again), "SPY") is None
    early = _bars(TODAY, [(100.0, 103.0, 99.0, 102.5, 3_000.0)] * 3)
    assert strategy.generate_signal(_with_today(early), "SPY") is None


def test_orb_short_mirror_sells_a_breakdown_with_the_range_high_as_stop():
    breakdown = _bars(
        TODAY,
        [(100.0, 101.0, 99.0, 99.5, 1_500.0)] * 6
        + [(99.5, 99.8, 99.2, 99.4, 1_000.0)] * 2
        + [(99.4, 99.5, 98.7, 98.8, 2_000.0)],
    )
    long_signal = build_candidate("research_orb30", timeframe="5m").generate_signal(_with_today(breakdown), "SPY")
    short = build_candidate("research_orb30", timeframe="5m", direction="short")
    signal = short.generate_signal(_with_today(breakdown), "SPY")
    assert long_signal is None
    assert signal is not None and signal.action == SignalAction.SELL
    assert signal.metadata["signal_role"] == "entry_short"
    assert signal.stop_loss == pytest.approx(101.0)
    assert signal.take_profit == pytest.approx(98.8 - 2 * 2.2)


def test_gap_go_and_gap_fade_read_the_first_bar_after_an_adjacent_session():
    gap_up_green = _bars(TODAY, [(101.5, 102.2, 101.3, 102.0, 1_000.0)])
    signal = build_candidate("research_gap_go", timeframe="5m").generate_signal(_with_today(gap_up_green), "X")
    assert signal is not None and signal.stop_loss == pytest.approx(101.3)
    assert signal.take_profit == pytest.approx(102.0 + 2 * 0.7)
    assert build_candidate("research_gap_fade", timeframe="5m").generate_signal(_with_today(gap_up_green), "X") is None

    gap_down_green = _bars(TODAY, [(98.0, 98.7, 97.9, 98.6, 1_000.0)])
    fade = build_candidate("research_gap_fade", timeframe="5m").generate_signal(_with_today(gap_down_green), "X")
    assert fade is not None and fade.take_profit == pytest.approx(100.0)  # the gap fill
    assert fade.stop_loss == pytest.approx(97.9)
    second_bar = pd.concat([gap_up_green, _bars(TODAY, [(102, 103, 101.9, 102.9, 1_000.0)], start="09:35")])
    assert build_candidate("research_gap_go", timeframe="5m").generate_signal(_with_today(second_bar), "X") is None
    gapped = pd.concat([_history(["2026-10-12", "2026-10-13"]), _bars("2026-10-15", [(101.5, 102.2, 101.3, 102.0, 1_000.0)])])
    assert build_candidate("research_gap_go", timeframe="5m").generate_signal(gapped.reset_index(drop=True), "X") is None


def _momentum_day(last_close: float, *, early_high_at: int | None = None) -> pd.DataFrame:
    """09:30-11:55 a steady climb whose closes stay just under the prior high, then at 12:00 a
    close at ``last_close`` above every earlier high. ``early_high_at`` makes that earlier bar
    close above the prior highs too (a setup before the 12:00 one)."""

    rows = [(100 + 0.05 * i, 100.1 + 0.05 * i, 99.95 + 0.05 * i, 100.03 + 0.05 * i, 2_000.0) for i in range(30)]
    if early_high_at is not None:
        i = early_high_at
        rows[i] = (100 + 0.05 * i, 101.55, 99.95 + 0.05 * i, 101.5, 2_000.0)
    rows.append((101.5, last_close + 0.05, 101.4, last_close, 3_000.0))
    return _bars(TODAY, rows)


def test_rvol_momentum_needs_volume_a_day_gain_vwap_and_a_new_high():
    strategy = build_candidate("research_rvol_momentum", timeframe="5m")
    signal = strategy.generate_signal(_with_today(_momentum_day(101.8)), "X")
    assert signal is not None and signal.action == SignalAction.BUY
    assert signal.stop_loss < signal.price < signal.take_profit
    assert signal.metadata["rvol"] > 1.5 and signal.metadata["day_change"] == pytest.approx(0.018)
    # Not the session's first setup: 11:35 already made a new high, up 1.5%, on volume.
    assert strategy.generate_signal(_with_today(_momentum_day(101.8, early_high_at=25)), "X") is None
    assert strategy.generate_signal(_with_today(_momentum_day(101.8).assign(volume=500.0)), "X") is None
    assert strategy.generate_signal(_with_today(_momentum_day(101.8), prior_price=101.0), "X") is None


def test_vwap_reversion_buys_a_green_bar_after_a_probe_below_the_band():
    calm = [(100.0, 100.2, 99.8, 100.0 + 0.1 * ((-1) ** i), 1_000.0) for i in range(36)]
    probe = [(99.2, 99.7, 98.0, 99.6, 1_000.0)]  # 12:30 bar: low far below VWAP - 2 sigma, closes green
    signal = build_candidate("research_vwap_reversion", timeframe="5m").generate_signal(
        _with_today(_bars(TODAY, calm + probe)), "SPY"
    )
    assert signal is not None
    vwap = signal.metadata["vwap"]
    assert signal.take_profit == pytest.approx(vwap)
    assert signal.price - signal.stop_loss == pytest.approx(vwap - signal.price)
    red = [(99.8, 99.9, 98.0, 99.3, 1_000.0)]
    assert build_candidate("research_vwap_reversion", timeframe="5m").generate_signal(
        _with_today(_bars(TODAY, calm + red)), "SPY"
    ) is None


def test_late_day_momentum_fires_only_on_the_bar_that_fills_at_1500():
    rows = [(100.0 + 0.01 * i, 100.05 + 0.01 * i, 99.95 + 0.01 * i, 100.01 + 0.01 * i, 1_000.0) for i in range(65)]
    day = _bars(TODAY, rows)  # last bar starts 14:50 (5m): fills at 15:00 one bar late
    signal = build_candidate("research_late_day_momentum", timeframe="5m").generate_signal(_with_today(day), "X")
    assert signal is not None and signal.metadata["max_hold_bars"] == 10
    assert build_candidate("research_late_day_momentum", timeframe="5m").generate_signal(
        _with_today(day.iloc[:-1]), "X"
    ) is None
    down = _with_today(day, prior_price=101.0)  # below the prior close: no trend day
    assert build_candidate("research_late_day_momentum", timeframe="5m").generate_signal(down, "X") is None


def test_candidates_stay_out_of_the_live_registry():
    live_names = set(STRATEGY_REGISTRY) | {spec.name for spec in STRATEGY_SPECS}
    assert not live_names & set(CANDIDATE_VARIANTS)
    assert all(name.startswith("research_") for name in CANDIDATE_VARIANTS)


# --------------------------------------------------------------------------- harness


def test_resample_session_bars_anchors_at_the_open():
    five = _bars(TODAY, [(100 + i, 100.5 + i, 99.5 + i, 100.2 + i, 10.0) for i in range(6)])
    fifteen = resample_session_bars(five, "15m")
    assert len(fifteen) == 2
    first = fifteen.iloc[0]
    assert first["timestamp"] == pd.Timestamp(f"{TODAY} 09:30", tz=NY).tz_convert("UTC")
    assert (first["open"], first["high"], first["low"], first["close"], first["volume"]) == (
        100, 102.5, 99.5, 102.2, 30.0
    )


def test_production_defaults_never_instantiate_app_settings(monkeypatch):
    from app.runtime_settings import AppSettings

    def refuse(*_args, **_kwargs):
        raise AssertionError("AppSettings() reads .env")

    monkeypatch.setattr(AppSettings, "__init__", refuse)
    settings = production_defaults()
    assert settings.walk_forward_intraday_entry_delay_bars == 1
    assert settings.strategy_evidence_min_trades == 40


def _random_sessions(days: int, *, seed: int = 3) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    frames, price = [], 100.0
    for day in pd.bdate_range("2026-06-01", periods=days):
        rows = []
        for _ in range(26):
            close = price * (1 + rng.normal(0.0, 0.004))
            rows.append((price, max(price, close) * 1.002, min(price, close) * 0.998, close, float(rng.integers(500, 1500))))
            price = close
        frames.append(_bars(day.strftime("%Y-%m-%d"), rows, minutes=15))
    return pd.concat(frames, ignore_index=True)


def test_harness_runs_the_production_walk_forward_and_verdict():
    frame = _random_sessions(45)
    run = run_symbol(frame, symbol="syn", variant="research_vwap_reversion", timeframe="15m")
    summary = run.summary
    assert summary["fold_count"] >= 2 and summary["holdout_evaluated"]
    assert set(summary["cost_profiles"]) == {"alpaca", "stress", "gross"}
    oos = run.trades["etoro"]["oos"]
    assert len(oos) == summary["number_of_trades"] > 0
    assert np.mean([t["pnl_usd"] for t in oos]) == pytest.approx(summary["expectancy_usd"])
    assert len(run.trades["stress"]["holdout"]) == summary["cost_profiles"]["stress"]["holdout_trades"]
    gross, stress = (trade_statistics(run.trades[p]["oos"])["expectancy_r"] for p in ("gross", "stress"))
    assert gross > stress  # same fills, costs only subtract
    verdict = pooled_verdict([run, replace(run, symbol="SYN2")])
    assert verdict["symbols"] == 2 and verdict["oos_trades"] == 2 * summary["number_of_trades"]
    assert verdict["oos_expectancy_r"] == pytest.approx(summary["expectancy_usd"] / 100.0, abs=1e-4)
    assert set(verdict["cost_profiles"]) == {"alpaca", "stress"}  # the diagnostic never votes
    assert verdict["paper_gate_full"] == (verdict["passed"] and verdict["paper_passed"])


def test_live_calibration_units_use_the_registry_spec_and_skip_missing_timeframes():
    from app.research.intraday.study import _strategy_for, unit_supported

    assert unit_supported("research_orb30", "10m")
    assert unit_supported("live:rsi_reversal", "10m")
    assert not unit_supported("live:opening_range_breakout_retest", "10m")  # no 10m spec
    strategy = _strategy_for("live:rsi_reversal", "10m")
    assert strategy.name == "rsi_reversal" and strategy.timeframe == "10m"
    assert _strategy_for("research_orb30", "5m") is None
