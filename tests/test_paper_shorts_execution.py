"""Shorts step 3: execution of a paper short on Alpaca paper (docs/notes/paper_shorts.md).

* the Alpaca adapter sends a sell-to-open bracket only when asked (``opens_short``): a market SELL
  whose stop leg is a buy-stop ABOVE the entry and whose target leg is a buy-limit BELOW it; any
  other SELL bracket is refused as before; the asset and account facts and a short's negative
  quantity are read from Alpaca;
* the borrow and margin facts come from Alpaca's asset data (the ETB borrow rate from the cost
  model, a hard-to-borrow name has none), and a failed read refuses;
* the coordinator re-reads them just before the risk gate, checks the short bracket at the fresh
  quote (stop above, target below, reward:risk from below), sizes whole shares from the notional,
  never sends a short without its bracket, never turns a refused short into a simulated one, and
  refuses any SELL that is not a tagged paper short as before (``long_entries_only``).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from alpaca.trading.enums import OrderClass, OrderSide
from alpaca.trading.requests import MarketOrderRequest

from app.execution.coordinator import ExecutionCoordinator
from app.execution.short_orders import bracket_kwargs, refresh_short_facts, short_bracket_reasons
from app.main import create_app
from app.models.approval import ApprovalDecisionRequest, TradeProposalCreate
from app.models.execution import ExecutionStatus
from app.models.execution_queue import ExecutionQueueStatus
from app.models.trade import AssetClass, TradeOrder
from app.performance.paper_only import AUTO_REVIEWER
from app.risk.short_side import BORROW_COST_SOURCE, short_asset_facts, short_fact_blockers
from tests.conftest import MockBroker, make_settings
from tests.test_alpaca_client import _client
from tests.test_execution_coordinator_alpaca_routing import (
    FakeAlpacaClient,
    FakeEtoroMarketData,
    execution_for,
)
from tests.test_paper_shorts_etoro import ON, PAPER_SHORT

ASSET = {
    "symbol": "NVDA",
    "tradable": True,
    "marginable": True,
    "shortable": True,
    "easy_to_borrow": True,
    "fractionable": True,
    "maintenance_margin_requirement": 30.0,
    "status": "active",
    "borrow_cost_available": False,
}


class FakeShortAlpaca(FakeAlpacaClient):
    """Alpaca paper with asset and account data and a sell-to-open bracket."""

    def __init__(self, *, asset=None, shorting=True, fail_facts=False, **kw):
        super().__init__(**kw)
        self.asset = dict(ASSET if asset is None else asset)
        self.shorting = shorting
        self.fail_facts = fail_facts
        self.fact_reads = 0

    def get_asset_capabilities(self, symbol):
        self.fact_reads += 1
        if self.fail_facts:
            raise RuntimeError("alpaca 503")
        return {**self.asset, "symbol": symbol}

    def get_account_identity(self):
        return {"account_number": "PA1", "shorting_enabled": self.shorting, "trading_blocked": False}

    def submit_bracket_order(self, *, opens_short=False, **kwargs):
        record = super().submit_bracket_order(**kwargs)
        self.submitted_orders[-1]["opens_short"] = opens_short
        return record


# --- the Alpaca adapter ----------------------------------------------------------------------


def test_the_adapter_sends_a_sell_to_open_bracket_only_when_asked(monkeypatch) -> None:
    client, trading, _ = _client(monkeypatch)
    client.submit_bracket_order(
        symbol="AAPL",
        side="sell",
        qty=7,
        stop_loss_price=210.0,
        take_profit_price=190.0,
        time_in_force="gtc",
        client_order_id="cid-short",
        opens_short=True,
    )
    request = trading.submitted_requests[0]
    assert isinstance(request, MarketOrderRequest)
    assert request.side == OrderSide.SELL and request.order_class == OrderClass.BRACKET
    assert request.qty == 7
    assert request.stop_loss.stop_price == 210.0  # a buy-stop above the entry
    assert request.take_profit.limit_price == 190.0  # a buy-limit below it
    with pytest.raises(ValueError, match="long entries only"):  # a SELL without the flag
        client.submit_bracket_order(symbol="AAPL", side="sell", qty=1, stop_loss_price=210.0, take_profit_price=190.0)
    with pytest.raises(ValueError, match="above take-profit for a short"):
        client.submit_bracket_order(
            symbol="AAPL", side="sell", qty=1, stop_loss_price=190.0, take_profit_price=210.0, opens_short=True
        )
    with pytest.raises(ValueError, match="whole share"):
        client.submit_bracket_order(
            symbol="AAPL", side="sell", qty=0, stop_loss_price=210.0, take_profit_price=190.0, opens_short=True
        )
    assert len(trading.submitted_requests) == 1


def test_the_adapter_reads_short_facts_and_a_short_quantity(monkeypatch) -> None:
    client, trading, _ = _client(monkeypatch)
    trading.get_asset = lambda symbol: SimpleNamespace(
        symbol=symbol, asset_class="us_equity", status="active", tradable=True, marginable=True,
        shortable=True, easy_to_borrow=False, fractionable=True, maintenance_margin_requirement=50.0,
    )
    caps = client.get_asset_capabilities("GME")
    assert caps["easy_to_borrow"] is False and caps["maintenance_margin_requirement"] == 50.0
    assert caps["borrow_cost_available"] is False
    trading.get_account = lambda: SimpleNamespace(
        id="1", account_number="PA1", status="ACTIVE", trading_blocked=False, shorting_enabled=True,
        equity="1", last_equity="1", cash="1",
    )
    assert client.get_account_identity()["shorting_enabled"] is True
    trading.get_all_positions = lambda: [
        SimpleNamespace(symbol="TSLA", qty="5", side="short", avg_entry_price="200", market_value="-1000", unrealized_pl="3"),
        SimpleNamespace(symbol="AAPL", qty="3", side="long", avg_entry_price="150", market_value="450", unrealized_pl="1"),
    ]
    positions = {p.symbol: p for p in client.get_portfolio().positions}
    assert positions["TSLA"].quantity == -5.0 and positions["TSLA"].is_buy is False
    assert positions["AAPL"].quantity == 3.0 and positions["AAPL"].is_buy is True


def test_borrow_and_margin_facts_come_from_alpaca() -> None:
    etb = short_asset_facts(FakeShortAlpaca(), "nvda")
    assert etb["opens_short"] is True and etb["position_side"] == "short"
    assert etb["easy_to_borrow"] is True and etb["margin_requirement_pct"] == 30.0
    assert etb["borrow_cost_annual_pct"] == pytest.approx(0.30)
    assert etb["borrow_cost_source"] == BORROW_COST_SOURCE
    assert short_fact_blockers(etb) == []
    htb = short_asset_facts(FakeShortAlpaca(asset={**ASSET, "easy_to_borrow": False}), "GME")
    assert htb["borrow_cost_annual_pct"] is None and "hard_to_borrow" in short_fact_blockers(htb)
    bare = short_asset_facts(
        FakeShortAlpaca(asset={**ASSET, "shortable": False, "maintenance_margin_requirement": None}, shorting=False),
        "X",
    )
    assert set(short_fact_blockers(bare)) == {
        "not_shortable",
        "margin_requirement_missing",
        "account_shorting_disabled",
    }
    with pytest.raises(RuntimeError):
        short_asset_facts(FakeShortAlpaca(fail_facts=True), "NVDA")


# --- the coordinator -------------------------------------------------------------------------


def _short_order(**metadata):
    return TradeOrder(
        symbol="NVDA", side="sell", amount_usd=10_000.0, proposed_price=120.0,
        stop_loss=126.0, take_profit=108.0, strategy_name="trend_following", asset_class=AssetClass.EQUITY,
        metadata={**PAPER_SHORT, "timeframe": "1d", **metadata},
    )


def test_the_short_bracket_rules_at_the_quote(tmp_path) -> None:
    settings = make_settings(tmp_path, **ON)
    proposal = SimpleNamespace(order=_short_order())
    reasons = ExecutionCoordinator._alpaca_bracket_reasons
    # Reward:risk of a short = (quote - target) / (stop - quote): a fall in the quote shrinks it.
    assert reasons(proposal, 120.0, 1.5, settings=settings) == []  # 12 / 6 = 2.0
    assert reasons(proposal, 122.0, 1.5, settings=settings) == []  # 14 / 4 = 3.5
    assert reasons(proposal, 117.0, 1.5, settings=settings) == ["reward_to_risk_below_min_at_quote"]  # 9 / 9
    assert short_bracket_reasons(settings, proposal, 117.0, 0.0) == []
    assert reasons(proposal, 127.0, 0.0, settings=settings) == ["stop_loss_not_above_entry"]
    assert reasons(proposal, 107.0, 0.0, settings=settings) == ["take_profit_not_below_entry"]
    # A SELL that is not a tagged paper short is refused as before.
    assert reasons(proposal, 120.0, 0.0) == ["long_entries_only"]  # no settings: no switch
    off = make_settings(tmp_path / "off")
    assert reasons(proposal, 120.0, 0.0, settings=off) == ["long_entries_only"]
    untagged = SimpleNamespace(order=_short_order(paper_only=False))
    assert reasons(untagged, 120.0, 0.0, settings=settings) == ["long_entries_only"]
    crypto = SimpleNamespace(order=_short_order().model_copy(update={"symbol": "BTCUSD"}))
    # A crypto SELL is not a paper short at all (review fix 3: equities routed to Alpaca only).
    assert reasons(crypto, 120.0, 0.0, settings=settings) == ["long_entries_only"]
    assert bracket_kwargs(settings, proposal.order) == {"opens_short": True}
    assert bracket_kwargs(settings, untagged.order) == {} and bracket_kwargs(off, proposal.order) == {}


def test_the_bracket_rules_of_a_long_are_unchanged(tmp_path) -> None:
    long = SimpleNamespace(order=SimpleNamespace(side="buy", stop_loss=114.0, take_profit=132.0))
    for settings in (None, make_settings(tmp_path, **ON)):
        assert ExecutionCoordinator._alpaca_bracket_reasons(long, 120.0, 1.0, settings=settings) == []


def test_the_facts_are_read_again_before_the_risk_gate(tmp_path) -> None:
    settings = make_settings(tmp_path, **ON)
    order = _short_order(easy_to_borrow=True)
    alpaca = FakeShortAlpaca(asset={**ASSET, "easy_to_borrow": False})
    refresh_short_facts(settings, alpaca, order)
    assert order.metadata["easy_to_borrow"] is False and order.metadata["borrow_cost_annual_pct"] is None
    failing = _short_order()
    refresh_short_facts(settings, FakeShortAlpaca(fail_facts=True), failing)
    assert failing.metadata["shortable"] is False and "short_facts_error" in failing.metadata
    untouched = _short_order(paper_only=False)
    reads = FakeShortAlpaca()
    refresh_short_facts(settings, reads, untouched)
    assert reads.fact_reads == 0 and untouched.metadata["easy_to_borrow"] is True


def _queued_short(tmp_path, alpaca=None, **overrides):
    alpaca = alpaca or FakeShortAlpaca()
    values = {
        **ON,
        "alpaca_enabled": True,
        "broker_for_non_equities": "etoro",
        "max_trade_amount_usd": 10_000,
        "allowed_instruments": ["NVDA", "BTCUSD"],
        "market_direction_min_drop_pct": 0.0,
    }
    values.update(overrides)
    app = create_app(
        make_settings(tmp_path, **values),
        broker=MockBroker(),
        alpaca_client=alpaca,
        market_data_client=FakeEtoroMarketData(price=alpaca.price),
        enable_background_jobs=False,
    )
    request = TradeProposalCreate(
        symbol="NVDA", side="sell", amount_usd=6_000, leverage=1, proposed_price=120.0,
        stop_loss=126.0, take_profit=108.0, strategy_name="trend_following", rationale="short",
    )
    request.metadata = {**PAPER_SHORT, "timeframe": "1d"}
    proposal = app.state.proposal_service.create_proposal(request)
    approved = app.state.proposal_service.approve_proposal(
        proposal.id, ApprovalDecisionRequest(reviewer=AUTO_REVIEWER, notes="auto")
    )
    queued = app.state.execution_coordinator.enqueue_approved_proposal(approved.id)
    return app, alpaca, approved, queued


def test_a_paper_short_is_sent_as_a_sell_to_open_bracket(tmp_path) -> None:
    app, alpaca, proposal, queued = _queued_short(tmp_path)
    result = app.state.execution_coordinator.process_queue_item(queued.id)
    assert result.status == ExecutionQueueStatus.EXECUTED, result.validation_reason
    assert alpaca.fact_reads == 1  # re-read just before the risk gate
    (sent,) = alpaca.submitted_orders
    assert sent["side"] == "sell" and sent["order_class"] == "bracket" and sent["opens_short"] is True
    assert sent["stop_loss_price"] == 126.0 and sent["take_profit_price"] == 108.0
    assert sent["time_in_force"] == "gtc"
    # Size by risk: $6,000 at 120 = 50 shares; the stop 6 above risks $300.
    assert sent["qty"] == 50
    execution = execution_for(app, result)
    assert execution.request_payload["side"] == "sell"
    assert execution.request_payload["metadata"]["paper_only"] is True


def test_a_short_refused_at_execution_never_reaches_alpaca(tmp_path) -> None:
    # Hard-to-borrow at execution: the risk gate refuses on the fresh facts.
    alpaca = FakeShortAlpaca()
    app, alpaca, _, queued = _queued_short(tmp_path, alpaca)
    alpaca.asset["easy_to_borrow"] = False
    result = app.state.execution_coordinator.process_queue_item(queued.id)
    assert result.status == ExecutionQueueStatus.BLOCKED
    assert "short_not_easy_to_borrow" in result.validation_reason
    assert alpaca.submitted_orders == []
    # With the switch flipped off after approval, the short is refused too.
    app2, alpaca2, _, queued2 = _queued_short(tmp_path / "off")
    app2.state.settings.paper_short_entries_enabled = False
    result2 = app2.state.execution_coordinator.process_queue_item(queued2.id)
    assert result2.status == ExecutionQueueStatus.BLOCKED and alpaca2.submitted_orders == []
    assert "short_trading_disabled" in result2.validation_reason


def test_a_short_is_never_sent_without_its_bracket_nor_simulated(tmp_path) -> None:
    app, alpaca, _, queued = _queued_short(
        tmp_path, alpaca_require_bracket_orders=False, paper_simulated_fallback_enabled=True
    )
    result = app.state.execution_coordinator.process_queue_item(queued.id)
    assert result.status == ExecutionQueueStatus.BLOCKED
    assert result.validation_reason == "broker_submission_failed"
    assert alpaca.submitted_orders == []
    execution = execution_for(app, result)
    assert execution.status == ExecutionStatus.FAILED
    assert "short_requires_bracket_order" in str(execution.error_message)
    assert app.state.paper_trading_service.positions.list(limit=10) == []  # no simulated short
