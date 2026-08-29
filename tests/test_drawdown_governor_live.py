"""The intraday drawdown governor scales new order size down as the day's
realized loss deepens, and is a no-op when disabled or flat/green."""

from __future__ import annotations

import math
from types import SimpleNamespace

from app.risk.volatility_target import daily_drawdown_pct, drawdown_governor_multiplier


class TestDailyDrawdownPct:
    def test_loss_becomes_positive_pct(self) -> None:
        # Down $2,000 on $100k equity == 2% daily drawdown.
        assert math.isclose(daily_drawdown_pct(daily_realized_pnl_usd=-2000, equity_usd=100_000), 2.0)

    def test_green_day_is_zero(self) -> None:
        assert daily_drawdown_pct(daily_realized_pnl_usd=1500, equity_usd=100_000) == 0.0


def _coordinator():
    """Bind the real _apply_drawdown_governor to a minimal stand-in with just the
    attributes it touches (settings + a logs sink)."""
    from app.execution.coordinator import ExecutionCoordinator

    obj = SimpleNamespace(logs=SimpleNamespace(log=lambda *a, **k: None))
    obj.settings = SimpleNamespace(
        drawdown_governor_enabled=True,
        drawdown_governor_soft_pct=2.0,
        drawdown_governor_hard_pct=5.0,
        drawdown_governor_floor=0.25,
    )
    obj._apply_drawdown_governor = ExecutionCoordinator._apply_drawdown_governor.__get__(obj)
    return obj


def _proposal(amount: float):
    order = SimpleNamespace(amount_usd=amount, symbol="NVDA")
    return SimpleNamespace(id="p1", order=order)


def test_governor_halves_size_midway_through_drawdown() -> None:
    coord = _coordinator()
    prop = _proposal(1000.0)
    # 3.5% daily drawdown is halfway from soft(2) to hard(5) -> multiplier 0.625.
    ctx = SimpleNamespace(daily_realized_pnl_usd=-3500.0, account_balance=100_000.0)
    coord._apply_drawdown_governor(prop, ctx)
    assert math.isclose(prop.order.amount_usd, 625.0, rel_tol=1e-6)


def test_governor_floor_caps_the_reduction() -> None:
    coord = _coordinator()
    prop = _proposal(1000.0)
    # Deep drawdown (8% > hard 5%) -> floored at 0.25.
    ctx = SimpleNamespace(daily_realized_pnl_usd=-8000.0, account_balance=100_000.0)
    coord._apply_drawdown_governor(prop, ctx)
    assert math.isclose(prop.order.amount_usd, 250.0, rel_tol=1e-6)


def test_no_change_when_shallow() -> None:
    coord = _coordinator()
    prop = _proposal(1000.0)
    ctx = SimpleNamespace(daily_realized_pnl_usd=-1000.0, account_balance=100_000.0)  # 1% < soft 2%
    coord._apply_drawdown_governor(prop, ctx)
    assert prop.order.amount_usd == 1000.0


def test_no_change_when_disabled() -> None:
    coord = _coordinator()
    coord.settings.drawdown_governor_enabled = False
    prop = _proposal(1000.0)
    ctx = SimpleNamespace(daily_realized_pnl_usd=-9000.0, account_balance=100_000.0)
    coord._apply_drawdown_governor(prop, ctx)
    assert prop.order.amount_usd == 1000.0
