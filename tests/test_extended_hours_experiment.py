from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.experiments.extended_hours import ExtendedHoursExperimentService
from app.live_signal_schema import MarketQuote
from app.main import create_app
from app.models.execution import AccountSummary, ExecutionRecord, PortfolioPosition, PortfolioSummary
from app.storage.db import Database
from app.storage.repositories import (
    ExtendedHoursExperimentRepository,
    RunLogRepository,
    RuntimeStateRepository,
    SafetyStateRepository,
)
from tests.conftest import MockBroker, make_settings


class FakeAlpaca:
    def __init__(self):
        self.submitted = []
        self.cancelled = []
        self.positions = []

    def get_account_identity(self):
        return {"account_number": "PA3B287XBZYU"}

    def get_quote(self, symbol, **_kwargs):
        return MarketQuote(
            symbol=symbol.upper(),
            bid=499.8,
            ask=500.0,
            last_execution=499.9,
            timestamp=datetime.now(UTC).isoformat(),
            source="alpaca",
            data_age_seconds=1.0,
        )

    def get_portfolio(self):
        return PortfolioSummary(
            mode="alpaca_paper",
            account=AccountSummary(cash_balance=100000, equity=100000),
            positions=self.positions,
        )

    def submit_order(self, **kwargs):
        self.submitted.append(kwargs)
        return ExecutionRecord(
            proposal_id=f"test:{kwargs['client_order_id']}",
            status="submitted",
            mode="alpaca_paper",
            broker_order_id=f"ord-{len(self.submitted)}",
            request_payload=kwargs,
            response_payload={
                "filled_qty": 0,
                "filled_avg_price": None,
            },
        )

    def cancel_order(self, broker_order_id):
        self.cancelled.append(broker_order_id)
        return True


class FakeAutomation:
    def status(self):
        return SimpleNamespace(kill_switch_enabled=False, circuit_breaker_reason="")


class FakeEToro:
    def __init__(self, capabilities=None, *, what_if_ok=False, verified=True):
        self.capabilities = capabilities or {}
        self.what_if_ok = what_if_ok
        self.verified = verified

    def get_account_identity(self):
        return {
            "account_id": "demo-1",
            "expected_account_id": "demo-1",
            "verified": self.verified,
        }

    def get_capabilities(self):
        return self.capabilities

    def list_supported_instruments(self):
        return [{"symbol": "SPY"}, {"symbol": "QQQ"}]

    def what_if(self, payload):
        if not self.what_if_ok:
            raise RuntimeError("orderType lmt rejected")
        return {"costs": []}


def _service(tmp_path, *, submit_enabled=False, etoro=None):
    settings = make_settings(
        tmp_path,
        alpaca_enabled=True,
        alpaca_expected_account_number="PA3B287XBZYU",
        execution_mode="paper",
        broker_for_equities="alpaca",
        paper_broker="alpaca",
        extended_hours_experiment_enabled=True,
        extended_hours_experiment_submit_enabled=submit_enabled,
        extended_hours_etoro_probe_enabled=True,
    )
    db = Database(settings)
    db.initialize()
    safety = SafetyStateRepository(db)
    safety.record_reconciliation(
        status="ok",
        account_number="PA3B287XBZYU",
        orders_seen=0,
        positions_seen=0,
        issues=[],
        account={"account_number": "PA3B287XBZYU"},
    )
    return ExtendedHoursExperimentService(
        settings=settings,
        alpaca_client=FakeAlpaca(),
        etoro_demo_client=etoro,
        repository=ExtendedHoursExperimentRepository(db),
        safety_state=safety,
        automation=FakeAutomation(),
        run_logs=RunLogRepository(db),
    )


def test_alpaca_probe_dry_run_uses_fractional_size_under_cap(tmp_path):
    service = _service(tmp_path)

    record = service.probe_alpaca(symbol="SPY", client_order_id="dry-1")

    assert record["status"] == "dry_run"
    assert record["symbol"] == "SPY"
    assert record["qty"] == 0.2
    assert record["notional_usd"] == 100.0
    assert service.alpaca.submitted == []


