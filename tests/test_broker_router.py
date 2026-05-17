from __future__ import annotations

import pytest

from app.broker.router import BrokerRouter, NoBrokerForAssetClass
from app.models.approval import TradeProposal
from app.models.trade import AssetClass, TradeOrder


class FakeBroker:
    pass


def proposal(asset_class: AssetClass = AssetClass.EQUITY) -> TradeProposal:
    return TradeProposal(
        order=TradeOrder(
            symbol="NVDA" if asset_class == AssetClass.EQUITY else "GOLD",
            amount_usd=1000,
            proposed_price=120.0,
            stop_loss=114.0,
            asset_class=asset_class,
        )
    )


def test_router_selects_alpaca_for_equity_proposal() -> None:
    alpaca = FakeBroker()
    etoro = FakeBroker()
    router = BrokerRouter(alpaca_client=alpaca, etoro_client=etoro)

    assert router.select_broker_for(proposal(AssetClass.EQUITY)) is alpaca


def test_router_selects_etoro_for_non_equity_proposal() -> None:
    alpaca = FakeBroker()
    etoro = FakeBroker()
    router = BrokerRouter(alpaca_client=alpaca, etoro_client=etoro)

    assert router.select_broker_for(proposal(AssetClass.GOLD)) is etoro


def test_router_raises_when_choice_is_none_for_asset_class() -> None:
    router = BrokerRouter(
        alpaca_client=FakeBroker(),
        etoro_client=FakeBroker(),
        broker_for_non_equities="none",
    )

    with pytest.raises(NoBrokerForAssetClass, match="no broker configured"):
        router.select_broker_for(proposal(AssetClass.GOLD))


def test_router_raises_when_chosen_broker_is_unconfigured() -> None:
    router = BrokerRouter(alpaca_client=None, etoro_client=FakeBroker())

    with pytest.raises(NoBrokerForAssetClass, match="alpaca client not configured"):
        router.select_broker_for(proposal(AssetClass.EQUITY))


def test_router_all_clients_returns_only_configured_brokers() -> None:
    alpaca = FakeBroker()
    router = BrokerRouter(alpaca_client=alpaca, etoro_client=None)

    assert router.all_clients() == [alpaca]
