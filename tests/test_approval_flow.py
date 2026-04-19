from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import MockBroker, make_settings


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
