"""Tests for the cross-sectional momentum selection filter."""

from __future__ import annotations

from types import SimpleNamespace

from app.screener.cross_sectional_momentum import filter_top_momentum, momentum_value


def _cand(symbol, *, meta=None, indicators=None, score=0.0):
    return SimpleNamespace(symbol=symbol, metadata=meta or {}, indicators=indicators or {}, score=score)


class TestMomentumValue:
    def test_prefers_metadata_then_indicators_then_score(self) -> None:
        assert momentum_value(_cand("A", meta={"momentum_pct": 12.0})) == 12.0
        assert momentum_value(_cand("B", indicators={"ema_50_slope": 3.5})) == 3.5
        assert momentum_value(_cand("C", score=7.0)) == 7.0

    def test_bad_values_fall_through_to_score(self) -> None:
        assert momentum_value(_cand("D", meta={"momentum_pct": "x"}, score=2.0)) == 2.0


class TestFilterTopMomentum:
    def test_keeps_top_slice(self) -> None:
        cands = [_cand(s, score=v) for s, v in [("A", 1), ("B", 9), ("C", 5), ("D", 7), ("E", 2)]]
        kept = {c.symbol for c in filter_top_momentum(cands, top_pct=40)}  # top 40% of 5 == 2
        assert kept == {"B", "D"}

    def test_pass_through_at_100(self) -> None:
        cands = [_cand("A", score=1), _cand("B", score=2)]
        assert filter_top_momentum(cands, top_pct=100) == cands

    def test_never_starves(self) -> None:
        cands = [_cand("A", score=1), _cand("B", score=2)]
        assert len(filter_top_momentum(cands, top_pct=0)) == 1  # keeps the single leader

    def test_empty(self) -> None:
        assert filter_top_momentum([], top_pct=30) == []
