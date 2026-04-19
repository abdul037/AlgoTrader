from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from app.screener.accuracy import build_accuracy_profile
from tests.conftest import make_settings


def _frame(closes: list[float], *, volume: float = 1_500_000) -> pd.DataFrame:
    timestamps = pd.date_range(start="2026-01-01T00:00:00Z", periods=len(closes), freq="1D", tz="UTC")
    return pd.DataFrame(
        [
            {
                "timestamp": timestamps[index],
                "open": close - 0.4,
                "high": close + 0.9,
                "low": close - 0.9,
                "close": close,
                "volume": volume,
            }
            for index, close in enumerate(closes)
        ]
    )


def _signal(*, price: float, confluence: float) -> SimpleNamespace:
    return SimpleNamespace(
        price=price,
        strategy_name="rsi_vwap_ema_confluence",
        metadata={
            "timeframe": "1d",
            "signal_role": "entry_long",
            "indicator_confluence_score": confluence,
        },
    )


def _context(*, price: float, relative_volume: float, efficiency_ratio: float) -> SimpleNamespace:
    return SimpleNamespace(
        current_price=price,
        relative_volume=relative_volume,
        atr_pct=1.4,
        efficiency_ratio=efficiency_ratio,
    )


def test_accuracy_profile_rewards_confirmed_clean_setup(tmp_path) -> None:
    closes = [100 + (index * 0.45) for index in range(80)] + [136, 137, 136.4, 138.2, 139.0]
    profile = build_accuracy_profile(
        _frame(closes, volume=2_500_000),
        signal=_signal(price=139.0, confluence=0.82),
        context=_context(price=139.0, relative_volume=1.8, efficiency_ratio=0.62),
        settings=make_settings(tmp_path),
    )

    assert profile.overall_score >= 0.52
    assert profile.confirmation_score >= 0.45
    assert profile.false_positive_risk_score <= 0.68
    assert "technical_confirmation_ok" in profile.pass_reasons


def test_accuracy_profile_flags_late_low_confirmation_setup(tmp_path) -> None:
    closes = [100 + ((-1) ** index * 0.35) for index in range(80)] + [101, 102, 103, 118, 131]
    profile = build_accuracy_profile(
        _frame(closes, volume=650_000),
        signal=_signal(price=131.0, confluence=0.18),
        context=_context(price=131.0, relative_volume=0.55, efficiency_ratio=0.12),
        settings=make_settings(tmp_path),
    )

    assert profile.overall_score < 0.52
    assert profile.false_positive_risk_score > 0.40
    assert profile.rejection_reasons
