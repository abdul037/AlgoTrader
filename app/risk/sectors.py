"""Symbol -> sector and correlation-bucket mapping for concentration limits.

Reuses the canonical sector-ETF proxy map (``SECTOR_ETF_BY_SYMBOL``). Symbols
that share a sector ETF are treated as one sector; related sector ETFs are then
grouped into broader correlation buckets so the correlated-exposure cap catches
complex-wide concentration (e.g. the entire tech complex) that a single
per-sector cap would miss.

These helpers give the risk-context builder and the guardrails a single,
consistent way to classify a symbol, so accumulated sector/correlation exposure
is actually measured against existing positions rather than only the new order.
"""

from __future__ import annotations

from app.intelligence.market_regime import SECTOR_ETF_BY_SYMBOL

UNKNOWN_SECTOR = "unknown"
UNKNOWN_BUCKET = "unclassified"

# Sector ETF proxy -> broad correlation bucket. The tech complex is deliberately
# broad: mega-cap tech, semis, software, cybersecurity, comms, and growth all
# move together, which is exactly the concentration a per-sector cap alone
# would let through in this tech-heavy universe.
_CORRELATION_BUCKET_BY_SECTOR_ETF: dict[str, str] = {
    "XLK": "tech_complex",
    "SMH": "tech_complex",
    "IGV": "tech_complex",
    "HACK": "tech_complex",
    "XLC": "tech_complex",
    "IWF": "tech_complex",
    "XLY": "consumer_cyclical",
    "XLP": "consumer_staples",
    "XLF": "financials",
    "XLV": "healthcare",
    "XLE": "energy",
    "XLI": "industrials",
    "XLB": "materials",
}


def sector_for_symbol(symbol: str) -> str:
    """Return the sector-ETF proxy for a symbol, or ``"unknown"``."""

    return SECTOR_ETF_BY_SYMBOL.get(str(symbol or "").upper(), UNKNOWN_SECTOR)


def correlation_bucket_for_symbol(symbol: str) -> str:
    """Return the broad correlation bucket for a symbol, or ``"unclassified"``."""

    return _CORRELATION_BUCKET_BY_SECTOR_ETF.get(sector_for_symbol(symbol), UNKNOWN_BUCKET)
