"""Instrument normalization and allow/block checks."""

from __future__ import annotations

from dataclasses import dataclass

from app.broker import crypto as crypto_symbols
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
        crypto = self._resolve_crypto(normalized)
        if crypto is not None:
            return crypto
        if normalized not in self.settings.allowed_instruments:
            raise ValueError(f"Instrument {normalized} is not in the allowed instrument list")
        return self._catalog_or_equity(normalized)

    def _resolve_crypto(self, normalized: str) -> SupportedInstrument | None:
        """Resolve a crypto pair when crypto trading is enabled and the pair is
        on the operator's ``crypto_symbols`` allowlist.

        Crypto uses its own allowlist (``crypto_symbols``) rather than
        ``allowed_instruments`` so enabling crypto never touches the equity
        allowlist. A crypto symbol the operator did not list raises, exactly
        like an equity that is not allowlisted.
        """

        if not bool(getattr(self.settings, "crypto_trading_enabled", False)):
            return None
        if not crypto_symbols.is_crypto_symbol(normalized):
            return None
        canonical = crypto_symbols.to_alpaca_symbol(normalized)
        allowed = {crypto_symbols.to_alpaca_symbol(s) for s in getattr(self.settings, "crypto_symbols", []) if crypto_symbols.is_crypto_symbol(s)}
        if canonical not in allowed:
            raise ValueError(f"Instrument {canonical} is not in the allowed crypto list")
        return SupportedInstrument(canonical, canonical, AssetClass.CRYPTO)

    def list_supported(self) -> list[SupportedInstrument]:
        """Return the supported and allowed instrument list (equities + crypto)."""

        supported: list[SupportedInstrument] = []
        for symbol in self.settings.allowed_instruments:
            normalized = str(symbol).upper().strip()
            if normalized and normalized not in self.settings.blocked_instruments:
                supported.append(self._catalog_or_equity(normalized))
        if bool(getattr(self.settings, "crypto_trading_enabled", False)):
            for symbol in getattr(self.settings, "crypto_symbols", []):
                if crypto_symbols.is_crypto_symbol(symbol):
                    canonical = crypto_symbols.to_alpaca_symbol(symbol)
                    supported.append(SupportedInstrument(canonical, canonical, AssetClass.CRYPTO))
        return supported

    @staticmethod
    def _catalog_or_equity(normalized: str) -> SupportedInstrument:
        """Catalogued instruments keep their metadata; any other allowlisted
        ticker is a plain US equity under its own symbol.

        The catalogue exists for the few names that need special handling
        (asset class, broker symbol mapping). Treating it as an exhaustive
        whitelist silently rejected every non-catalogued symbol at the proposal
        step on 2026-09-09 ("not supported in this version") — 15 of 15 that
        day — even though the operator's allowlist admitted them.
        """

        instrument = DEFAULT_INSTRUMENTS.get(normalized)
        if instrument is not None:
            return instrument
        if not normalized.replace(".", "").replace("-", "").isalnum():
            raise ValueError(f"Instrument {normalized} is not a valid ticker symbol")
        return SupportedInstrument(normalized, normalized, AssetClass.EQUITY)
