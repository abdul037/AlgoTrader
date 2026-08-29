"""Tests for the regime router and its guarded wiring into strategy selection."""

from __future__ import annotations

from pathlib import Path

from app.backtesting.strategy_selection import strategy_specs_for
from app.screener.regime_router import (
    FAMILY_INTRADAY,
    FAMILY_MEAN_REVERSION,
    FAMILY_MOMENTUM,
    RegimeSignal,
    allowed_families,
    family_for_style,
    regime_signal_from_scores,
    route_specs,
)
from tests.conftest import make_settings


class _Spec:
    def __init__(self, name: str, style: str):
        self.name = name
        self.style = style


class TestAllowedFamilies:
    def test_healthy_uptrend_runs_momentum(self) -> None:
        fams = allowed_families(RegimeSignal(trend="up", volatility="normal", breadth="strong"))
        assert FAMILY_MOMENTUM in fams and FAMILY_INTRADAY in fams
        assert FAMILY_MEAN_REVERSION not in fams

    def test_high_vol_uptrend_favors_mean_reversion(self) -> None:
        fams = allowed_families(RegimeSignal(trend="up", volatility="high", breadth="strong"))
        assert fams == {FAMILY_MEAN_REVERSION, FAMILY_INTRADAY}

    def test_narrow_uptrend_drops_momentum(self) -> None:
        fams = allowed_families(RegimeSignal(trend="up", volatility="normal", breadth="weak"))
        assert FAMILY_MOMENTUM not in fams

    def test_downtrend_is_defensive(self) -> None:
        assert allowed_families(RegimeSignal(trend="down")) == {FAMILY_MEAN_REVERSION}

    def test_neutral_is_mean_reversion_and_intraday(self) -> None:
        assert allowed_families(RegimeSignal(trend="neutral")) == {FAMILY_MEAN_REVERSION, FAMILY_INTRADAY}


class TestStyleMapping:
    def test_known_and_unknown_styles(self) -> None:
        assert family_for_style("breakout") == FAMILY_MOMENTUM
        assert family_for_style("mean_reversion") == FAMILY_MEAN_REVERSION
        assert family_for_style("scalp") == FAMILY_INTRADAY
        assert family_for_style("some_new_style") == FAMILY_MOMENTUM  # safe default


class TestRouteSpecs:
    def test_downtrend_keeps_only_mean_reversion(self) -> None:
        specs = [_Spec("a", "breakout"), _Spec("b", "mean_reversion"), _Spec("c", "intraday")]
        kept = route_specs(specs, RegimeSignal(trend="down"))
        assert [s.name for s in kept] == ["b"]

    def test_healthy_uptrend_keeps_momentum_and_intraday(self) -> None:
        specs = [_Spec("a", "breakout"), _Spec("b", "mean_reversion"), _Spec("c", "intraday")]
        kept = {s.name for s in route_specs(specs, RegimeSignal(trend="up", breadth="strong"))}
        assert kept == {"a", "c"}


class TestRegimeSignalFromScores:
    def test_maps_scores_and_vol_label(self) -> None:
        sig = regime_signal_from_scores(trend_score=0.8, breadth_score=0.3, volatility_environment="volatile")
        assert sig.trend == "up" and sig.breadth == "weak" and sig.volatility == "high"

    def test_missing_inputs_are_neutral(self) -> None:
        sig = regime_signal_from_scores(trend_score=None, breadth_score=None, volatility_environment=None)
        assert sig == RegimeSignal(trend="neutral", volatility="normal", breadth="neutral")


class TestSelectionWiring:
    def test_no_op_when_flag_disabled(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path, regime_router_enabled=False)
        baseline = strategy_specs_for(settings, timeframe="1d")
        # Even with a downtrend regime, a disabled router changes nothing.
        routed = strategy_specs_for(settings, timeframe="1d", regime=RegimeSignal(trend="down"))
        assert [s.name for s in routed] == [s.name for s in baseline]

    def test_no_op_when_regime_absent(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path, regime_router_enabled=True)
        baseline = strategy_specs_for(settings, timeframe="1d")
        assert strategy_specs_for(settings, timeframe="1d", regime=None) == baseline

    def test_filters_when_enabled_with_regime(self, tmp_path: Path) -> None:
        settings = make_settings(tmp_path, regime_router_enabled=True)
        baseline = strategy_specs_for(settings, timeframe="1d")
        routed = strategy_specs_for(settings, timeframe="1d", regime=RegimeSignal(trend="down"))
        # Downtrend keeps only mean-reversion-family specs -> strictly fewer, and
        # every survivor is a mean-reversion/reversal style.
        assert 0 < len(routed) < len(baseline)
        assert all(family_for_style(s.style) == FAMILY_MEAN_REVERSION for s in routed)
