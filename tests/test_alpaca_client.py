from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pandas as pd
from alpaca.data.timeframe import TimeFrameUnit
from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
from alpaca.trading.requests import LimitOrderRequest, MarketOrderRequest

from app.broker.alpaca_client import AlpacaClient


class FakeTradingClient:
    def __init__(self):
        self.submitted_requests = []
        self.cancelled_ids = []
        self.orders_request = None
        self.submit_error: Exception | None = None
        self.existing_order = _order(client_order_id="cid-1")

    def get_account(self):
        return SimpleNamespace(
            equity="12500.50",
            cash="5000.25",
            buying_power="25000.75",
            day_trade_count=2,
            currency="USD",
        )

    def get_all_positions(self):
        return [
            SimpleNamespace(
                symbol="AAPL",
                qty="3",
                avg_entry_price="150.25",
                current_price="155.00",
                market_value="465.00",
                unrealized_pl="14.25",
            )
        ]

    def get_asset(self, symbol):
        return SimpleNamespace(
            symbol=symbol,
            asset_class="us_equity",
            status="active",
            tradable=True,
        )

    def submit_order(self, request):
        self.submitted_requests.append(request)
        if self.submit_error is not None:
            raise self.submit_error
        return _order(
            symbol=request.symbol,
            side=request.side.value,
            qty=request.qty,
            client_order_id=request.client_order_id,
        )

    def get_order_by_client_id(self, client_order_id):
        assert client_order_id == "cid-1"
        return self.existing_order

    def cancel_order_by_id(self, broker_order_id):
        self.cancelled_ids.append(broker_order_id)

    def cancel_orders(self):
        return [SimpleNamespace(id="1"), SimpleNamespace(id="2")]

    def close_all_positions(self, cancel_orders=True):
        assert cancel_orders is True
        return [SimpleNamespace(symbol="AAPL")]

    def get_orders(self, filter=None):
        self.orders_request = filter
        return [_order(client_order_id="cid-2")]

    def close_position(self, symbol):
        return _order(symbol=symbol, status="submitted")


class FakeDataClient:
    def __init__(self):
        self.quote_request = None
        self.trade_request = None
        self.bars_request = None

    def get_stock_latest_quote(self, request):
        self.quote_request = request
        return {
            "AAPL": SimpleNamespace(
                bid_price=199.8,
                ask_price=200.1,
                timestamp=datetime(2026, 5, 1, 14, 30, tzinfo=UTC),
            )
        }

    def get_stock_latest_trade(self, request):
        self.trade_request = request
        return {
            "AAPL": SimpleNamespace(
                price=200.0,
                timestamp=datetime(2026, 5, 1, 14, 31, tzinfo=UTC),
            )
        }

    def get_stock_bars(self, request):
        self.bars_request = request
        index = pd.MultiIndex.from_tuples(
            [
                ("AAPL", pd.Timestamp("2026-05-01T14:30:00Z")),
                ("AAPL", pd.Timestamp("2026-05-01T15:30:00Z")),
            ],
            names=["symbol", "timestamp"],
        )
        frame = pd.DataFrame(
            {
                "open": [100.0, 101.0],
                "high": [102.0, 103.0],
                "low": [99.5, 100.5],
                "close": [101.0, 102.0],
                "volume": [1000, 2000],
                "vwap": [100.75, 101.75],
            },
            index=index,
        )
        return SimpleNamespace(df=frame)


def _order(
    *,
    id: str = "ord-1",
    symbol: str = "AAPL",
    side: str = "buy",
    qty: float = 1.0,
    status: str = "new",
    client_order_id: str = "cid-1",
):
    return SimpleNamespace(
        id=id,
        symbol=symbol,
        side=side,
        qty=qty,
        status=status,
        submitted_at=datetime(2026, 5, 1, 14, 32, tzinfo=UTC),
        filled_qty=0,
        filled_avg_price=None,
        client_order_id=client_order_id,
    )


def _client(monkeypatch, trading=None, data=None) -> tuple[AlpacaClient, FakeTradingClient, FakeDataClient]:
    trading = trading or FakeTradingClient()
    data = data or FakeDataClient()
    monkeypatch.setattr("app.broker.alpaca_client.TradingClient", lambda **kwargs: trading)
    monkeypatch.setattr("app.broker.alpaca_client.StockHistoricalDataClient", lambda **kwargs: data)
    client = AlpacaClient(
        api_key="key",
        secret_key="secret",
        base_url="https://paper-api.alpaca.markets",
        data_url="https://data.alpaca.markets",
    )
    return client, trading, data


def test_get_portfolio_translates_alpaca_account_and_positions_to_project_portfolio(monkeypatch):
    client, _, _ = _client(monkeypatch)

    portfolio = client.get_portfolio()

    assert portfolio.mode == "alpaca_paper"
    assert portfolio.account.equity == 12500.50
    assert portfolio.account.cash_balance == 5000.25
    assert portfolio.account.buying_power == 25000.75
    assert portfolio.account.day_trade_count == 2
    assert portfolio.positions[0].symbol == "AAPL"
    assert portfolio.positions[0].quantity == 3
    assert portfolio.positions[0].average_price == 150.25
    assert portfolio.positions[0].market_value == 465.0
    assert portfolio.positions[0].unrealized_pnl == 14.25


