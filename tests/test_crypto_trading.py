"""Crypto instrument resolution and broker routing (additive to equities)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.broker.instrument_resolver import InstrumentResolver
from app.broker.router import BrokerRouter
from app.models.trade import AssetClass
from tests.conftest import make_settings


def _settings(tmp_path, **overrides):
    return make_settings(tmp_path, **overrides)


def test_crypto_resolves_to_crypto_asset_class_when_enabled(tmp_path) -> None:
    resolver = InstrumentResolver(_settings(tmp_path, crypto_trading_enabled=True, crypto_symbols=["BTC/USD", "ETH/USD"]))

    for raw in ("BTC/USD", "btc-usd", "BTCUSD"):
        instrument = resolver.resolve(raw)
        assert instrument.asset_class == AssetClass.CRYPTO
        assert instrument.symbol == "BTC/USD"
        assert instrument.broker_symbol == "BTC/USD"


def test_crypto_not_on_allowlist_is_rejected(tmp_path) -> None:
    resolver = InstrumentResolver(_settings(tmp_path, crypto_trading_enabled=True, crypto_symbols=["BTC/USD"]))
    with pytest.raises(ValueError, match="allowed crypto list"):
        resolver.resolve("ETH/USD")


def test_crypto_disabled_falls_through_to_equity_allowlist(tmp_path) -> None:
    # With crypto disabled, "BTCUSD" is treated as any other ticker: it must be
    # on the equity allowlist or it is rejected. The equity path is untouched.
    resolver = InstrumentResolver(_settings(tmp_path, crypto_trading_enabled=False, allowed_instruments=["AAPL"]))
    with pytest.raises(ValueError, match="allowed instrument list"):
        resolver.resolve("BTCUSD")


def test_equity_allowlist_is_unchanged_by_crypto(tmp_path) -> None:
    resolver = InstrumentResolver(
        _settings(tmp_path, crypto_trading_enabled=True, crypto_symbols=["BTC/USD"], allowed_instruments=["AAPL", "NVDA"])
    )
    assert resolver.resolve("AAPL").asset_class == AssetClass.EQUITY
    assert resolver.resolve("NVDA").asset_class == AssetClass.EQUITY


def test_list_supported_includes_crypto_when_enabled(tmp_path) -> None:
    resolver = InstrumentResolver(
        _settings(tmp_path, crypto_trading_enabled=True, crypto_symbols=["BTC/USD", "ETH/USD"], allowed_instruments=["AAPL"])
    )
    symbols = {i.symbol for i in resolver.list_supported()}
    assert {"AAPL", "BTC/USD", "ETH/USD"} <= symbols


def test_router_sends_crypto_to_the_crypto_broker(tmp_path) -> None:
    alpaca = SimpleNamespace(name="alpaca")
    etoro = SimpleNamespace(name="etoro")
    router = BrokerRouter(
        alpaca_client=alpaca,
        etoro_client=etoro,
        broker_for_equities="alpaca",
        broker_for_non_equities="etoro",
        broker_for_crypto="alpaca",
    )
    crypto_proposal = SimpleNamespace(order=SimpleNamespace(asset_class=AssetClass.CRYPTO))
    equity_proposal = SimpleNamespace(order=SimpleNamespace(asset_class=AssetClass.EQUITY))
    gold_proposal = SimpleNamespace(order=SimpleNamespace(asset_class=AssetClass.GOLD))

    assert router.select_broker_for(crypto_proposal) is alpaca
    assert router.selected_broker_name_for(crypto_proposal) == "alpaca"
    assert router.select_broker_for(equity_proposal) is alpaca
    assert router.select_broker_for(gold_proposal) is etoro  # non-equity → etoro, unchanged
