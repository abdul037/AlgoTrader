"""Point-in-time universe and historical data integrity validation."""

from __future__ import annotations

from datetime import date

import pandas as pd
from pydantic import BaseModel, Field


class UniverseMembership(BaseModel):
    symbol: str
    effective_from: date
    effective_to: date | None = None
    delisted: bool = False


class DataIntegrityReport(BaseModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class PointInTimeUniverse:
    """Resolve constituents as they existed on a historical date."""

    def __init__(self, memberships: list[UniverseMembership]):
        self.memberships = memberships

    def members_on(self, as_of: date) -> list[str]:
        return sorted(
            {
                item.symbol.upper()
                for item in self.memberships
                if item.effective_from <= as_of
                and (item.effective_to is None or as_of <= item.effective_to)
            }
        )


def validate_historical_frame(frame: pd.DataFrame) -> DataIntegrityReport:
    """Validate normalized OHLCV data before a production-grade audit."""

    required = {"timestamp", "open", "high", "low", "close", "volume"}
    errors: list[str] = []
    warnings: list[str] = []
    missing = sorted(required - set(frame.columns))
    if missing:
        return DataIntegrityReport(valid=False, errors=[f"missing_columns:{','.join(missing)}"])
    if frame.empty:
        return DataIntegrityReport(valid=False, errors=["empty_frame"])
    timestamps = pd.to_datetime(frame["timestamp"], utc=True, errors="coerce")
    if timestamps.isna().any():
        errors.append("invalid_timestamps")
    if timestamps.duplicated().any():
        errors.append("duplicate_timestamps")
    numeric = frame[["open", "high", "low", "close", "volume"]].apply(
        pd.to_numeric,
        errors="coerce",
    )
    if numeric.isna().any().any():
        errors.append("non_numeric_or_missing_ohlcv")
    if (numeric[["open", "high", "low", "close"]] <= 0).any().any():
        errors.append("non_positive_prices")
    if (numeric["volume"] < 0).any():
        errors.append("negative_volume")
    if (numeric["high"] < numeric[["open", "close", "low"]].max(axis=1)).any():
        errors.append("high_price_inconsistent")
    if (numeric["low"] > numeric[["open", "close", "high"]].min(axis=1)).any():
        errors.append("low_price_inconsistent")
    if not timestamps.is_monotonic_increasing:
        warnings.append("timestamps_not_sorted")
    return DataIntegrityReport(valid=not errors, errors=errors, warnings=warnings)