def test_supported_equity_requires_active_tradable_us_equity(monkeypatch):
    client, _, _ = _client(monkeypatch)

    assert client.is_supported_equity("aapl") is True


def test_get_quote_returns_market_quote_with_source_alpaca(monkeypatch):
    client, _, _ = _client(monkeypatch)

    quote = client.get_quote("aapl")

    assert quote.symbol == "AAPL"
    assert quote.source == "alpaca"
    assert quote.bid == 199.8
    assert quote.ask == 200.1
    assert quote.last_execution == 200.0
    assert quote.timestamp == "2026-05-01T14:31:00+00:00"


def test_get_bars_translates_timeframe_strings_to_alpaca_timeframes(monkeypatch):
    client, _, data = _client(monkeypatch)

    bars = client.get_bars(
        "AAPL",
        timeframe="5m",
        start=datetime(2026, 5, 1, tzinfo=UTC),
        end=datetime(2026, 5, 2, tzinfo=UTC),
    )

    assert data.bars_request.timeframe.amount == 5
    assert data.bars_request.timeframe.unit == TimeFrameUnit.Minute
    assert list(bars.columns) == ["timestamp", "open", "high", "low", "close", "volume", "vwap"]
    assert len(bars) == 2
    assert bars.iloc[-1]["close"] == 102.0


def test_submit_order_forwards_client_order_id_to_alpaca(monkeypatch):
    client, trading, _ = _client(monkeypatch)

    client.submit_order(symbol="AAPL", side="buy", qty=1, order_type="market", client_order_id="cid-1")

    assert trading.submitted_requests[0].client_order_id == "cid-1"


def test_submit_order_duplicate_client_order_id_returns_existing_without_resubmission(monkeypatch):
    trading = FakeTradingClient()
    trading.submit_error = RuntimeError("client_order_id already exists")
    trading.existing_order = _order(id="existing-1", client_order_id="cid-1")
    client, trading, _ = _client(monkeypatch, trading=trading)

    record = client.submit_order(symbol="AAPL", side="buy", qty=1, order_type="market", client_order_id="cid-1")

    assert len(trading.submitted_requests) == 1
    assert record.broker_order_id == "existing-1"
    assert record.response_payload["client_order_id"] == "cid-1"


def test_submit_market_order_uses_MarketOrderRequest_with_correct_side(monkeypatch):
    client, trading, _ = _client(monkeypatch)

    client.submit_order(symbol="AAPL", side="buy", qty=2, order_type="market")

    request = trading.submitted_requests[0]
    assert isinstance(request, MarketOrderRequest)
    assert request.side == OrderSide.BUY
    assert request.qty == 2.0
    assert request.time_in_force == TimeInForce.DAY


def test_submit_bracket_order_attaches_broker_native_protection(monkeypatch):
    client, trading, _ = _client(monkeypatch)

    client.submit_bracket_order(
        symbol="AAPL",
        side="buy",
        qty=3,
        stop_loss_price=190.0,
        take_profit_price=220.0,
        client_order_id="cid-1",
    )

    request = trading.submitted_requests[0]
    assert isinstance(request, MarketOrderRequest)
    assert request.order_class == OrderClass.BRACKET
    assert request.qty == 3
    assert request.stop_loss.stop_price == 190.0
    assert request.take_profit.limit_price == 220.0


def test_submit_limit_order_uses_LimitOrderRequest_with_limit_price(monkeypatch):
    client, trading, _ = _client(monkeypatch)

    client.submit_order(symbol="AAPL", side="sell", qty=2, order_type="limit", limit_price=201.25, time_in_force="gtc")

    request = trading.submitted_requests[0]
    assert isinstance(request, LimitOrderRequest)
    assert request.side == OrderSide.SELL
    assert request.limit_price == 201.25
    assert request.time_in_force == TimeInForce.GTC


def test_cancel_order_returns_true_on_success_false_on_already_done(monkeypatch):
    client, trading, _ = _client(monkeypatch)

    assert client.cancel_order("ord-1") is True
    assert trading.cancelled_ids == ["ord-1"]

    def already_done(_broker_order_id):
        raise RuntimeError("order already canceled")

    trading.cancel_order_by_id = already_done
    assert client.cancel_order("ord-1") is False


def test_cancel_all_orders_returns_count_for_kill_switch(monkeypatch):
    client, _, _ = _client(monkeypatch)

    assert client.cancel_all_orders() == 2


def test_close_all_positions_returns_count_for_kill_switch(monkeypatch):
    client, _, _ = _client(monkeypatch)

    assert client.close_all_positions() == 1


def test_get_executions_filters_by_since_timestamp(monkeypatch):
    client, trading, _ = _client(monkeypatch)
    since = datetime(2026, 5, 1, tzinfo=UTC)

    records = client.get_executions(since=since)

    assert trading.orders_request.after == since
    assert records[0].broker_order_id == "ord-1"
    assert records[0].response_payload["symbol"] == "AAPL"
