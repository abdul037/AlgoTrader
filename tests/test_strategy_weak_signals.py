from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from app.models.signal import SignalAction
from app.strategies.base import BaseStrategy
from app.strategies.weak_signals import (
    build_supervised_weak_long_signal,
    configure_weak_signal_emission,
)


class DummyWeakStrategy(BaseStrategy):
    name = "vwap_reclaim"

    def generate_signal(self, data: pd.DataFrame, symbol: str):
        return None


def _enabled_strategy() -> DummyWeakStrategy:
    strategy = DummyWeakStrategy()
    strategy._paper_weak_signal_enabled = True
    strategy._paper_weak_signal_min_reward_to_risk = 1.0
    return strategy


def _build(strategy: DummyWeakStrategy, **overrides):
    payload = {
        "symbol": "NVDA",
        "price": 100.0,
        "stop": 98.0,
        "risk_multiple": 1.2,
        "rationale": "Supervised weak-valid fixture",
        "confidence": 0.5,
        "metadata": {"style": "fixture"},
        "rejection_reasons": ["relative_volume_too_low"],
        "setup_anchor": True,
    }
    payload.update(overrides)
    return build_supervised_weak_long_signal(strategy, **payload)


def test_configure_weak_signal_emission_requires_paper_supervised_allowed_strategy() -> None:
    settings = SimpleNamespace(
        paper_strategy_weak_signal_emission_enabled=True,
        execution_mode="paper",
        enable_real_trading=False,
        paper_supervised_weak_valid_enabled=True,
        paper_strategy_weak_signal_allowed_strategies=["vwap_reclaim"],
        paper_supervised_weak_valid_min_reward_to_risk=1.0,
    )

    strategy = configure_weak_signal_emission(DummyWeakStrategy(), settings)

    assert strategy._paper_weak_signal_enabled is True
    assert strategy._paper_weak_signal_min_reward_to_risk == 1.0


def test_configure_weak_signal_emission_blocks_live_or_disallowed_strategy() -> None:
    settings = SimpleNamespace(
        paper_strategy_weak_signal_emission_enabled=True,
        execution_mode="live",
        enable_real_trading=False,
        paper_supervised_weak_valid_enabled=True,
        paper_strategy_weak_signal_allowed_strategies=["momentum_breakout"],
        paper_supervised_weak_valid_min_reward_to_risk=1.0,
    )

    strategy = configure_weak_signal_emission(DummyWeakStrategy(), settings)

    assert strategy._paper_weak_signal_enabled is False


def test_build_supervised_weak_long_signal_accepts_valid_long_setup() -> None:
    signal = _build(_enabled_strategy())

    assert signal is not None
    assert signal.action == SignalAction.BUY
    assert signal.stop_loss < signal.price < signal.take_profit
    assert signal.metadata["signal_classification"] == "supervised_weak_valid"
    assert signal.metadata["source"] == "supervised_weak_valid"
    assert signal.metadata["supervised_approval_required"] is True
    assert signal.metadata["production_qualified"] is False
    assert signal.metadata["weak_signal_reasons"] == ["relative_volume_too_low"]


def test_build_supervised_weak_long_signal_rejects_disabled_or_no_anchor() -> None:
    disabled = DummyWeakStrategy()
    disabled._paper_weak_signal_enabled = False

    assert _build(disabled) is None
    assert _build(_enabled_strategy(), setup_anchor=False) is None


def test_build_supervised_weak_long_signal_rejects_invalid_bracket_or_missing_price() -> None:
    strategy = _enabled_strategy()

    assert _build(strategy, stop=100.5) is None
    assert _build(strategy, price=None) is None
    assert _build(strategy, stop=None) is None


def test_build_supervised_weak_long_signal_rejects_reward_to_risk_below_minimum() -> None:
    strategy = _enabled_strategy()
    strategy._paper_weak_signal_min_reward_to_risk = 1.0

    assert _build(strategy, risk_multiple=0.95) is None
