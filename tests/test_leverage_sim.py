"""Leverage simulation of closed paper trades and the exploration leaderboard (Phase 1, 2026-11-10).

* the price result scales with the leverage; financing is charged on the leveraged notional for
  each New York night held (a Friday three times, the weekend not again), never at 1x;
* a move of 1/L against the entry over the hold wipes the margin out (a forced close);
* the worst price comes from the marks the paper refresh keeps (exact when they cover the hold,
  else flagged approximate);
* the paper ledger keeps each closed trade's simulation; older trades are simulated on the fly;
* the leaderboard aggregates per lane and strategy:timeframe and applies the provisional proof
  rule to exploration rows only; it is refreshed hourly into its runtime-state key, read by the
  hosted scorecard's own query.

No wall clock: every time is given.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.backtesting.cost_model import CostModel
from app.performance.leverage_sim import (
    LEADERBOARD_KEY,
    LEVERAGES,
    PROOF_MIN_TRADES,
    REFRESHED_KEY,
    SIM_FIELD,
    compute_leaderboard,
    financing_nights,
    refresh_leaderboard,
    simulate,
    track_marks,
    trade_simulation,
)
from tests.conftest import make_settings
from tests.test_workflow_service import FakeLogs, FakeState

MON = datetime(2026, 11, 9, 15, 0, tzinfo=UTC)  # Monday 10:00 New York
RATE = CostModel().overnight_fee_daily_pct
EXPLORATION = {"paper_only": True, "paper_only_reason": "exploration"}
OOS_ONLY = {"paper_only": True, "paper_only_reason": "oos_only"}


def _sim(entry=100.0, exit_=102.0, qty=50.0, opened=MON, closed=None, **kw):
    return simulate(
        side=kw.pop("side", "buy"),
        entry_price=entry,
        exit_price=exit_,
        quantity=qty,
        opened_at=opened.isoformat(),
        closed_at=(closed or opened + timedelta(hours=2)).isoformat(),
        **kw,
    )


def test_an_intraday_trade_scales_with_the_leverage_and_pays_no_financing() -> None:
    sim = _sim(stop_loss=99.0, pnl_usd=100.0, worst_price=99.5, worst_at=MON.isoformat(), marks_since=MON.isoformat())
    assert sim["margin_usd"] == 5_000.0 and sim["risk_usd"] == 50.0 and sim["nights"] == 0.0
    assert sim["worst_basis"] == "marks" and sim["approximate"] is False
    assert sim["adverse_move_pct"] == 0.5
    assert {lev: (r["pnl_usd"], r["r"], r["financing_usd"], r["forced_close"]) for lev, r in sim["by_leverage"].items()} == {
        "1": (100.0, 2.0, 0.0, False),
        "2": (200.0, 4.0, 0.0, False),
        "5": (500.0, 10.0, 0.0, False),
        "10": (1_000.0, 20.0, 0.0, False),
    }
    assert [int(k) for k in sim["by_leverage"]] == list(LEVERAGES) == [1, 2, 5, 10]


@pytest.mark.parametrize(
    ("opened", "closed", "nights"),
    [
        (MON, MON + timedelta(hours=5), 0.0),  # same day
        (MON, MON + timedelta(days=1), 1.0),  # Monday night
        (MON + timedelta(days=3), MON + timedelta(days=7), 4.0),  # Thursday + Friday x3
        (MON + timedelta(days=4), MON + timedelta(days=7), 3.0),  # Friday x3 only
        (MON, MON + timedelta(days=7), 7.0),  # a whole week
        (MON, MON + timedelta(days=14), 14.0),
    ],
)
def test_financing_nights_follow_etoro_s_weekend_charge(opened, closed, nights) -> None:
    assert CostModel().weekend_multiplier == 3.0
    assert financing_nights(opened, closed, weekend_multiplier=3.0) == nights


def test_overnight_financing_is_charged_on_the_leveraged_notional_from_2x() -> None:
    sim = _sim(closed=MON + timedelta(days=3), stop_loss=95.0, pnl_usd=100.0)  # Mon..Thu: 3 nights
    assert sim["nights"] == 3.0
    for leverage in LEVERAGES:
        result = sim["by_leverage"][str(leverage)]
        financing = 0.0 if leverage == 1 else round(5_000.0 * leverage * RATE * 3, 2)
        assert result["financing_usd"] == financing
        assert result["pnl_usd"] == round(100.0 * leverage - financing, 2)
        assert result["r"] == round((100.0 * leverage - financing) / 250.0, 4)
    assert sim["by_leverage"]["10"]["financing_usd"] == 22.5  # 0.015% a night on $50,000


def test_a_move_of_one_over_the_leverage_against_the_entry_wipes_out_the_margin() -> None:
    worst_at = MON + timedelta(days=1)
    sim = _sim(
        exit_=104.0,
        closed=MON + timedelta(days=3),
        stop_loss=85.0,
        pnl_usd=200.0,
        worst_price=89.0,  # -11%: past 10x's 10%, short of 5x's 20%
        worst_at=worst_at.isoformat(),
        marks_since=(MON + timedelta(minutes=1)).isoformat(),
    )
    ten, five, two = (sim["by_leverage"][k] for k in ("10", "5", "2"))
    assert ten["forced_close"] is True and five["forced_close"] is False and not two["forced_close"]
    assert ten["gross_usd"] == -5_000.0  # the whole margin
    assert ten["financing_usd"] == round(50_000.0 * RATE * 1, 2)  # until the worst mark, 1 night
    assert ten["pnl_usd"] == round(-5_000.0 - 50_000.0 * RATE, 2)
    assert ten["r"] == round(ten["pnl_usd"] / 750.0, 4)
    assert five["gross_usd"] == 1_000.0 and five["financing_usd"] == round(25_000.0 * RATE * 3, 2)
    assert sim["adverse_move_pct"] == 11.0 and sim["approximate"] is False
    deeper = _sim(exit_=104.0, stop_loss=85.0, worst_price=79.0, worst_at=worst_at.isoformat(), marks_since=MON.isoformat())
    assert [deeper["by_leverage"][k]["forced_close"] for k in ("1", "2", "5", "10")] == [False, False, True, True]
    exactly = _sim(exit_=104.0, worst_price=90.0, marks_since=MON.isoformat())
    assert exactly["by_leverage"]["10"]["forced_close"] is True  # exactly 10%: the margin is gone


def test_a_gap_through_the_stop_counts_without_marks_and_is_approximate() -> None:
    sim = _sim(exit_=88.0, closed=MON + timedelta(days=1), stop_loss=97.0)  # a -12% gap at the open
    assert sim["worst_basis"] == "entry_exit" and sim["approximate"] is True
    assert sim["pnl_1x_usd"] == -600.0  # computed from the fills when no P/L is given
    assert sim["by_leverage"]["10"]["forced_close"] is True
    assert sim["by_leverage"]["10"]["financing_usd"] == round(50_000.0 * RATE, 2)  # whole hold
    assert sim["by_leverage"]["5"]["pnl_usd"] == round(-3_000.0 - 25_000.0 * RATE, 2)
    late = _sim(worst_price=99.0, marks_since=(MON + timedelta(hours=1)).isoformat())
    assert late["worst_basis"] == "partial_marks" and late["approximate"] is True


def test_a_short_s_worst_price_is_its_highest() -> None:
    sim = _sim(side="sell", exit_=98.0, pnl_usd=100.0, stop_loss=103.0, worst_price=111.0, marks_since=MON.isoformat())
    assert sim["worst_price"] == 111.0 and sim["adverse_move_pct"] == 11.0
    assert sim["by_leverage"]["10"]["forced_close"] is True
    assert sim["by_leverage"]["2"]["pnl_usd"] == 200.0


@pytest.mark.parametrize(
    "changes",
    [{"entry": 0.0}, {"exit_": None}, {"qty": 0.0}, {"closed": MON - timedelta(hours=1)}],
)
def test_an_unreadable_trade_is_not_simulated(changes) -> None:
    assert _sim(**changes) is None


def test_the_paper_refresh_keeps_the_worst_and_best_marks_of_the_hold() -> None:
    position = SimpleNamespace(side="buy", payload={"paper_only": True})
    for minute, price in enumerate([100.0, 98.0, 101.0, 97.5, 99.0, float("nan"), -1.0]):
        track_marks(position, price, at=MON + timedelta(minutes=minute))
    assert position.payload == {
        "paper_only": True,
        "worst_price": 97.5,
        "worst_at": (MON + timedelta(minutes=3)).isoformat(),
        "best_price": 101.0,
        "marks_since": MON.isoformat(),
    }
    short = SimpleNamespace(side="sell", payload={})
    for price in (100.0, 103.0, 96.0):
        track_marks(short, price, at=MON)
    assert (short.payload["worst_price"], short.payload["best_price"]) == (103.0, 96.0)
    track_marks(SimpleNamespace(side="buy", payload=None), 1.0)  # never raises


def test_the_paper_ledger_keeps_each_closed_trade_s_simulation(tmp_path) -> None:
    from app.live_signal_schema import MarketQuote
    from app.models.trade import TradeOrder
    from app.paper.service import PaperTradingService
    from app.storage.db import Database
    from app.storage.repositories import PaperPositionRepository, PaperTradeRepository

    settings = make_settings(tmp_path, paper_slippage_bps=0.0)
    db = Database(settings)
    db.initialize()
    service = PaperTradingService(
        settings=settings,
        positions=PaperPositionRepository(db),
        trades=PaperTradeRepository(db),
        run_logs=FakeLogs(),
    )
    order = TradeOrder(
        symbol="UBER",
        amount_usd=5_000.0,
        proposed_price=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        strategy_name="trend_following",
        metadata={"timeframe": "1d", **EXPLORATION},
    )
    quote = MarketQuote(symbol="UBER", last_execution=100.0)
    position = service.open_from_approved_proposal(SimpleNamespace(id="p", order=order, signal=None), live_quote=quote)
    marks = [MarketQuote(symbol="UBER", last_execution=price) for price in (99.0, 96.0, 103.0)]
    for mark in marks:  # each refresh marks the open position (it hits no stop or target)
        result = service.refresh_open_positions(market_data_engine=SimpleNamespace(get_quote=lambda *_a, mark=mark, **_k: mark))
        assert result["closed"] == 0
    stored = service.positions.get(position.id)
    assert (stored.payload["worst_price"], stored.payload["best_price"]) == (96.0, 103.0)
    target = MarketQuote(symbol="UBER", last_execution=110.0)
    service.refresh_open_positions(market_data_engine=SimpleNamespace(get_quote=lambda *_a, **_k: target))
    (trade,) = service.trades.list(limit=10)
    sim = trade.payload[SIM_FIELD]
    assert trade.payload["paper_only_reason"] == "exploration"
    assert sim["worst_price"] == 96.0 and sim["worst_basis"] == "marks" and not sim["approximate"]
    assert sim["margin_usd"] == 5_000.0 and sim["risk_usd"] == 250.0
    assert sim["by_leverage"]["2"]["pnl_usd"] == 1_000.0 and sim["by_leverage"]["2"]["r"] == 4.0
    assert trade_simulation(trade) == sim  # the stored one is read back as it is


def test_an_older_trade_is_simulated_from_its_row() -> None:
    trade = SimpleNamespace(
        side="buy",
        entry_price=100.0,
        exit_price=95.0,
        quantity=10.0,
        opened_at=MON.isoformat(),
        closed_at=(MON + timedelta(days=1)).isoformat(),
        realized_pnl_usd=-50.0,
        payload={"realized_r_multiple": -1.0},
    )
    sim = trade_simulation(trade)
    assert sim["risk_usd"] == 50.0 and sim["approximate"] is True and sim["nights"] == 1.0
    assert sim["by_leverage"]["1"]["r"] == -1.0
    assert sim["by_leverage"]["2"]["r"] == round((-100.0 - 2_000.0 * RATE) / 50.0, 4)
    trade.payload = {"realized_r_multiple": 0.0}  # no stop known: R unknown, $ still simulated
    assert trade_simulation(trade)["by_leverage"]["2"]["r"] is None


# --- the leaderboard -------------------------------------------------------------------------


def _trade(strategy, pnl, day, *, tags=EXPLORATION, timeframe="1d", exit_=None, hours=2):
    opened = MON + timedelta(days=day)
    return SimpleNamespace(
        strategy_name=strategy,
        timeframe=timeframe,
        side="buy",
        entry_price=100.0,
        exit_price=exit_ if exit_ is not None else 100.0 + pnl / 10.0,
        quantity=10.0,
        opened_at=opened.isoformat(),
        closed_at=(opened + timedelta(hours=hours)).isoformat(),
        realized_pnl_usd=pnl,
        payload={**tags, "realized_r_multiple": pnl / 50.0},
    )


def _row(board, lane, key):
    (row,) = [r for r in board["lanes"][lane] if r["key"] == key]
    return row


def test_the_leaderboard_aggregates_per_lane_and_strategy_timeframe() -> None:
    trades = [_trade("a", pnl, i) for i, pnl in enumerate([10.0, -30.0, 5.0, -10.0])]
    trades += [_trade("a", 20.0, 0, timeframe="5m"), _trade("a", 40.0, 1, tags={})]
    trades += [_trade("b", -5.0, 2, tags=OOS_ONLY), _trade("", 5.0, 3), SimpleNamespace(strategy_name="c", payload={})]
    board = compute_leaderboard(trades, now=MON)
    assert (board["trades_read"], board["trades_skipped"]) == (9, 2)
    assert [r["key"] for r in board["lanes"]["exploration"]] == ["a:5m", "a:1d"]
    row = _row(board, "exploration", "a:1d")
    one = row["by_leverage"]["1"]
    assert (row["trades"], one["wins"], one["win_rate"], one["total_usd"]) == (4, 2, 50.0, -25.0)
    assert (one["mean_usd"], one["total_r"], one["mean_r"], one["r_trades"]) == (-6.25, -0.5, -0.125, 4)
    assert one["max_drawdown_usd"] == 35.0  # +10, -30, +5, -10: from +10 down to -25
    assert row["by_leverage"]["2"]["total_usd"] == -50.0  # same-day trades: no financing
    assert row["meets_proof_rule"] == {"1": False, "2": False, "5": False, "10": False}
    normal = _row(board, "normal", "a:1d")
    assert normal["trades"] == 1 and normal["meets_proof_rule"] == {"1": None, "2": None, "5": None, "10": None}
    assert "meets_proof_rule" not in normal["by_leverage"]["1"]
    assert _row(board, "oos_only", "b:1d")["trades"] == 1
    assert board["proof_rule"]["min_closed_trades"] == PROOF_MIN_TRADES == 30
    json.dumps(board, allow_nan=False)  # what the runtime state stores


def test_the_provisional_proof_rule_per_leverage() -> None:
    winners = [_trade("w", 30.0 if i % 3 else -20.0, i % 20, hours=26) for i in range(PROOF_MIN_TRADES)]
    board = compute_leaderboard(winners, now=MON)
    row = _row(board, "exploration", "w:1d")
    assert row["meets_proof_rule"] == {"1": True, "2": True, "5": True, "10": True}
    assert row["proven_leverages"] == [1, 2, 5, 10]
    short = compute_leaderboard(winners[:-1], now=MON)
    assert _row(short, "exploration", "w:1d")["meets_proof_rule"]["1"] is False  # 29 trades
    # Every trade ends +5%, but half dipped 11% on the way: 10x loses its margin on each of those.
    dipped = [_trade("d", 50.0, i % 20) for i in range(PROOF_MIN_TRADES)]
    for trade in dipped[::2]:
        trade.payload.update(worst_price=89.0, marks_since=trade.opened_at)
    board = compute_leaderboard(dipped, now=MON)
    row = _row(board, "exploration", "d:1d")
    assert row["by_leverage"]["10"]["forced_closes"] == 15
    assert row["by_leverage"]["10"]["total_usd"] == 15 * 500.0 - 15 * 1_000.0  # same-day: no fee
    assert row["meets_proof_rule"] == {"1": True, "2": True, "5": True, "10": False}
    assert row["proven_leverages"] == [1, 2, 5]
    assert row["approximate_trades"] == 15  # the rows without marks
    # A positive $ total but a negative mean R: one big win on a big risk, many small losses.
    big = _trade("r", 300.0, 0)
    big.payload["realized_r_multiple"] = 0.3  # $1,000 at risk
    small = [_trade("r", -5.0, 1 + i % 19) for i in range(PROOF_MIN_TRADES - 1)]
    for trade in small:
        trade.payload["realized_r_multiple"] = -0.5  # $10 at risk
    row = _row(compute_leaderboard([big, *small], now=MON), "exploration", "r:1d")
    assert row["by_leverage"]["1"]["total_usd"] == 155.0 and row["by_leverage"]["1"]["mean_r"] < 0
    assert row["meets_proof_rule"]["1"] is False


def test_proven_strategies_rank_first() -> None:
    proven = [_trade("p", 30.0, i % 20) for i in range(PROOF_MIN_TRADES)]
    board = compute_leaderboard([_trade("q", 100.0, 1), *proven], now=MON)
    assert [r["key"] for r in board["lanes"]["exploration"]] == ["p:1d", "q:1d"]


def test_the_leaderboard_is_refreshed_hourly_into_its_runtime_state_key() -> None:
    trades = [_trade("a", 10.0, 0)]
    service = SimpleNamespace(
        runtime_state=FakeState(),
        paper_trading=SimpleNamespace(trades=SimpleNamespace(list=lambda limit=500: trades)),
    )
    board = refresh_leaderboard(service)
    assert json.loads(service.runtime_state.get(LEADERBOARD_KEY)) == board
    assert service.runtime_state.get(REFRESHED_KEY) == board["computed_at"]
    assert refresh_leaderboard(service) is None  # not due again within the hour
    trades.append(_trade("b", 10.0, 1))
    assert len(refresh_leaderboard(service, force=True)["lanes"]["exploration"]) == 2
    assert refresh_leaderboard(SimpleNamespace(runtime_state=FakeState(), paper_trading=None)) is None


def test_maintenance_refreshes_it_with_the_phase_gates() -> None:
    from app.workflow import operations

    trades = [_trade("a", 10.0, 0)]
    service = SimpleNamespace(
        runtime_state=FakeState(),
        run_logs=FakeLogs(),
        settings=SimpleNamespace(),
        market_screener=SimpleNamespace(),
        auto_trading=None,
        paper_trading=SimpleNamespace(trades=SimpleNamespace(list=lambda limit=500: trades)),
        _is_due=lambda key, minutes: True,
    )
    completed: list[str] = []
    operations.refresh_phase_gates(service, completed, [])
    assert "exploration_leaderboard" in completed
    assert json.loads(service.runtime_state.get(LEADERBOARD_KEY))["lanes"]["exploration"][0]["key"] == "a:1d"


def test_the_hosted_scorecard_reads_the_leaderboard_and_keeps_its_query_one_statement() -> None:
    from app.dashboard.scorecard import PAGE_PATH, scorecard_sql

    sql = scorecard_sql()
    assert sql.startswith("select json_build_object('now', now()") and sql.rstrip().endswith("as d;")
    assert f"state_key='{LEADERBOARD_KEY}'" in sql
    assert "'exploration_open', (select count(*) from paper_positions" in sql
    assert '"paper_only_reason": "exploration"' in sql and '\\"' not in sql
    assert sql.count(";") == 1
    page = PAGE_PATH.read_text(encoding="utf-8")
    for anchor in ('id="explore"', 'id="explore-others"', 'id="explore-summary"', "renderExploration(d);"):
        assert anchor in page
