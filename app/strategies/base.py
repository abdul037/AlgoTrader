"""Base strategy contract."""

from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

from app.models.signal import Signal


class BaseStrategy(ABC):
    """Strategy interface."""

    name: str = "base"
    required_bars: int = 20

    @abstractmethod
    def generate_signal(self, data: pd.DataFrame, symbol: str) -> Signal | None:
        """Generate a signal from normalized OHLCV data."""

    def _ensure_length(self, data: pd.DataFrame) -> bool:
        return len(data) >= self.required_bars
