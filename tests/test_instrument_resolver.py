"""The instrument catalogue must not act as a second, hidden allowlist.

On 2026-09-09 every auto-proposal (15/15) died with "not supported in this
version" because the resolver only knew six catalogued tickers, even though the
operator's ALLOWED_INSTRUMENTS admitted the whole 25-name universe.
"""

from __future__ import annotations

import pytest

from app.broker.instrument_resolver import InstrumentResolver
from app.models.trade import AssetClass
from tests.conftest import make_settings


def test_allowlisted_equity_outside_the_catalogue_resolves_as_equity(tmp_path) -> None:
    resolver = InstrumentResolver(make_settings(tmp_path, allowed_instruments=["INTC", "META", "GOOGL"]))

    intc = resolver.resolve("intc")

    assert intc.symbol == "INTC"
    assert intc.broker_symbol == "INTC"
    assert intc.asset_class is AssetClass.EQUITY


def test_catalogued_instruments_keep_their_metadata(tmp_path) -> None:
    resolver = InstrumentResolver(make_settings(tmp_path, allowed_instruments=["GOLD", "NVDA"]))

    assert resolver.resolve("GOLD").asset_class is AssetClass.GOLD
    assert resolver.resolve("NVDA").asset_class is AssetClass.EQUITY


def test_allowlist_and_blocklist_still_gate(tmp_path) -> None:
    resolver = InstrumentResolver(
        make_settings(tmp_path, allowed_instruments=["INTC", "OIL"], blocked_instruments=["OIL"])
    )

    with pytest.raises(ValueError, match="not in the allowed instrument list"):
        resolver.resolve("TSLA")
    with pytest.raises(ValueError, match="explicitly blocked"):
        resolver.resolve("OIL")


def test_list_supported_includes_every_allowlisted_symbol(tmp_path) -> None:
    resolver = InstrumentResolver(
        make_settings(tmp_path, allowed_instruments=["INTC", "GOLD", "OIL"], blocked_instruments=["OIL"])
    )

    assert [item.symbol for item in resolver.list_supported()] == ["INTC", "GOLD"]
