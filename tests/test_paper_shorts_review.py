"""Shorts step 3, review fixes of fa9b7d3 (docs/notes/paper_shorts.md, section 9).

Each fix only tightens or corrects what a short meets; every long path is shown unchanged.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.models.execution import AccountSummary, PortfolioPosition, PortfolioSummary
from app.models.trade import TradeOrder
from app.risk.context import build_risk_context
from app.risk.guardrails import RiskManager
from tests.conftest import make_settings

CAP = "Open-position cap of 2 reached for the information technology sector"


class _Book:
    """Alpaca paper's portfolio: (symbol, signed quantity) positions."""

    def __init__(self, positions, *, equity=100_000.0, cash=100_000.0):
        self.positions, self.equity, self.cash = positions, equity, cash

    def get_portfolio(self):
        return PortfolioSummary(
            mode="alpaca_paper",
            account=AccountSummary(cash_balance=self.cash, equity=self.equity),
            positions=[
                PortfolioPosition(symbol=s, quantity=q, is_buy=q >= 0, market_value=q * 100.0)
                for s, q in self.positions
            ],
        )


class _Executions:
    def count_since(self, _since):
        return 0

    def daily_loss_stats(self):
        return 0.0, 0

    def period_realized_pnl(self, *, days):
        return 0.0

    def list(self, limit=200):
        return []


def _settings(tmp_path, **values):
    values.setdefault("allowed_instruments", ["NVDA", "AAPL", "MSFT", "CRM", "WMT"])
    return make_settings(tmp_path, paper_broker="alpaca", broker_for_equities="alpaca", max_open_positions=20, **values)


def _order(symbol, side="buy"):
    stop, target = (102.0, 96.0) if side == "sell" else (98.0, 104.0)
    return TradeOrder(symbol=symbol, side=side, amount_usd=1_000, proposed_price=100.0, stop_loss=stop, take_profit=target)


def _capped(manager, order, book):
    context = build_risk_context(manager.settings, book, _Executions())
    return [r for r in manager.validate_order(order, context).reasons if r.startswith("Open-position cap")]


# --- 1. the sector cap counts shorts ---------------------------------------------------------


def test_open_shorts_count_toward_a_sector_s_open_positions(tmp_path) -> None:
    manager = RiskManager(_settings(tmp_path))
    two_shorts = _Book([("AAPL", -10.0), ("CRM", -5.0)])
    assert _capped(manager, _order("NVDA"), two_shorts) == [CAP]  # a new long
    assert _capped(manager, _order("NVDA", "sell"), two_shorts) == [CAP]  # a new short
    mixed = _Book([("AAPL", 10.0), ("CRM", -5.0)])
    assert _capped(manager, _order("NVDA"), mixed) == [CAP]
    assert _capped(manager, _order("NVDA", "sell"), _Book([("AAPL", -10.0), ("WMT", 3.0)])) == []


def test_a_long_meets_the_sector_cap_exactly_as_before_with_no_short_open(tmp_path) -> None:
    manager = RiskManager(_settings(tmp_path))
    assert _capped(manager, _order("NVDA"), _Book([("AAPL", 10.0), ("CRM", 5.0)])) == [CAP]
    assert _capped(manager, _order("NVDA"), _Book([("AAPL", 10.0), ("WMT", 5.0)])) == []
    assert _capped(manager, _order("AAPL"), _Book([("AAPL", 10.0), ("CRM", 5.0)])) == []  # own symbol
    off = RiskManager(_settings(tmp_path / "off", max_open_positions_per_sector=0))
    assert _capped(off, _order("NVDA", "sell"), _Book([("AAPL", -1.0), ("CRM", -1.0)])) == []


def test_while_etoro_decides_the_room_the_cap_is_not_paper_s(tmp_path) -> None:
    manager = RiskManager(_settings(tmp_path))
    context = build_risk_context(manager.settings, _Book([("AAPL", -10.0), ("CRM", -5.0)]), _Executions())
    reasons = manager.validate_order(_order("NVDA"), context, etoro_room=[]).reasons
    assert not [r for r in reasons if r.startswith("Open-position cap")]


# --- 3. a paper short is an equity routed to Alpaca --------------------------------------------


def test_a_paper_short_must_be_an_equity_the_router_sends_to_alpaca(tmp_path) -> None:
    from app.models.trade import AssetClass
    from app.risk.short_side import paper_short_order, routes_to_alpaca_equity
    from app.risk.shorting import ShortTradePolicy
    from tests.test_paper_shorts_etoro import ON, _short

    on = make_settings(tmp_path, **ON)
    assert paper_short_order(on, _short())
    for asset_class in (AssetClass.UNKNOWN, AssetClass.GOLD, AssetClass.CRYPTO):  # unknown: eToro
        order = _short().model_copy(update={"asset_class": asset_class})
        assert not paper_short_order(on, order) and not routes_to_alpaca_equity(on, order)
        assert ShortTradePolicy(on).blockers(order, account_equity_usd=1e5) == ["short_trading_disabled"]
    assert not paper_short_order(on, _short(symbol="BTCUSD"))
    etoro = SimpleNamespace(**{**ON, "broker_for_equities": "etoro"})
    assert not routes_to_alpaca_equity(etoro, _short()) and not paper_short_order(etoro, _short())


