from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import MockBroker, make_settings


def test_paper_dashboard_exposes_performance_and_controls(tmp_path) -> None:
    app = create_app(make_settings(tmp_path), broker=MockBroker())
    client = TestClient(app)

    response = client.get("/paper/dashboard")

    assert response.status_code == 200
    payload = response.json()
    assert payload["paper"]["mode"] == "paper"
    assert payload["paper"]["total_trades"] == 0
    assert payload["risk_controls"]["execution_mode"] == "paper"
    assert payload["risk_controls"]["enable_real_trading"] is False
    assert "calibration_suggestions" in payload
