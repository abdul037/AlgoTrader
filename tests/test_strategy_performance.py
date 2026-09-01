"""Tests for per-strategy live performance analysis and decay verdicts."""

from __future__ import annotations

from types import SimpleNamespace

from app.performance.strategy_performance import (
    ACTION_DEMOTE,
    ACTION_KEEP,
    ACTION_WATCH,
    STATUS_DEAD,
    STATUS_DECAYING,
    STATUS_HEALTHY,
    STATUS_INSUFFICIENT,
    analyze_by_strategy,
    daily_pnl_series,
    decay_verdict,
    demoted_strategy_names,
    strategy_demote_verdicts,
)


def _trade(strategy: str, pnl: float, *, closed_at: str = "2026-08-28T00:00:00Z", r: float | None = None):
    payload = {} if r is None else {"realized_r_multiple": r}
    return SimpleNamespace(strategy_name=strategy, realized_pnl_usd=pnl, closed_at=closed_at, payload=payload)


class TestAnalyzeByStrategy:
    def test_splits_metrics_per_strategy(self) -> None:
        trades = [
            _trade("orb", 100, closed_at="2026-08-01"),
            _trade("orb", -50, closed_at="2026-08-02"),
            _trade("orb", 150, closed_at="2026-08-03"),
            _trade("meanrev", -20, closed_at="2026-08-01"),
            _trade("meanrev", -30, closed_at="2026-08-02"),
        ]
        results = {r.strategy_name: r for r in analyze_by_strategy(trades)}
        orb = results["orb"]
        assert orb.trades == 3
        assert orb.realized_pnl_usd == 200.0
        assert round(orb.win_rate, 0) == 67.0
        assert orb.profit_factor == 5.0  # 250 gross profit / 50 gross loss
        assert orb.expectancy_usd == round(200 / 3, 2)
        meanrev = results["meanrev"]
        assert meanrev.realized_pnl_usd == -50.0
        assert meanrev.win_rate == 0.0

    def test_ranks_by_realized_pnl(self) -> None:
        trades = [_trade("low", 10), _trade("high", 500)]
        ranked = analyze_by_strategy(trades)
        assert ranked[0].strategy_name == "high"

    def test_per_strategy_drawdown_uses_time_order(self) -> None:
        # Up 100, down 300, up 50 -> peak 100, trough -200 -> max dd 300.
        trades = [
            _trade("s", 100, closed_at="2026-08-01"),
            _trade("s", -300, closed_at="2026-08-02"),
            _trade("s", 50, closed_at="2026-08-03"),
        ]
        (perf,) = analyze_by_strategy(trades)
        assert perf.max_drawdown_usd == 300.0

    def test_missing_strategy_name_bucketed_as_unknown(self) -> None:
        (perf,) = analyze_by_strategy([SimpleNamespace(realized_pnl_usd=5, closed_at="x", payload={})])
        assert perf.strategy_name == "unknown"


class TestDecayVerdict:
    def _perf(self, *, trades: int, expectancy: float):
        return analyze_by_strategy(
            [_trade("s", expectancy, closed_at=f"2026-08-{i+1:02d}") for i in range(trades)]
        )[0]

    def test_insufficient_data_keeps(self) -> None:
        perf = self._perf(trades=5, expectancy=10)
        v = decay_verdict(perf, backtest_expectancy_usd=20, min_trades=20)
        assert v.status == STATUS_INSUFFICIENT and v.action == ACTION_KEEP

    def test_negative_live_expectancy_is_dead(self) -> None:
        perf = self._perf(trades=30, expectancy=-5)
        v = decay_verdict(perf, backtest_expectancy_usd=20, min_trades=20)
        assert v.status == STATUS_DEAD and v.action == ACTION_DEMOTE

    def test_eroded_edge_is_decaying(self) -> None:
        perf = self._perf(trades=30, expectancy=4)  # 4 vs backtest 20 = 20% < 50%
        v = decay_verdict(perf, backtest_expectancy_usd=20, min_trades=20, retention_threshold=0.5)
        assert v.status == STATUS_DECAYING and v.action == ACTION_WATCH
        assert v.retention_ratio == 0.2

    def test_holding_edge_is_healthy(self) -> None:
        perf = self._perf(trades=30, expectancy=18)  # 90% of backtest
        v = decay_verdict(perf, backtest_expectancy_usd=20, min_trades=20)
        assert v.status == STATUS_HEALTHY and v.action == ACTION_KEEP

    def test_positive_without_baseline_is_healthy(self) -> None:
        perf = self._perf(trades=30, expectancy=10)
        v = decay_verdict(perf, backtest_expectancy_usd=None, min_trades=20)
        assert v.status == STATUS_HEALTHY and v.retention_ratio is None


class TestDailyPnlSeries:
    def test_aggregates_per_day_with_cumulative_equity(self) -> None:
        trades = [
            _trade("a", 100, closed_at="2026-08-01T15:00:00Z"),
            _trade("b", -40, closed_at="2026-08-01T16:00:00Z"),
            _trade("c", 60, closed_at="2026-08-03T10:00:00Z"),
        ]
        series = daily_pnl_series(trades)
        assert [r["date"] for r in series] == ["2026-08-01", "2026-08-03"]
        assert series[0]["realized_pnl_usd"] == 60.0  # 100 - 40
        assert series[0]["trades"] == 2
        assert series[0]["cumulative_pnl_usd"] == 60.0
        assert series[1]["cumulative_pnl_usd"] == 120.0  # 60 + 60

    def test_empty_input(self) -> None:
        assert daily_pnl_series([]) == []


class TestDemotedStrategyNames:
    def test_losing_strategy_with_enough_trades_is_demoted(self) -> None:
        # 20 losing trades (<= 0 expectancy) -> demote.
        trades = [_trade("loser", -10, closed_at=f"2026-08-{i+1:02d}") for i in range(20)]
        assert demoted_strategy_names(trades, min_trades=20) == {"loser"}

    def test_winning_strategy_is_not_demoted(self) -> None:
        trades = [_trade("winner", 25, closed_at=f"2026-08-{i+1:02d}") for i in range(20)]
        assert demoted_strategy_names(trades, min_trades=20) == set()

    def test_losing_strategy_below_min_trades_is_not_demoted(self) -> None:
        # Only 5 losing trades -> insufficient evidence, never demoted.
        trades = [_trade("loser", -10, closed_at=f"2026-08-{i+1:02d}") for i in range(5)]
        assert demoted_strategy_names(trades, min_trades=20) == set()

    def test_mixed_book_demotes_only_the_dead_strategy(self) -> None:
        losers = [_trade("dead", -5, closed_at=f"2026-08-{i+1:02d}") for i in range(20)]
        winners = [_trade("alive", 15, closed_at=f"2026-08-{i+1:02d}") for i in range(20)]
        assert demoted_strategy_names(losers + winners, min_trades=20) == {"dead"}

    def test_verdicts_cover_every_strategy_with_trades(self) -> None:
        trades = [
            _trade("a", 10, closed_at="2026-08-01"),
            _trade("b", -10, closed_at="2026-08-01"),
        ]
        names = {v.strategy_name for v in strategy_demote_verdicts(trades, min_trades=1)}
        assert names == {"a", "b"}

    def test_empty_input(self) -> None:
        assert demoted_strategy_names([]) == set()
        assert strategy_demote_verdicts([]) == []
