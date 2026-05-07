import pytest

from app.backtesting.slippage import SlippageModel


def test_slippage_floor_is_half_spread() -> None:
    model = SlippageModel(default_half_spread_bps=5.0)

    result = model.slippage_bps(
        symbol="NVDA",
        side="long",
        position_notional=0.0,
        avg_dollar_volume_30d=10_000_000.0,
    )

    assert result == pytest.approx(5.0)


def test_slippage_increases_with_position_size() -> None:
    model = SlippageModel(default_half_spread_bps=5.0, k=1.0)

    small = model.slippage_bps(
        symbol="NVDA",
        side="long",
        position_notional=1_000.0,
        avg_dollar_volume_30d=10_000_000.0,
    )
    large = model.slippage_bps(
        symbol="NVDA",
        side="long",
        position_notional=100_000.0,
        avg_dollar_volume_30d=10_000_000.0,
    )

    assert small == pytest.approx(5.0)
    assert large == pytest.approx(100.0)
    assert large > small


def test_slippage_zero_volume_returns_heavy_penalty() -> None:
    model = SlippageModel(default_half_spread_bps=5.0)

    result = model.slippage_bps(
        symbol="NVDA",
        side="long",
        position_notional=10_000.0,
        avg_dollar_volume_30d=0.0,
    )

    assert result == pytest.approx(25.0)


def test_long_entry_pays_higher_than_midpoint() -> None:
    model = SlippageModel()

    result = model.adjust_fill(side="long", action="entry", midpoint=100.0, slippage_bps=10.0)

    assert result == pytest.approx(100.10)
    assert result > 100.0


def test_long_exit_pays_lower_than_midpoint() -> None:
    model = SlippageModel()

    result = model.adjust_fill(side="long", action="exit", midpoint=100.0, slippage_bps=10.0)

    assert result == pytest.approx(99.90)
    assert result < 100.0


def test_short_entry_lower_short_exit_higher() -> None:
    model = SlippageModel()

    entry = model.adjust_fill(side="short", action="entry", midpoint=100.0, slippage_bps=10.0)
    exit_ = model.adjust_fill(side="short", action="exit", midpoint=100.0, slippage_bps=10.0)

    assert entry == pytest.approx(99.90)
    assert exit_ == pytest.approx(100.10)
    assert entry < 100.0 < exit_


def test_adjust_fill_zero_slippage_returns_midpoint() -> None:
    model = SlippageModel()

    long_entry = model.adjust_fill(side="long", action="entry", midpoint=100.0, slippage_bps=0.0)
    short_exit = model.adjust_fill(side="short", action="exit", midpoint=100.0, slippage_bps=0.0)

    assert long_entry == pytest.approx(100.0)
    assert short_exit == pytest.approx(100.0)
