"""Broker selection for execution routing."""

from __future__ import annotations

from typing import Literal

from app.models.approval import TradeProposal


class NoBrokerForAssetClass(Exception):
    """Raised when no broker is configured for the proposal's asset class."""


class BrokerRouter:
    """Select the broker that should execute a proposal."""

    def __init__(
        self,
        *,
        alpaca_client=None,
        etoro_client=None,
        broker_for_equities: Literal["alpaca", "etoro", "none"] = "alpaca",
        broker_for_non_equities: Literal["alpaca", "etoro", "none"] = "etoro",
    ):
        self._alpaca = alpaca_client
        self._etoro = etoro_client
        self._equities_choice = broker_for_equities
        self._non_equities_choice = broker_for_non_equities

    def select_broker_for(self, proposal: TradeProposal):
        asset_class = self._asset_class_of(proposal)
        choice = self._equities_choice if asset_class == "equity" else self._non_equities_choice
        if choice == "alpaca":
            if self._alpaca is None:
                raise NoBrokerForAssetClass(f"alpaca client not configured for asset_class={asset_class}")
            return self._alpaca
        if choice == "etoro":
            if self._etoro is None:
                raise NoBrokerForAssetClass(f"etoro client not configured for asset_class={asset_class}")
            return self._etoro
        raise NoBrokerForAssetClass(f"no broker configured for asset_class={asset_class}")

    def selected_broker_name_for(self, proposal: TradeProposal) -> str:
        """Return the configured broker name for a proposal's asset class."""

        asset_class = self._asset_class_of(proposal)
        return self._equities_choice if asset_class == "equity" else self._non_equities_choice

    def all_clients(self) -> list:
        """Return configured clients for emergency-stop fanout."""

        return [client for client in (self._alpaca, self._etoro) if client is not None]

    def _asset_class_of(self, proposal: TradeProposal) -> str:
        asset_class = getattr(proposal.order, "asset_class", None)
        if asset_class is None:
            return "equity"
        if hasattr(asset_class, "value"):
            value = asset_class.value
        else:
            value = str(asset_class)
        normalized = str(value or "").strip().lower()
        if normalized == "":
            return "equity"
        return normalized
