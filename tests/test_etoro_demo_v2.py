from __future__ import annotations

import pytest

from app.broker.etoro_demo_v2 import EToroDemoV2Client
from app.models.trade import AssetClass, OrderSide, TradeOrder
from app.storage.db import Database
from app.storage.repositories import EToroDemoIdempotencyRepository
from tests.conftest import make_settings


def _client(tmp_path):
    settings = make_settings(
        tmp_path,
        etoro_demo_v2_enabled=True,
        etoro_api_key="api-key",
        etoro_user_key="user-key",
    )
    db = Database(settings)
    db.initialize()
    client = EToroDemoV2Client(
        settings,
        idempotency_repository=EToroDemoIdempotencyRepository(db),
    )
    return client


def test_create_order_is_durably_idempotent(tmp_path, monkeypatch):
    client = _client(tmp_path)
    calls = []

    def request(method, path, **kwargs):
        calls.append((method, path, kwargs))
        return {"orderId": 123, "referenceId": kwargs["request_id"]}

    monkeypatch.setattr(client, "_request", request)
    payload = {
        "action": "open",
        "transaction": "buy",
        "symbol": "AAPL",
        "orderType": "mkt",
        "amount": 1000,
    }

    first = client.create_order(payload, client_order_id="proposal-1")
    second = client.create_order(payload, client_order_id="proposal-1")

    assert first == second
    assert len(calls) == 1
    assert calls[0][1] == "/api/v2/trading/execution/demo/orders"


def test_client_order_id_cannot_be_reused_for_changed_payload(tmp_path, monkeypatch):
    client = _client(tmp_path)
    monkeypatch.setattr(client, "_request", lambda *_args, **_kwargs: {"orderId": 123})

    client.create_order({"action": "open", "symbol": "AAPL"}, client_order_id="proposal-1")

    with pytest.raises(RuntimeError, match="different eToro Demo request"):
        client.create_order({"action": "open", "symbol": "MSFT"}, client_order_id="proposal-1")


def test_open_order_runs_cost_check_and_requires_protection(tmp_path, monkeypatch):
    client = _client(tmp_path)
    calls = []

    def request(method, path, **kwargs):
        calls.append(path)
        return {"costs": []} if path.endswith("/costs") else {"orderId": 456}

    monkeypatch.setattr(client, "_request", request)
    order = TradeOrder(
        symbol="AAPL",
        side=OrderSide.BUY,
        amount_usd=1000,
        proposed_price=200,
        stop_loss=190,
        take_profit=220,
        asset_class=AssetClass.EQUITY,
    )

    result = client.open_market_order_by_amount(order, client_order_id="proposal-2")

    assert result.order_id == "456"
    assert calls == [
        "/api/v2/trading/info/demo/costs",
        "/api/v2/trading/execution/demo/orders",
    ]


def test_demo_mutations_require_explicit_enablement(tmp_path):
    settings = make_settings(tmp_path, etoro_demo_v2_enabled=False)
    client = EToroDemoV2Client(settings)
    order = TradeOrder(symbol="AAPL", amount_usd=100, proposed_price=100, stop_loss=95, take_profit=110)

    with pytest.raises(PermissionError, match="ETORO_DEMO_V2_ENABLED"):
        client.open_market_order_by_amount(order, client_order_id="proposal-3")