# --- 5. the risk base of a net-short book ------------------------------------------------------


def test_a_net_short_book_is_sized_on_its_equity_not_its_short_proceeds(tmp_path) -> None:
    from app.risk.context import account_risk_base

    settings = _settings(tmp_path)
    # $100k equity, short $50k of AAPL: the sale's $50k proceeds sit in cash ($150k).
    short_book = _Book([("AAPL", -500.0)], equity=100_000.0, cash=150_000.0)
    assert build_risk_context(settings, short_book, _Executions()).account_balance == 100_000.0
    assert account_risk_base(-5.0, 20.0) == 20.0 and account_risk_base(0.0, 0.0) == 1.0  # as before


def test_a_long_only_book_s_risk_base_is_exactly_the_old_one(tmp_path) -> None:
    from app.risk.context import account_risk_base

    old = lambda equity, cash: max(equity, cash, 1.0)  # noqa: E731 - the pre-fix rule
    for equity, cash in [(100_000.0, 100_000.0), (100_000.0, 40_000.0), (100_000.0, -20_000.0),
                         (0.5, 0.2), (1.0, 0.0), (0.0, 0.0), (-10.0, -20.0), (250_000.0, 250_000.0)]:
        assert equity >= cash  # a long-only book: its positions are worth >= 0
        assert account_risk_base(equity, cash) == old(equity, cash), (equity, cash)
    long_book = _Book([("AAPL", 400.0), ("WMT", 100.0)], equity=100_000.0, cash=50_000.0)
    assert build_risk_context(_settings(tmp_path), long_book, _Executions()).account_balance == 100_000.0


# --- 6. an unprotected position held by its live take-profit is flattened ---------------------


def _held_by_target(base, *, settles=True, legs=("new", "canceled"), **kw):
    """``base`` (a long or a short Alpaca double) whose stop leg is gone and whose take-profit
    leg is live: Alpaca refuses the close while that leg holds the shares."""

    from tests.test_reconciliation_unprotected_positions import QTY_HELD_ERROR

    class Broker(base):
        def __init__(self):
            super().__init__(leg_statuses=legs, **kw)
            self.cancelled: list[str] = []

        def cancel_order(self, order_id):
            self.cancelled.append(order_id)
            if settles and order_id == "leg-tp":
                self.leg_statuses = ("canceled", self.leg_statuses[1])
            return True

        def close_position(self, symbol):
            if self.leg_statuses[0] == "new":
                raise RuntimeError(QTY_HELD_ERROR)
            return super().close_position(symbol)

    return Broker()


def _bases():
    from tests.test_paper_shorts_lifecycle import ShortBroker
    from tests.test_reconciliation_unprotected_positions import BrokerWithDeadBracket

    return {"long": (BrokerWithDeadBracket, "GOOGL"), "short": (ShortBroker, "TSLA")}


def test_a_live_take_profit_no_longer_keeps_an_unprotected_position_open(tmp_path) -> None:
    from tests.test_reconciliation_unprotected_positions import _events, _service

    for side, (base, symbol) in _bases().items():
        broker = _held_by_target(base)
        service, automation, logs = _service(tmp_path / side, broker)
        result = service.reconcile()
        assert broker.cancelled == ["leg-tp"], side  # the take-profit only, never the stop
        assert broker.closed == [symbol] and result["issues"] == [], side
        events = _events(logs)
        assert "unprotected_position_legs_cancelled" in events and "unprotected_position_flattened" in events
        assert automation.status().kill_switch_enabled is False


def test_a_cancel_that_has_not_settled_is_closed_on_the_next_sweep(tmp_path) -> None:
    from tests.test_reconciliation_unprotected_positions import _events, _service

    for side, (base, symbol) in _bases().items():
        broker = _held_by_target(base, settles=False)
        service, _, logs = _service(tmp_path / side, broker)
        service.reconcile()
        assert broker.cancelled == ["leg-tp"] and broker.closed == [], side
        assert "unprotected_position_flatten_deferred" in _events(logs)
        broker.leg_statuses = ("canceled", "canceled")  # Alpaca has cancelled it since
        service.reconcile()
        assert broker.cancelled == ["leg-tp"] and broker.closed == [symbol], side  # no new cancel


def test_a_live_stop_is_never_cancelled_and_a_close_in_flight_is_left_alone(tmp_path) -> None:
    from tests.test_reconciliation_unprotected_positions import BrokerWithDeadBracket, _service

    for side, (base, _symbol) in _bases().items():
        protected = _held_by_target(base, legs=("new", "held"))  # both legs live: protected
        service, _, _ = _service(tmp_path / f"{side}-p", protected)
        assert service.reconcile()["issues"] == []
        assert protected.cancelled == [] and protected.closed == []
    in_flight = {"broker_order_id": "close-pending", "symbol": "GOOGL", "side": "sell", "type": "market",
                 "qty": 1.0, "status": "pending_new"}
    closing = _held_by_target(BrokerWithDeadBracket, extra_orders=[in_flight])
    service, _, _ = _service(tmp_path / "flight", closing)
    assert service.reconcile()["issues"] == []
    assert closing.cancelled == [] and closing.closed == []  # the genuine in-flight case: deferred
