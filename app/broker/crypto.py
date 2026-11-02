"""Crypto symbol handling.

Crypto is additive to the equities pipeline and, unlike US equities, trades
24/7. Alpaca names crypto pairs like ``BTC/USD``; yfinance uses ``BTC-USD``;
operators and configs may type ``BTCUSD``. This module is the single place that
recognizes and normalizes those forms so the rest of the pipeline can treat a
crypto symbol like any other instrument, tagged with ``AssetClass.CRYPTO``.

Pure functions only — no network, no settings — so the classification logic is
fully unit-testable.
"""

from __future__ import annotations

# The majors Alpaca paper supports with the deepest liquidity. This is the
# recognition set; the *tradable* set is the operator's ``crypto_symbols``
# config, which must be a subset of pairs Alpaca actually lists.
KNOWN_CRYPTO_BASES: frozenset[str] = frozenset(
    {"BTC", "ETH", "SOL", "LTC", "LINK", "AVAX", "BCH", "UNI", "AAVE", "DOGE", "DOT", "MATIC", "USDT", "USDC"}
)

_QUOTES: tuple[str, ...] = ("USD", "USDT", "USDC")


def _split(symbol: str) -> tuple[str, str] | None:
    """Return (base, quote) for a recognizable crypto symbol, else None."""

    raw = str(symbol or "").strip().upper()
    if not raw:
        return None
    # Explicit separators first: BTC/USD, BTC-USD.
    for sep in ("/", "-"):
        if sep in raw:
            base, _, quote = raw.partition(sep)
            base, quote = base.strip(), quote.strip()
            if base in KNOWN_CRYPTO_BASES and quote in _QUOTES:
                return base, quote
            return None
    # Concatenated: BTCUSD, ETHUSDT.
    for quote in sorted(_QUOTES, key=len, reverse=True):
        if raw.endswith(quote):
            base = raw[: -len(quote)]
            if base in KNOWN_CRYPTO_BASES:
                return base, quote
    return None


def is_crypto_symbol(symbol: str) -> bool:
    """True when ``symbol`` names a recognized crypto pair in any common form."""

    return _split(symbol) is not None


def to_alpaca_symbol(symbol: str) -> str:
    """Canonical Alpaca crypto form, e.g. ``BTC/USD``. Raises on non-crypto."""

    parts = _split(symbol)
    if parts is None:
        raise ValueError(f"{symbol!r} is not a recognized crypto symbol")
    base, quote = parts
    return f"{base}/{quote}"


def to_yfinance_symbol(symbol: str) -> str:
    """yfinance crypto form, e.g. ``BTC-USD``. Raises on non-crypto."""

    parts = _split(symbol)
    if parts is None:
        raise ValueError(f"{symbol!r} is not a recognized crypto symbol")
    base, quote = parts
    return f"{base}-{quote}"


def canonical(symbol: str) -> str:
    """The internal canonical form used across the pipeline (== Alpaca form)."""

    return to_alpaca_symbol(symbol)
