"""Instrument normalization and allow/block checks."""

from __future__ import annotations

from dataclasses import dataclass

from app.config import AppSettings
from app.models.trade import AssetClass


@dataclass(frozen=True)
class SupportedInstrument:
    """Metadata for a supported trading instrument."""

    symbol: str
    broker_symbol: str
    asset_class: AssetClass


DEFAULT_INSTRUMENTS: dict[str, SupportedInstrument] = {
    "NVDA": SupportedInstrument("NVDA", "NVDA", AssetClass.EQUITY),
    "GOOG": SupportedInstrument("GOOG", "GOOG", AssetClass.EQUITY),
    "GOOGL": SupportedInstrument("GOOGL", "GOOGL", AssetClass.EQUITY),
    "AMD": SupportedInstrument("AMD", "AMD", AssetClass.EQUITY),
    "MU": SupportedInstrument("MU", "MU", AssetClass.EQUITY),
    "GOLD": SupportedInstrument("GOLD", "GOLD", AssetClass.GOLD),
}


class InstrumentResolver:
    """Resolve symbols and enforce supported instrument policy."""

    def __init__(self, settings: AppSettings):
        self.settings = settings

    def resolve(self, symbol: str) -> SupportedInstrument:
        """Resolve an instrument or raise an error."""

        normalized = symbol.upper().strip()
        if normalized in self.settings.blocked_instruments:
            raise ValueError(f"Instrument {normalized} is explicitly blocked")
        if normalized not in self.settings.allowed_instruments:
            raise ValueError(f"Instrument {normalized} is not in the allowed instrument list")
        instrument = DEFAULT_INSTRUMENTS.get(normalized)
        if instrument is None:
            raise ValueError(f"Instrument {normalized} is not supported in this version")
        return instrument

    def list_supported(self) -> list[SupportedInstrument]:
        """Return the supported and allowed instrument list."""

        supported: list[SupportedInstrument] = []
        for symbol in self.settings.allowed_instruments:
            instrument = DEFAULT_INSTRUMENTS.get(symbol)
            if instrument and symbol not in self.settings.blocked_instruments:
                supported.append(instrument)
        return supported
