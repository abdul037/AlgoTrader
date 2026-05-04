from __future__ import annotations

from fastapi.testclient import TestClient

from app.live_signal_schema import MarketQuote
from app.main import create_app
from tests.conftest import MockBroker, make_settings


class FakeEtoroMarketData:
    def get_rates(self, symbols: list[str]):
        return {
            symbol.upper(): MarketQuote(
                symbol=symbol.upper(),
                bid=120.0,
                ask=120.1,
                last_execution=120.0,
                source="etoro",
                quote_derived_from_history=False,
                data_age_seconds=0.0,
            )
            for symbol in symbols
        }


def proposal_payload() -> dict:
    return {
        "symbol": "NVDA",
        "amount_usd": 1000,
        "leverage": 1,
        "proposed_price": 120.0,
        "stop_loss": 114.0,
        "take_profit": 132.0,
        "strategy_name": "ma_crossover",
        "rationale": "Crossed above medium-term average after a pullback.",
        "notes": "integration test",
    }


def test_execution_is_blocked_when_not_approved(tmp_path) -> None:
    app = create_app(make_settings(tmp_path), broker=MockBroker())
    client = TestClient(app)

    create_response = client.post("/proposals/create", json=proposal_payload())
    proposal_id = create_response.json()["id"]

    execute_response = client.post(f"/proposals/{proposal_id}/execute")
    assert execute_response.status_code == 409
    assert "approved" in execute_response.json()["detail"]


def test_proposal_approval_and_execution_flow(tmp_path) -> None:
    broker = MockBroker()
    app = create_app(make_settings(tmp_path), broker=broker)
    client = TestClient(app)

    create_response = client.post("/proposals/create", json=proposal_payload())
    assert create_response.status_code == 201
    proposal_id = create_response.json()["id"]
    assert create_response.json()["status"] == "pending"

    approve_response = client.post(
        f"/proposals/{proposal_id}/approve",
        json={"reviewer": "qa-user", "notes": "looks acceptable"},
    )
    assert approve_response.status_code == 200
    assert approve_response.json()["status"] == "approved"

    execute_response = client.post(f"/proposals/{proposal_id}/execute")
    assert execute_response.status_code == 200
    assert execute_response.json()["status"] == "submitted"
    assert len(broker.orders) == 1

    proposals_response = client.get("/proposals")
    proposals = proposals_response.json()
    assert proposals[0]["status"] == "executed"


def test_automation_routes_smoke(tmp_path) -> None:
    app = create_app(make_settings(tmp_path), broker=MockBroker())
    client = TestClient(app)

    status_response = client.get("/automation/status")
    assert status_response.status_code == 200
    assert status_response.json()["paused"] is False

    pause_response = client.post("/automation/pause", json={"reason": "test pause"})
    assert pause_response.status_code == 200
    assert pause_response.json()["paused"] is True

    resume_response = client.post("/automation/resume", json={"reason": "test resume"})
    assert resume_response.status_code == 200
    assert resume_response.json()["paused"] is False

    kill_response = client.post("/automation/kill-switch", json={"reason": "test kill"})
    assert kill_response.status_code == 200
    assert kill_response.json()["kill_switch_enabled"] is True


def test_live_queue_execution_blocks_without_live_gates(tmp_path) -> None:
    app = create_app(
        make_settings(
            tmp_path,
            execution_mode="live",
            enable_real_trading=False,
            paper_trading_enabled=True,
            primary_market_data_provider="auto",
            fallback_market_data_provider="none",
        ),
        broker=MockBroker(),
        market_data_client=FakeEtoroMarketData(),
    )
    client = TestClient(app)

    create_response = client.post("/proposals/create", json=proposal_payload())
    assert create_response.status_code == 201
    proposal_id = create_response.json()["id"]
    approve_response = client.post(
        f"/proposals/{proposal_id}/approve",
        json={"reviewer": "qa-user", "notes": "approved"},
    )
    assert approve_response.status_code == 200
    enqueue_response = client.post(f"/execution/queue/{proposal_id}/enqueue")
    assert enqueue_response.status_code == 201
    queue_id = enqueue_response.json()["id"]

    process_response = client.post(f"/execution/queue/{queue_id}/process")

    assert process_response.status_code == 200
    payload = process_response.json()
    assert payload["status"] == "blocked"
    assert "enable_real_trading_false" in payload["validation_reason"]
    assert "paper_trading_enabled_in_live_mode" in payload["validation_reason"]
