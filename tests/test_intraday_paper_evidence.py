"""Honest minute-bar walk-forward evidence (operator plan, Stage 1, 2026-10-20).

* 10m is backtested (as an intraday timeframe) and 1m stays out of the scheduler;
* minute-bar backtests refetch full history and never write a row from a fallback frame or
  with fewer than two folds;
* their entries fill a bar late, the live scan-to-order delay;
* on the main paper account a minute-bar strategy also needs positive OOS and holdout
  expectancy under both the Alpaca and the stress cost profile, on top of the fee-inclusive
  verdict: the profiles only tighten it (fee-band trades wait for their own account), and
  every eToro use and every 1h/1d/1w verdict keeps reading the fee-inclusive numbers.
"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from app.automation.unattended import PaperAutoTradingService
from app.backtesting import batch as batch_module
from app.backtesting.batch import BatchBacktestService
from app.backtesting.cost_model import CostModel
from app.backtesting.engine import BacktestEngine, EngineConfig
from app.backtesting.metrics import bars_per_year_for
from app.backtesting.strategy_selection import strategy_kwargs_for, strategy_specs_for
from app.backtesting.venue_costs import COST_PROFILES_FIELD, venue_cost_models
from app.performance.strategy_evidence import (
    VERDICTS_KEY,
    compute_verdicts,
    evidence_blocker,
    refresh_verdicts,
)
from app.runtime_settings import AppSettings
from app.storage.db import Database
from app.storage.repositories import BacktestRepository, RunLogRepository, RuntimeStateRepository
from app.strategies import get_strategy
from app.utils.time import utc_now
from tests.conftest import make_settings


def _sessions(days: int, *, minutes: int, seed: int) -> pd.DataFrame:
    """Regular-hours bars, 13:30-20:00 UTC (09:30-16:00 EDT), on weekdays from 2026-04-06."""

    rng = np.random.default_rng(seed)
    rows, price = [], 100.0
    for day in pd.bdate_range("2026-04-06", periods=days, tz="UTC"):
        for step in range(390 // minutes):
            change = rng.normal(0.0, 0.002)
            open_, close = price, price * (1 + change)
            rows.append(
                {
                    "timestamp": day + pd.Timedelta(hours=13, minutes=30 + step * minutes),
                    "open": open_,
                    "high": max(open_, close) * 1.0015,
                    "low": min(open_, close) * 0.9985,
                    "close": close,
                    "volume": float(rng.integers(5_000, 30_000)),
                }
            )
            price = close
    return pd.DataFrame(rows)


def _daily(bars: int) -> pd.DataFrame:
    stamps = pd.bdate_range("2026-01-05", periods=bars, tz="UTC") + pd.Timedelta(hours=4)
    close = np.linspace(100.0, 120.0, bars)
    return pd.DataFrame(
        {
            "timestamp": stamps,
            "open": close,
            "high": close * 1.01,
            "low": close * 0.99,
            "close": close,
            "volume": 1e6,
        }
    )


class FakeMarketData:
    def __init__(self, frames: dict[tuple[str, str], pd.DataFrame]) -> None:
        self.frames = frames
        self.calls: list[tuple[str, str, bool]] = []

    def get_history(
        self, symbol, *, timeframe, bars, provider=None, force_refresh=False, backtest=False
    ):
        self.calls.append((symbol, timeframe, backtest))
        return self.frames[(symbol, timeframe)].copy()


def _service(tmp_path, frames, **overrides):
    settings = make_settings(tmp_path, walk_forward_intraday_variants=[], **overrides)
    db = Database(settings)
    db.initialize()
    service = BatchBacktestService(
        settings=settings,
        market_data_engine=FakeMarketData(frames),
        backtest_repository=BacktestRepository(db),
        run_log_repository=RunLogRepository(db),
    )
    return service, db


def _rows(db) -> dict[str, dict]:
    with db.connect() as connection:
        rows = connection.execute(
            "SELECT strategy_name, file_path, metrics_json FROM backtests"
        ).fetchall()
    return {f"{r['strategy_name']} {r['file_path']}": json.loads(r["metrics_json"]) for r in rows}


def _logs(db, event: str) -> list[dict]:
    with db.connect() as connection:
        rows = connection.execute(
            "SELECT payload_json FROM run_logs WHERE event_type = ?", (event,)
        ).fetchall()
    return [json.loads(r["payload_json"]) for r in rows]


# --- 10m / 1m in the scheduler --------------------------------------------------------------


def test_scheduler_backtests_10m_not_1m_and_fills_minute_bars_a_bar_late() -> None:
    # The code defaults (production has no .env.example; tests and local runs load its "1d").
    fields = AppSettings.model_fields
    assert fields["backtest_scheduler_timeframes"].default_factory() == ["1d", "15m", "5m", "10m"]
    assert fields["walk_forward_intraday_entry_delay_bars"].default == 1


# --- data: full history, no 0-fold rows -----------------------------------------------------


def test_short_minute_frames_refetch_and_write_no_row_while_daily_is_unchanged(tmp_path) -> None:
    # A live scan's ~320 cached 5m bars (4 sessions) fit no fold. The batch now asks for the
    # backtest fetch (refetched, cached nowhere) for minute bars, and a run that still fits no
    # fold writes no row, so the symbol's last real row keeps counting. 1d is unchanged.
    frames = {("AAA", "5m"): _sessions(4, minutes=5, seed=1), ("AAA", "1d"): _daily(60)}
    service, db = _service(tmp_path, frames)

    service.run(
        symbols=["AAA"],
        timeframes=["5m", "1d"],
        strategy_names=["rsi_vwap_ema_confluence"],
        provider="test",
    )

    assert service.market_data.calls == [("AAA", "5m", True), ("AAA", "1d", False)]
    assert list(_rows(db)) == ["rsi_vwap_ema_confluence test:1d:AAA:walk_forward_oos"]
    assert _logs(db, "backtest_oos_row_skipped") == [
        {
            "symbol": "AAA",
            "strategy_name": "rsi_vwap_ema_confluence",
            "timeframe": "5m",
            "bars": 4 * 78,
            "folds": 0,
            "reason": "too_few_folds",
        }
    ]


def test_minute_rows_from_a_fallback_frame_or_one_fold_are_not_written(tmp_path) -> None:
    # yfinance serves 30 days of 5m: saved under the requested provider's path, its 1-fold
    # (or, for 15m's 60 days, ~5-fold) row would replace the symbol's ~14-fold one.
    fallback = _sessions(34, minutes=5, seed=3)
    fallback.attrs["used_fallback"] = True
    frames = {("AAA", "5m"): fallback, ("BBB", "5m"): _sessions(22, minutes=5, seed=4)}
    service, db = _service(tmp_path, frames)

    service.run(
        symbols=["AAA", "BBB"], timeframes=["5m"], strategy_names=["vwap_reclaim"], provider="t"
    )

    assert _rows(db) == {}
    skipped = _logs(db, "backtest_oos_row_skipped")
    assert [(e["symbol"], e["folds"], e["reason"]) for e in skipped] == [
        ("AAA", 3, "fallback_provider"),
        ("BBB", 1, "too_few_folds"),
    ]


def test_a_failing_profile_run_still_writes_the_etoro_row(tmp_path, monkeypatch) -> None:
    def broken(_summary):
        raise RuntimeError("profile boom")

    monkeypatch.setattr(batch_module, "profile_metrics", broken)
    service, db = _service(tmp_path, {("AAA", "5m"): _sessions(34, minutes=5, seed=11)})

    service.run(symbols=["AAA"], timeframes=["5m"], strategy_names=["vwap_reclaim"], provider="t")

    row = _rows(db)["vwap_reclaim t:5m:AAA:walk_forward_oos"]
    assert row["number_of_trades"] > 10 and COST_PROFILES_FIELD not in row  # paper fails closed
    assert _logs(db, "backtest_cost_profiles_failed") == [
        {
            "symbol": "AAA",
            "strategy_name": "vwap_reclaim",
            "timeframe": "5m",
            "error": "RuntimeError: profile boom",
        }
    ]


def test_the_deadline_is_checked_before_the_long_minute_fetch(tmp_path, monkeypatch) -> None:
    # Clock reads: the pass start, the symbol, the one 1d spec, then the 5m fetch is due.
    clock = iter([0.0, 0.0, 0.0, 100.0])
    monkeypatch.setattr(batch_module.time, "monotonic", lambda: next(clock, 1e9))
    frames = {("AAA", "1d"): _daily(60), ("AAA", "5m"): _sessions(4, minutes=5, seed=1)}
    service, _ = _service(tmp_path, frames)

    summary = service.run(
        symbols=["AAA"],
        timeframes=["1d", "5m"],
        strategy_names=["rsi_vwap_ema_confluence"],
        provider="t",
        deadline_seconds=50.0,
    )

    assert service.market_data.calls == [("AAA", "1d", False)]  # the 5m fetch never went out
    assert summary.stopped_mid_symbol and summary.resume_unit == 1
    assert summary.errors == ["backtest_deadline_exceeded"]


# --- latency: a bar late on minute bars only ------------------------------------------------


def test_entry_delay_applies_to_minute_bar_evidence_only(tmp_path, monkeypatch) -> None:
    seen: list[tuple[float, int]] = []

    class Recording(BacktestEngine):
        def __init__(self, repository=None, *, config=None):
            super().__init__(repository, config=config)
            seen.append((config.bars_per_year, config.entry_delay_bars))

    monkeypatch.setattr(batch_module, "BacktestEngine", Recording)
    frames = {
        ("AAA", "5m"): _sessions(4, minutes=5, seed=1),
        ("AAA", "10m"): _sessions(4, minutes=10, seed=1),
        ("AAA", "1h"): _sessions(40, minutes=60, seed=1),
        ("AAA", "1d"): _daily(60),
    }
    timeframes = ["5m", "10m", "1h", "1d"]
    delays = {}
    for setting in (1, 0):
        seen.clear()
        service, _ = _service(
            tmp_path / str(setting), frames, walk_forward_intraday_entry_delay_bars=setting
        )
        service.run(
            symbols=["AAA"],
            timeframes=timeframes,
            strategy_names=["rsi_vwap_ema_confluence"],
            provider="t",
        )
        delays[setting] = {bars: {d for b, d in seen if b == bars} for bars in {b for b, _ in seen}}

    per_tf = {tf: bars_per_year_for(tf) for tf in timeframes}
    assert {tf: delays[1][bars] for tf, bars in per_tf.items()} == {
        "5m": {1},
        "10m": {1},
        "1h": {0},
        "1d": {0},
    }
    assert {tf: delays[0][bars] for tf, bars in per_tf.items()} == {
        "5m": {0},
        "10m": {0},
        "1h": {0},
        "1d": {0},
    }


# --- venue costs: Alpaca and stress profiles on the same signals ----------------------------


def test_stress_profile_is_alpaca_plus_about_eight_bps_a_round_trip() -> None:
    def round_trip_bps(
        model: CostModel, notional: float = 10_000.0, shares: float = 100.0
    ) -> float:
        dollars = (
            notional * model.spread_bps / 10_000
            + 2 * model.slippage_usd(notional_usd=notional)
            + model.regulatory_sell_fee_usd(notional_usd=notional, quantity=shares)
        )
        return dollars / notional * 10_000

    profiles = venue_cost_models()
    assert profiles["alpaca"] == CostModel.alpaca_equities()
    assert round_trip_bps(profiles["alpaca"]) == pytest.approx(4.3, abs=0.05)
    assert round_trip_bps(profiles["stress"]) == pytest.approx(12.3, abs=0.05)


class Counting:
    def __init__(self, inner) -> None:
        self.inner, self.name, self.calls = inner, inner.name, 0

    def generate_signal(self, window, symbol):
        self.calls += 1
        return self.inner.generate_signal(window, symbol)


def test_minute_rows_carry_both_profiles_from_one_evaluation_of_the_strategy(
    tmp_path, monkeypatch
) -> None:
    frame = _sessions(34, minutes=5, seed=11)
    service, db = _service(tmp_path, {("AAA", "5m"): frame, ("BTCUSD", "5m"): frame})
    built: list[Counting] = []

    def counting_strategy(name, **kwargs):
        built.append(Counting(get_strategy(name, **kwargs)))
        return built[-1]

    monkeypatch.setattr(batch_module, "get_strategy", counting_strategy)
    service.run(
        symbols=["AAA", "BTCUSD"], timeframes=["5m"], strategy_names=["vwap_reclaim"], provider="t"
    )

    rows = _rows(db)
    equity, crypto = (
        rows["vwap_reclaim t:5m:AAA:walk_forward_oos"],
        rows["vwap_reclaim t:5m:BTCUSD:walk_forward_oos"],
    )
    assert COST_PROFILES_FIELD not in crypto  # an equities cost model
    profiles = equity[COST_PROFILES_FIELD]
    assert list(profiles) == ["alpaca", "stress"]
    # eToro's 10 bps spread and $1 a leg, and the stress add-on, both cost more than Alpaca.
    assert equity["expectancy_usd"] < profiles["alpaca"]["expectancy_usd"]
    assert profiles["stress"]["expectancy_usd"] < profiles["alpaca"]["expectancy_usd"]

    # The profiles replayed the eToro run's signals: one evaluation per bar, as one plain run.
    spec = strategy_specs_for(service.settings, timeframe="5m", requested={"vwap_reclaim"})[0]
    inner = get_strategy(spec.name, **strategy_kwargs_for(service.settings, spec))
    etoro = EngineConfig(
        initial_cash=10_000.0,
        risk_per_trade_pct=float(service.settings.max_risk_per_trade_pct),
        bars_per_year=bars_per_year_for("5m"),
        flatten_at_session_end=True,
        entry_delay_bars=1,
    )
    for name, model in venue_cost_models().items():
        fresh = Counting(inner)
        direct = service._run_strategy(
            engine=BacktestEngine(config=replace(etoro, cost_model=model)),
            symbol="AAA",
            strategy=fresh,
            history=frame.copy(),
            timeframe="5m",
            provider="t",
            walk_forward=True,
            persist=False,
        )
        # Replay == a fresh evaluation, pinned to that run's own OOS and holdout numbers.
        assert profiles[name] == {
            "number_of_trades": direct["number_of_trades"],
            "expectancy_usd": round(direct["expectancy_usd"], 4),
            "holdout_trades": direct["holdout_trades"],
            "holdout_expectancy_usd": direct["holdout_expectancy_usd"],
        }, name
        assert direct["number_of_trades"] > 10 and direct["holdout_trades"] > 5
    assert built[0].calls == fresh.calls > 1000


# --- verdicts ---------------------------------------------------------------------------------


def _row(
    db,
    *,
    symbol,
    strategy,
    tf,
    trades,
    expectancy,
    holdout_trades,
    holdout_expectancy,
    profiles=None,
):
    metrics = {
        "number_of_trades": trades,
        "expectancy_usd": expectancy,
        "holdout_trades": holdout_trades,
        "holdout_expectancy_usd": holdout_expectancy,
        "out_of_sample": True,
    }
    if profiles is not None:
        metrics[COST_PROFILES_FIELD] = {
            name: {
                "number_of_trades": trades,
                "expectancy_usd": oos,
                "holdout_trades": holdout_trades,
                "holdout_expectancy_usd": holdout,
            }
            for name, (oos, holdout) in profiles.items()
        }
    now = utc_now().isoformat()
    with db.connect() as connection:
        connection.execute(
            "INSERT INTO backtests (id, symbol, strategy_name, file_path, started_at, completed_at, metrics_json, trades_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                f"bt_{symbol}_{strategy}_{tf}",
                symbol,
                strategy,
                f"alpaca:{tf}:{symbol}:walk_forward_oos",
                now,
                now,
                json.dumps(metrics),
                "[]",
            ),
        )


def _verdict_db(tmp_path):
    db = Database(make_settings(tmp_path))
    db.initialize()
    for symbol in ("AAPL", "MSFT", "NVDA"):
        # Loses after eToro's fees, wins on Alpaca's and on the stress costs.
        _row(
            db,
            symbol=symbol,
            strategy="vwap_reclaim",
            tf="5m",
            trades=20,
            expectancy=-4.0,
            holdout_trades=5,
            holdout_expectancy=-1.0,
            profiles={"alpaca": (6.0, 3.0), "stress": (1.5, 0.5)},
        )
        # Wins on Alpaca's costs only.
        _row(
            db,
            symbol=symbol,
            strategy="rsi_reversal",
            tf="15m",
            trades=20,
            expectancy=-3.0,
            holdout_trades=5,
            holdout_expectancy=-2.0,
            profiles={"alpaca": (4.0, 2.0), "stress": (-0.5, 1.0)},
        )
        # Passes with eToro's fees and on both venue profiles.
        _row(
            db,
            symbol=symbol,
            strategy="intraday_vwap_trend",
            tf="15m",
            trades=20,
            expectancy=3.0,
            holdout_trades=5,
            holdout_expectancy=1.0,
            profiles={"alpaca": (9.0, 5.0), "stress": (2.5, 0.5)},
        )
        # Rows written before the profiles existed.
        _row(
            db,
            symbol=symbol,
            strategy="ema_trend_stack",
            tf="10m",
            trades=20,
            expectancy=5.0,
            holdout_trades=5,
            holdout_expectancy=2.0,
        )
        # A daily key is judged as before, whatever its row carries.
        _row(
            db,
            symbol=symbol,
            strategy="momentum_breakout",
            tf="1d",
            trades=20,
            expectancy=5.0,
            holdout_trades=5,
            holdout_expectancy=2.0,
            profiles={"alpaca": (-1.0, -1.0), "stress": (-1.0, -1.0)},
        )
    return db


def test_minute_verdicts_add_a_paper_verdict_and_keep_the_etoro_fields(tmp_path) -> None:
    verdicts = compute_verdicts(_verdict_db(tmp_path), SimpleNamespace())

    fivem = verdicts["vwap_reclaim:5m"]
    assert {
        k: fivem[k]
        for k in (
            "passed",
            "reasons",
            "symbols",
            "oos_trades",
            "oos_expectancy_usd",
            "oos_expectancy_r",
            "holdout_trades",
            "holdout_expectancy_usd",
        )
    } == {
        "passed": False,
        "reasons": ["oos_expectancy_not_positive", "holdout_expectancy_not_positive"],
        "symbols": 3,
        "oos_trades": 60,
        "oos_expectancy_usd": -4.0,
        "oos_expectancy_r": -0.04,
        "holdout_trades": 15,
        "holdout_expectancy_usd": -1.0,
    }
    assert fivem["paper_passed"] and fivem["paper_reasons"] == []
    assert fivem["cost_profiles"] == {
        "alpaca": {
            "symbols": 3,
            "oos_trades": 60,
            "oos_expectancy_usd": 6.0,
            "holdout_trades": 15,
            "holdout_expectancy_usd": 3.0,
        },
        "stress": {
            "symbols": 3,
            "oos_trades": 60,
            "oos_expectancy_usd": 1.5,
            "holdout_trades": 15,
            "holdout_expectancy_usd": 0.5,
        },
    }
    assert verdicts["rsi_reversal:15m"]["paper_reasons"] == ["stress:oos_expectancy_not_positive"]
    assert not verdicts["rsi_reversal:15m"]["paper_passed"]
    tenm = verdicts["ema_trend_stack:10m"]
    assert tenm["passed"] and not tenm["paper_passed"]  # fails closed without profile rows
    assert tenm["paper_reasons"] == ["alpaca:no_cost_profile_rows", "stress:no_cost_profile_rows"]
    daily = verdicts["momentum_breakout:1d"]
    assert daily["passed"] and set(daily) == {
        "passed",
        "reasons",
        "symbols",
        "oos_trades",
        "oos_expectancy_usd",
        "oos_expectancy_r",
        "holdout_trades",
        "holdout_expectancy_usd",
    }
    # The same trade-count and R bars apply to each profile.
    strict = compute_verdicts(
        _verdict_db(tmp_path / "strict"), SimpleNamespace(strategy_evidence_min_expectancy_r=0.02)
    )
    assert strict["vwap_reclaim:5m"]["paper_reasons"] == ["stress:oos_expectancy_below_0.02R"]
    few = compute_verdicts(
        _verdict_db(tmp_path / "few"), SimpleNamespace(strategy_evidence_min_trades=61)
    )
    assert few["vwap_reclaim:5m"]["paper_reasons"] == [
        "alpaca:too_few_oos_trades",
        "stress:too_few_oos_trades",
    ]


def test_a_minute_verdict_stays_the_same_size_however_many_symbols_pool(tmp_path) -> None:
    def size(symbols: int) -> int:
        db = Database(make_settings(tmp_path / str(symbols)))
        db.initialize()
        for index in range(symbols):
            _row(
                db,
                symbol=f"S{index}",
                strategy="vwap_reclaim",
                tf="5m",
                trades=20,
                expectancy=-4.0,
                holdout_trades=5,
                holdout_expectancy=-1.0,
                profiles={"alpaca": (6.0, 3.0), "stress": (1.5, 0.5)},
            )
        return len(json.dumps(compute_verdicts(db, SimpleNamespace())["vwap_reclaim:5m"]))

    few, many = size(3), size(300)
    # Pooled numbers only, never per-symbol data: just the 9 counts gain two digits each.
    assert few < many <= few + 9 * 2 < 800


def test_only_the_paper_gate_reads_the_paper_verdict(tmp_path) -> None:
    db = _verdict_db(tmp_path)
    state = RuntimeStateRepository(db)
    refresh_verdicts(db, SimpleNamespace(), state)
    on = SimpleNamespace(require_strategy_oos_evidence=True)

    def gate(strategy, timeframe, **kwargs):
        return evidence_blocker(on, state, strategy=strategy, timeframe=timeframe, **kwargs)

    # Main paper account: a minute key needs the fee-inclusive pass AND both venue profiles.
    assert gate("intraday_vwap_trend", "15m", paper=True) is None
    assert gate("vwap_reclaim", "5m", paper=True) == "strategy_lacks_oos_evidence"  # fee-band
    assert gate("rsi_reversal", "15m", paper=True) == "strategy_lacks_oos_evidence"
    assert (
        gate("ema_trend_stack", "10m", paper=True) == "strategy_lacks_oos_evidence"
    )  # no profiles
    assert gate("momentum_breakout", "1d", paper=True) is None
    # Every other caller (the eToro mirror, its +0.03R bar, the hints) reads `passed` as before.
    assert gate("vwap_reclaim", "5m") == "strategy_lacks_oos_evidence"
    assert gate("ema_trend_stack", "10m") is None
    assert gate("ema_trend_stack", "10m", min_expectancy_r=0.03) is None  # +0.05R fee-inclusive
    assert gate("ema_trend_stack", "10m", min_expectancy_r=0.06) == "strategy_edge_below_bar"
    payload = json.loads(state.get(VERDICTS_KEY))
    assert sorted(payload["passing_since"]) == [
        "ema_trend_stack:10m",
        "intraday_vwap_trend:15m",
        "momentum_breakout:1d",
    ]  # streaks: `passed`
    # A cached minute verdict written before paper_passed existed fails the paper gate only.
    old = {
        VERDICTS_KEY: json.dumps({"verdicts": {"s:5m": {"passed": True}, "s:1d": {"passed": True}}})
    }
    for timeframe, paper_result in (("5m", "strategy_lacks_oos_evidence"), ("1d", None)):
        kwargs = {"strategy": "s", "timeframe": timeframe}
        assert (
            evidence_blocker(on, SimpleNamespace(get=old.get), paper=True, **kwargs) == paper_result
        )
        assert evidence_blocker(on, SimpleNamespace(get=old.get), **kwargs) is None


def test_the_main_paper_gate_only_tightens_on_minute_bars(tmp_path) -> None:
    # Operator decision 3 (2026-10-20): fee-band trades never run on the main paper account,
    # so the venue profiles can block a minute-bar candidate there but never let one in.
    settings = make_settings(tmp_path, require_strategy_oos_evidence=True)
    verdicts = {
        "vwap_reclaim:5m": {"passed": False, "paper_passed": True},  # fee-band
        "rsi_reversal:15m": {"passed": True, "paper_passed": False},
        "ema_trend_stack:10m": {"passed": True},  # written before the profiles existed
        "intraday_vwap_trend:15m": {"passed": True, "paper_passed": True},
        "momentum_breakout:1d": {"passed": True},
    }
    service = PaperAutoTradingService(
        settings=settings,
        proposal_service=None,
        execution_coordinator=None,
        automation=SimpleNamespace(execution_blockers=lambda: []),
        reconciliation=SimpleNamespace(account_verified=lambda: True),
        safety_state=SimpleNamespace(
            is_blacklisted=lambda _symbol: False, strategy_active=lambda _strategy: True
        ),
        executions=None,
        run_logs=None,
        notifier=None,
        alpaca_client=None,
        runtime_state=SimpleNamespace(get={VERDICTS_KEY: json.dumps({"verdicts": verdicts})}.get),
    )

    def blockers(strategy, timeframe, symbol="AAPL"):
        return service.candidate_proposal_blockers(
            SimpleNamespace(symbol=symbol, strategy_name=strategy, timeframe=timeframe)
        )

    lacks = ["strategy_lacks_oos_evidence"]
    assert settings.execution_mode == "paper" and not settings.enable_real_trading
    assert blockers("intraday_vwap_trend", "15m") == []
    assert blockers("vwap_reclaim", "5m") == lacks
    assert blockers("rsi_reversal", "15m") == lacks
    assert blockers("ema_trend_stack", "10m") == lacks
    assert blockers("momentum_breakout", "1d") == []
    # Crypto rows carry no venue profiles: crypto keeps the fee-inclusive verdict alone.
    assert blockers("ema_trend_stack", "10m", symbol="BTCUSD") == []
    assert blockers("vwap_reclaim", "5m", symbol="BTCUSD") == lacks


# --- regression: today's 5m/15m eToro verdicts with the new options off -----------------------

# compute_verdicts' eToro fields for this fixture at 1046d29, before the 2026-10-20 changes.
VERDICTS_AT_1046D29 = {
    "vwap_reclaim:5m": {
        "passed": False,
        "reasons": ["oos_expectancy_not_positive"],
        "symbols": 2,
        "oos_trades": 45,
        "oos_expectancy_usd": -10.3582,
        "oos_expectancy_r": -0.1036,
        "holdout_trades": 29,
        "holdout_expectancy_usd": 0.2438,
    },
    "vwap_reclaim:15m": {
        "passed": False,
        "reasons": [
            "too_few_oos_trades",
            "oos_expectancy_not_positive",
            "holdout_expectancy_not_positive",
        ],
        "symbols": 2,
        "oos_trades": 25,
        "oos_expectancy_usd": -18.8963,
        "oos_expectancy_r": -0.189,
        "holdout_trades": 13,
        "holdout_expectancy_usd": -8.8549,
    },
    "rsi_reversal:15m": {
        "passed": False,
        "reasons": ["too_few_oos_trades", "too_few_holdout_trades"],
        "symbols": 2,
        "oos_trades": 2,
        "oos_expectancy_usd": 22.9978,
        "oos_expectancy_r": 0.23,
        "holdout_trades": 1,
        "holdout_expectancy_usd": 28.0127,
    },
}


def test_todays_5m_15m_etoro_verdicts_reproduce_with_the_new_options_off(tmp_path) -> None:
    frames = {}
    for index, symbol in enumerate(("AAA", "BBB")):
        frames[(symbol, "5m")] = _sessions(34, minutes=5, seed=11 + index)
        frames[(symbol, "15m")] = _sessions(34, minutes=15, seed=21 + index)
    service, db = _service(tmp_path, frames, walk_forward_intraday_entry_delay_bars=0)
    service.run(
        symbols=["AAA", "BBB"], timeframes=["5m"], strategy_names=["vwap_reclaim"], provider="test"
    )
    service.run(
        symbols=["AAA", "BBB"],
        timeframes=["15m"],
        strategy_names=["vwap_reclaim", "rsi_reversal"],
        provider="test",
    )

    verdicts = compute_verdicts(db, service.settings)
    for key, expected in VERDICTS_AT_1046D29.items():
        got = {field: verdicts[key][field] for field in expected}
        assert got == pytest.approx(expected, abs=1e-3), key