def test_alpaca_probe_submit_is_idempotent_by_client_order_id(tmp_path):
    service = _service(tmp_path, submit_enabled=True)

    first = service.probe_alpaca(symbol="QQQ", client_order_id="probe-1")
    second = service.probe_alpaca(symbol="QQQ", client_order_id="probe-1")

    assert first["id"] == second["id"]
    assert first["broker_order_id"] == "ord-1"
    assert len(service.alpaca.submitted) == 1
    submitted = service.alpaca.submitted[0]
    assert submitted["order_type"] == "limit"
    assert submitted["extended_hours"] is True
    assert submitted["time_in_force"] == "day"


def test_alpaca_probe_blocks_non_whitelisted_symbol(tmp_path):
    service = _service(tmp_path)

    try:
        service.probe_alpaca(symbol="AAPL", client_order_id="bad-1")
    except ValueError as exc:
        assert "symbol_not_whitelisted" in str(exc)
    else:
        raise AssertionError("Expected non-whitelisted symbol to be blocked")


def test_cancel_expired_orders_cancels_broker_order(tmp_path):
    service = _service(tmp_path, submit_enabled=True)
    record = service.probe_alpaca(symbol="SPY", client_order_id="exp-1")
    service.repository.update_order(
        record["id"],
        expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
    )

    cancelled = service.cancel_expired_orders()

    assert cancelled[0]["status"] == "canceled"
    assert service.alpaca.cancelled == ["ord-1"]


def test_submit_exit_uses_extended_hours_sell_limit(tmp_path):
    service = _service(tmp_path, submit_enabled=True)
    record = service.probe_alpaca(symbol="SPY", client_order_id="entry-1")
    service.alpaca.positions = [
        PortfolioPosition(symbol="SPY", quantity=0.2, average_price=500.0, market_value=100.0)
    ]

    updated = service.submit_exit(record["id"], client_order_id="exit-1")

    assert updated["status"] == "exit_submitted"
    submitted = service.alpaca.submitted[-1]
    assert submitted["side"] == "sell"
    assert submitted["order_type"] == "limit"
    assert submitted["extended_hours"] is True


def test_etoro_probe_classifies_current_demo_client_as_non_equivalent(tmp_path):
    service = _service(tmp_path, etoro=FakeEToro())

    result = service.run_etoro_capability_probe()

    assert result["classification"] == "non_equivalent_order_type"
    assert result["status"] == "blocked"
    assert result["evidence"]["submitted_order"] is False


def test_etoro_probe_can_classify_supported_future_capability(tmp_path):
    service = _service(
        tmp_path,
        etoro=FakeEToro(
            {
                "supports_extended_hours_limit_orders": True,
                "supports_24_5": True,
                "supports_extended_hours_exits": True,
            },
            what_if_ok=True,
        ),
    )

    result = service.run_etoro_capability_probe()

    assert result["classification"] == "supported"
    assert result["status"] == "ok"


def test_etoro_probe_classifies_account_mismatch(tmp_path):
    service = _service(tmp_path, etoro=FakeEToro(verified=False))

    result = service.run_etoro_capability_probe()

    assert result["classification"] == "account_mismatch"


def test_extended_hours_routes_are_wired_through_create_app(tmp_path):
    settings = make_settings(
        tmp_path,
        alpaca_enabled=True,
        alpaca_expected_account_number="PA3B287XBZYU",
        execution_mode="paper",
        broker_for_equities="alpaca",
        paper_broker="alpaca",
        extended_hours_experiment_enabled=True,
    )
    app = create_app(settings, broker=MockBroker(), alpaca_client=FakeAlpaca(), enable_background_jobs=False)
    app.state.safety_state_repository.record_reconciliation(
        status="ok",
        account_number="PA3B287XBZYU",
        orders_seen=0,
        positions_seen=0,
        issues=[],
        account={"account_number": "PA3B287XBZYU"},
    )
    client = TestClient(app)

    status_response = client.get("/experiments/extended-hours/status")
    probe_response = client.post(
        "/experiments/extended-hours/alpaca/probe",
        json={"symbol": "SPY", "client_order_id": "route-1"},
    )

    assert status_response.status_code == 200
    assert status_response.json()["primary_broker"] == "alpaca"
    assert probe_response.status_code == 201
    assert probe_response.json()["status"] == "dry_run"
