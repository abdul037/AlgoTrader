from __future__ import annotations

from datetime import date

import pandas as pd

from app.research.data_integrity import (
    PointInTimeUniverse,
    UniverseMembership,
    validate_historical_frame,
)
from app.universe.liquidity import LiquiditySnapshot, build_liquidity_universe


def test_point_in_time_universe_avoids_survivorship_bias():
    universe = PointInTimeUniverse(
        [
            UniverseMembership(
                symbol="OLD",
                effective_from=date(2020, 1, 1),
                effective_to=date(2022, 12, 31),
                delisted=True,
            ),
            UniverseMembership(symbol="NEW", effective_from=date(2023, 1, 1)),
        ]
    )

    assert universe.members_on(date(2022, 6, 1)) == ["OLD"]
    assert universe.members_on(date(2024, 6, 1)) == ["NEW"]


def test_historical_frame_rejects_invalid_ohlcv():
    frame = pd.DataFrame(
        [
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "open": 100,
                "high": 90,
                "low": 95,
                "close": 105,
                "volume": 1000,
            }
        ]
    )

    report = validate_historical_frame(frame)

    assert report.valid is False
    assert "high_price_inconsistent" in report.errors


def test_dynamic_liquidity_universe_ranks_and_filters():
    result = build_liquidity_universe(
        [
            LiquiditySnapshot(
                symbol="AAPL",
                price=200,
                average_volume=10_000_000,
                average_dollar_volume=2_000_000_000,
                spread_bps=2,
            ),
            LiquiditySnapshot(
                symbol="ILLIQUID",
                price=10,
                average_volume=100,
                average_dollar_volume=1000,
                spread_bps=500,
            ),
        ],
        min_price=5,
        min_average_volume=1_000_000,
        min_average_dollar_volume=20_000_000,
        max_spread_bps=50,
        limit=100,
    )

    assert result == ["AAPL"]
