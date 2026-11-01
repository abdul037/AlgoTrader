"""At most N open paper positions per GICS sector on a new buy (operator 2026-11-01)."""

from __future__ import annotations

import pytest

from app.models.trade import TradeOrder
from app.risk.guardrails import RiskContext, RiskManager
from app.risk.sectors import UNKNOWN_SECTOR, gics_sector_for_symbol
from tests.conftest import make_settings

CAP = "Open-position cap of 2 reached for the"


def _order(symbol: str, **extra) -> TradeOrder:
    return TradeOrder(symbol=symbol, amount_usd=1000, leverage=1, proposed_price=100.0, stop_loss=98.0, take_profit=104.0, **extra)


def _manager(tmp_path, **values) -> RiskManager:
    values.setdefault("allowed_instruments", ["NVDA", "AAPL", "MSFT", "CRM", "COST", "WMT", "PG", "SPY", "PYPL", "JPM"])
    return RiskManager(make_settings(tmp_path, max_open_positions=20, **values))


def _context(*held: str) -> RiskContext:
    return RiskContext(account_balance=100_000.0, open_positions=len(held), positions_by_symbol={s: 1 for s in held})


def _capped(result) -> list[str]:
    return [reason for reason in result.reasons if reason.startswith(CAP)]


@pytest.mark.parametrize(
    ("symbol", "sector"),
    [("NVDA", "information_technology"), ("CRM", "information_technology"), ("CRWD", "information_technology"),
     ("PLTR", "information_technology"), ("META", "communication_services"), ("WMT", "consumer_staples"),
     ("PYPL", "financials"), ("SPY", UNKNOWN_SECTOR), ("ZZZZ", UNKNOWN_SECTOR)],
)
def test_the_sector_etf_proxies_fold_into_gics_sectors(symbol, sector) -> None:
    assert gics_sector_for_symbol(symbol) == sector


def test_a_third_open_position_in_one_sector_is_refused(tmp_path) -> None:
    manager = _manager(tmp_path)
    held = _context("AAPL", "CRM", "WMT")  # chips, hardware and software are one sector
    assert _capped(manager.validate_order(_order("NVDA"), held)) == [f"{CAP} information technology sector"]
    assert _capped(manager.validate_order(_order("COST"), held)) == []  # one staples name held
    assert _capped(manager.validate_order(_order("NVDA"), _context("AAPL", "WMT"))) == []


def test_index_funds_unmapped_names_and_sells_are_never_capped(tmp_path) -> None:
    manager = _manager(tmp_path)
    held = _context("AAPL", "CRM", "SPY")
    assert _capped(manager.validate_order(_order("SPY"), _context("SPY", "QQQ", "IWM"))) == []
    assert _capped(manager.validate_order(_order("NVDA", side="sell"), held)) == []


def test_while_etoro_decides_the_room_paper_holdings_do_not_block_the_copy(tmp_path) -> None:
    manager = _manager(tmp_path)
    held = _context("AAPL", "CRM")
    assert _capped(manager.validate_order(_order("NVDA"), held, etoro_room=[])) == []


def test_zero_turns_it_off(tmp_path) -> None:
    manager = _manager(tmp_path, max_open_positions_per_sector=0)
    assert _capped(manager.validate_order(_order("NVDA"), _context("AAPL", "CRM", "MSFT"))) == []
