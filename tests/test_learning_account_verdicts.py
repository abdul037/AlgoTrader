"""Learning verdicts and their classification (app/learning_account/verdicts.py).

The pooling and judging are strategy_evidence.py's own helpers (imported, never copied; that
file is not edited). The pinned verdict below for fixed rows is the coupling's tripwire: a
main-side change to ``_pool`` / ``_judge`` / ``_timeframe`` shows up here."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.learning_account.verdicts import (
    FEE_BAND,
    LANE,
    LANE_MIN_EXPECTANCY_R,
    LANE_MIN_OOS_TRADES,
    classify,
    compute_learning_verdicts,
    fee_free_metrics,
    rank_key,
    stale,
)
from app.performance.strategy_evidence import compute_verdicts
from app.storage.repositories import BacktestRepository
from tests.learning_account_fakes import NOW, database, settings_for

FRESH = (NOW - timedelta(days=1)).isoformat()


def _row(repo, strategy, symbol, timeframe, *, trades, expectancy, holdout, holdout_expectancy, fees=None,
         holdout_fees=None, completed=FRESH, suffix=":walk_forward_oos", **extra):
    metrics = {"number_of_trades": trades, "expectancy_usd": expectancy, "holdout_trades": holdout,
               "holdout_expectancy_usd": holdout_expectancy, **extra}
    if fees is not None:
        metrics["etoro_fees_usd"] = fees
    if holdout_fees is not None:
        metrics["holdout_etoro_fees_usd"] = holdout_fees
    repo.create(backtest_id=f"{strategy}-{symbol}-{timeframe}-{completed}-{suffix}", symbol=symbol,
                strategy_name=strategy, file_path=f"alpaca:{timeframe}:{symbol}{suffix}", started_at=completed,
                completed_at=completed, metrics=metrics, trades=[])


@pytest.fixture
def rows(tmp_path):
    settings = settings_for(tmp_path)
    db = database(settings)
    repo = BacktestRepository(db)
    # Fee band: fails with eToro's $2 a trade, passes without (OOS and holdout).
    _row(repo, "momentum_breakout", "AAA", "1d", trades=30, expectancy=-1.0, holdout=6, holdout_expectancy=-0.5, fees=60.0, holdout_fees=12.0)
    _row(repo, "momentum_breakout", "BBB", "1d", trades=20, expectancy=-0.5, holdout=5, holdout_expectancy=-1.0, fees=40.0, holdout_fees=10.0)
    # An older row for AAA: the newest per symbol counts, as on the main gate.
    _row(repo, "momentum_breakout", "AAA", "1d", trades=99, expectancy=50.0, holdout=50, holdout_expectancy=50.0,
         fees=198.0, holdout_fees=100.0, completed=(NOW - timedelta(days=2)).isoformat())
    # Lane: fails both, 35 trades, -0.02R fee-free.
    _row(repo, "ma_crossover", "AAA", "1d", trades=35, expectancy=-4.0, holdout=12, holdout_expectancy=-3.0, fees=70.0, holdout_fees=24.0)
    # Outside the lane: -0.06R fee-free.
    _row(repo, "trend_following", "AAA", "1d", trades=50, expectancy=-8.0, holdout=12, holdout_expectancy=-1.0, fees=100.0, holdout_fees=24.0)
    # A row written before the fee fields: no fees counted (never looser).
    _row(repo, "pullback_trend", "AAA", "1d", trades=45, expectancy=0.5, holdout=4, holdout_expectancy=2.0)
    # A minute key: passes with eToro's fees, its stress profile fails (paper_passed false); Alpaca passes.
    profiles = {
        "alpaca": {"number_of_trades": 60, "expectancy_usd": 3.0, "holdout_trades": 12, "holdout_expectancy_usd": 1.0},
        "stress": {"number_of_trades": 60, "expectancy_usd": -1.0, "holdout_trades": 12, "holdout_expectancy_usd": -2.0},
    }
    _row(repo, "rsi_reversal", "AAA", "15m", trades=60, expectancy=1.0, holdout=12, holdout_expectancy=0.5, fees=120.0,
         holdout_fees=24.0, cost_profiles=profiles)
    _row(repo, "vwap_reclaim", "AAA", "5m", trades=80, expectancy=1.0, holdout=12, holdout_expectancy=0.5, fees=160.0, holdout_fees=24.0)
    # Never read: out of the 7-day pool, another strategy, a short-research row.
    _row(repo, "ma_crossover", "OLD", "1d", trades=500, expectancy=90.0, holdout=90, holdout_expectancy=90.0,
         fees=1.0, holdout_fees=1.0, completed=(NOW - timedelta(days=8)).isoformat())
    _row(repo, "mean_reversion", "AAA", "1d", trades=500, expectancy=90.0, holdout=90, holdout_expectancy=90.0, fees=1.0, holdout_fees=1.0)
    _row(repo, "ma_crossover", "SHO", "1d", trades=500, expectancy=90.0, holdout=90, holdout_expectancy=90.0, fees=1.0,
         holdout_fees=1.0, suffix=":walk_forward_oos_short")
    return settings, db


STRATEGIES = {"momentum_breakout", "ma_crossover", "trend_following", "pullback_trend", "rsi_reversal", "vwap_reclaim"}
PINNED = {
    "momentum_breakout:1d": {"basis": "fee_free", "passed": True, "reasons": [], "symbols": 2, "oos_trades": 50,
                             "oos_expectancy_usd": 1.2, "oos_expectancy_r": 0.012, "holdout_trades": 11,
                             "holdout_expectancy_usd": 1.2727, "rows": 2, "rows_without_fees": 0},
    "ma_crossover:1d": {"basis": "fee_free", "passed": False,
                        "reasons": ["too_few_oos_trades", "oos_expectancy_not_positive", "holdout_expectancy_not_positive"], "symbols": 1,
                        "oos_trades": 35, "oos_expectancy_usd": -2.0, "oos_expectancy_r": -0.02, "holdout_trades": 12,
                        "holdout_expectancy_usd": -1.0, "rows": 1, "rows_without_fees": 0},
    "trend_following:1d": {"basis": "fee_free", "passed": False, "reasons": ["oos_expectancy_not_positive"],
                           "symbols": 1, "oos_trades": 50, "oos_expectancy_usd": -6.0, "oos_expectancy_r": -0.06,
                           "holdout_trades": 12, "holdout_expectancy_usd": 1.0, "rows": 1, "rows_without_fees": 0},
    "pullback_trend:1d": {"basis": "fee_free", "passed": False, "reasons": ["too_few_holdout_trades"], "symbols": 1,
                          "oos_trades": 45, "oos_expectancy_usd": 0.5, "oos_expectancy_r": 0.005, "holdout_trades": 4,
                          "holdout_expectancy_usd": 2.0, "rows": 1, "rows_without_fees": 1},
    "rsi_reversal:15m": {"basis": "alpaca", "passed": True, "reasons": [], "symbols": 1, "oos_trades": 60,
                         "oos_expectancy_usd": 3.0, "oos_expectancy_r": 0.03, "holdout_trades": 12,
                         "holdout_expectancy_usd": 1.0, "rows": 1, "rows_without_fees": 0},
    "vwap_reclaim:5m": {"basis": "alpaca", "passed": False,
                        "reasons": ["no_cost_profile_rows", "too_few_oos_trades", "oos_expectancy_not_positive", "too_few_holdout_trades"],
                        "symbols": 0, "oos_trades": 0, "oos_expectancy_usd": 0.0, "oos_expectancy_r": 0.0,
                        "holdout_trades": 0, "holdout_expectancy_usd": 0.0, "rows": 1, "rows_without_fees": 0},
}


def test_learning_verdicts_for_fixed_rows_are_pinned(rows) -> None:
    settings, db = rows
    assert compute_learning_verdicts(db, settings, STRATEGIES, now=NOW) == PINNED


def test_the_main_gate_judges_the_same_rows_with_the_fees(rows) -> None:
    settings, db = rows
    main = compute_verdicts(db, settings, now=NOW)
    assert main["momentum_breakout:1d"]["passed"] is False and main["momentum_breakout:1d"]["oos_expectancy_r"] == -0.008
    assert main["ma_crossover:1d"]["oos_expectancy_r"] == -0.04
    assert main["rsi_reversal:15m"]["passed"] is True and main["rsi_reversal:15m"]["paper_passed"] is False
    learning = compute_learning_verdicts(db, settings, STRATEGIES, now=NOW)
    kinds = {
        key: classify(settings, main[key], learning.get(key), key.rsplit(":", 1)[1], crypto=False)[0]
        for key in PINNED
    }
    assert kinds == {
        "momentum_breakout:1d": FEE_BAND, "ma_crossover:1d": LANE, "trend_following:1d": None,
        # OOS passes, too few holdout trades: option B's (paper_oos_passed) while its bar is on.
        "pullback_trend:1d": None, "rsi_reversal:15m": FEE_BAND, "vwap_reclaim:5m": None,
    }
    assert classify(settings, main["pullback_trend:1d"], learning["pullback_trend:1d"], "1d", crypto=False)[1] == (
        "passes_the_paper_oos_bar"
    )
    off = settings.model_copy(update={"paper_oos_bar_enabled": False})
    assert classify(off, main["pullback_trend:1d"], learning["pullback_trend:1d"], "1d", crypto=False) == (LANE, "near_miss")


def test_only_the_strategies_asked_for_and_never_an_empty_query(rows) -> None:
    settings, db = rows
    assert set(compute_learning_verdicts(db, settings, {"ma_crossover"}, now=NOW)) == {"ma_crossover:1d"}

    class NoDatabase:
        def connect(self):
            raise AssertionError("an empty strategy set must not touch the database")

    assert compute_learning_verdicts(NoDatabase(), settings, set(), now=NOW) == {}
    assert compute_learning_verdicts(NoDatabase(), settings, {""}, now=NOW) == {}


def test_fee_free_metrics_add_the_fees_back() -> None:
    adjusted, recorded = fee_free_metrics(
        {"number_of_trades": 4, "expectancy_usd": -1.0, "holdout_trades": 2, "holdout_expectancy_usd": -2.0,
         "etoro_fees_usd": 8.0, "holdout_etoro_fees_usd": 4.0}
    )
    assert adjusted == {"number_of_trades": 4, "expectancy_usd": 1.0, "holdout_trades": 2, "holdout_expectancy_usd": 0.0}
    assert recorded is True
    adjusted, recorded = fee_free_metrics({"number_of_trades": 0, "expectancy_usd": None})
    assert adjusted["expectancy_usd"] == 0.0 and recorded is False


@pytest.mark.parametrize(
    ("verdict", "learning", "timeframe", "crypto", "bar", "expected"),
    [
        ({"passed": True}, {"passed": True}, "1d", False, True, (None, "passes_the_main_gate")),
        ({"passed": False, "paper_oos_passed": True}, {"passed": True}, "1d", False, True, (None, "passes_the_paper_oos_bar")),
        ({"passed": False, "paper_oos_passed": True}, {"passed": True, "basis": "fee_free"}, "1d", False, False, (FEE_BAND, "passes_fee_free")),
        ({"passed": False}, {"passed": False, "oos_trades": 30, "oos_expectancy_r": -0.05}, "1d", False, True, (LANE, "near_miss")),
        ({"passed": False}, {"passed": False, "oos_trades": 29, "oos_expectancy_r": 0.5}, "1d", False, True, (None, "lane_too_few_oos_trades")),
        ({"passed": False}, {"passed": False, "oos_trades": 300, "oos_expectancy_r": -0.0501}, "1d", False, True, (None, "lane_expectancy_below_floor")),
        ({"passed": False}, None, "1d", False, True, (None, "no_learning_verdict")),
        (None, {"passed": True}, "1d", False, True, (None, "no_evidence_verdict")),
        # A minute key needs paper_passed too: passing with fees alone is still refused by main.
        ({"passed": True, "paper_passed": False}, {"passed": True, "basis": "alpaca"}, "15m", False, True, (FEE_BAND, "passes_alpaca")),
        ({"passed": True, "paper_passed": True}, {"passed": True}, "15m", False, True, (None, "passes_the_main_gate")),
        ({"passed": False, "paper_oos_passed": True, "paper_passed": False}, {"passed": True}, "5m", False, True, (None, "passes_the_paper_oos_bar")),
        # Crypto mirrors the gate: passed alone; option B's bar never applies to it.
        ({"passed": True, "paper_passed": False}, {"passed": True}, "15m", True, True, (None, "passes_the_main_gate")),
        ({"passed": False, "paper_oos_passed": True}, {"passed": True, "basis": "fee_free"}, "1d", True, True, (FEE_BAND, "passes_fee_free")),
        ({"passed": False}, {"passed": False, "oos_trades": 40, "oos_expectancy_r": 0.0}, "1d", True, True, (LANE, "near_miss")),
    ],
)
def test_classify_mirrors_the_main_gate(tmp_path, verdict, learning, timeframe, crypto, bar, expected) -> None:
    settings = settings_for(tmp_path, paper_oos_bar_enabled=bar)
    assert classify(settings, verdict, learning, timeframe, crypto=crypto) == expected
    assert (LANE_MIN_OOS_TRADES, LANE_MIN_EXPECTANCY_R) == (30, -0.05)


def test_near_miss_first() -> None:
    keys = [
        rank_key(LANE, {"oos_expectancy_r": -0.01}, 90),
        rank_key(FEE_BAND, {"oos_expectancy_r": 0.001}, 60),
        rank_key(LANE, {"oos_expectancy_r": -0.001}, 70),
        rank_key(LANE, {"oos_expectancy_r": -0.001}, 75),
    ]
    assert sorted(range(4), key=keys.__getitem__) == [1, 3, 2, 0]


def test_stale_learning_verdicts() -> None:
    assert not stale({"computed_at": NOW.isoformat()}, now=NOW + timedelta(hours=23))
    assert stale({"computed_at": NOW.isoformat()}, now=NOW + timedelta(hours=25))
    assert stale({"computed_at": "2026-10-26T14:00:00"}, now=NOW)
    assert stale({}, now=NOW) and stale(None, now=NOW)
