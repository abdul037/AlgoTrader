from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from app.signals.evaluation import _risk_floor, atr_from_candles


def _flat_range_candles(rows: int, *, close: float = 100.0, half_range: float = 1.0) -> pd.DataFrame:
    # Constant true range of 2*half_range per bar -> ATR converges to 2*half_range.
    return pd.DataFrame(
        {
            "high": [close + half_range] * rows,
            "low": [close - half_range] * rows,
            "close": [close] * rows,
        }
    )


def _service(**overrides):
    base = dict(
        live_signal_atr_stop_enabled=True,
        live_signal_atr_period=14,
        live_signal_atr_stop_mult=1.5,
    )
    base.update(overrides)
    return SimpleNamespace(settings=SimpleNamespace(**base))


def test_atr_zero_when_too_few_bars() -> None:
    assert atr_from_candles(_flat_range_candles(5), period=14) == 0.0


def test_atr_matches_constant_true_range() -> None:
    atr = atr_from_candles(_flat_range_candles(20, half_range=1.0), period=14)
    assert atr == 2.0  # high-low = 2 every bar


def test_risk_floor_uses_atr_when_available() -> None:
    floor = _risk_floor(_service(), _flat_range_candles(20, half_range=1.0), entry_price=100.0, pct_floor=0.02)
    assert floor == 3.0  # 1.5 * ATR(2.0), beats the 2% (=2.0) percentage floor


def test_risk_floor_falls_back_to_pct_when_atr_unavailable() -> None:
    floor = _risk_floor(_service(), _flat_range_candles(5), entry_price=100.0, pct_floor=0.02)
    assert floor == 2.0  # too few bars -> 2% of 100


def test_risk_floor_falls_back_when_disabled() -> None:
    floor = _risk_floor(
        _service(live_signal_atr_stop_enabled=False),
        _flat_range_candles(20, half_range=5.0),
        entry_price=100.0,
        pct_floor=0.02,
    )
    assert floor == 2.0  # ATR disabled -> percentage floor regardless of volatility
