import pytest

from app.backtesting.financing import overnight_financing_cost


def test_zero_days_held_zero_cost() -> None:
    assert overnight_financing_cost(10_000.0, 0, annual_rate=0.07) == pytest.approx(0.0)
    assert overnight_financing_cost(10_000.0, -1, annual_rate=0.07) == pytest.approx(0.0)


def test_one_day_long_at_seven_pct_annual() -> None:
    result = overnight_financing_cost(10_000.0, 1, annual_rate=0.07)

    assert result == pytest.approx(10_000.0 * 0.07 / 365.0)


def test_thirty_days_held_proportional() -> None:
    one_day = overnight_financing_cost(25_000.0, 1, annual_rate=0.07)
    thirty_days = overnight_financing_cost(25_000.0, 30, annual_rate=0.07)

    assert thirty_days == pytest.approx(25_000.0 * 0.07 * (30 / 365.0))
    assert thirty_days == pytest.approx(one_day * 30)
