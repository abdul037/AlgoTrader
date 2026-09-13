"""Crypto symbol recognition and normalization (pure logic)."""

from __future__ import annotations

import pytest

from app.broker import crypto


@pytest.mark.parametrize(
    "raw,alpaca,yf",
    [
        ("BTC/USD", "BTC/USD", "BTC-USD"),
        ("btc/usd", "BTC/USD", "BTC-USD"),
        ("BTC-USD", "BTC/USD", "BTC-USD"),
        ("BTCUSD", "BTC/USD", "BTC-USD"),
        ("ETHUSDT", "ETH/USDT", "ETH-USDT"),
        (" sol/usd ", "SOL/USD", "SOL-USD"),
    ],
)
def test_recognizes_and_normalizes_crypto(raw, alpaca, yf) -> None:
    assert crypto.is_crypto_symbol(raw) is True
    assert crypto.to_alpaca_symbol(raw) == alpaca
    assert crypto.to_yfinance_symbol(raw) == yf
    assert crypto.canonical(raw) == alpaca


@pytest.mark.parametrize("raw", ["AAPL", "GOOGL", "SPY", "", "OIL", "BTC", "USD", "XYZ/USD", "BTC/EUR"])
def test_rejects_non_crypto(raw) -> None:
    assert crypto.is_crypto_symbol(raw) is False
    with pytest.raises(ValueError):
        crypto.to_alpaca_symbol(raw)


def test_equity_ticker_is_never_crypto() -> None:
    # A three-letter equity that happens to look like a base is not crypto
    # unless it carries a recognized USD quote.
    assert crypto.is_crypto_symbol("LINK") is False  # bare base, no quote
    assert crypto.is_crypto_symbol("LINK/USD") is True
