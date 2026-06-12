from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import MockBroker, make_settings


def test_metrics_exposes_reconciliation_trading_and_safety_gauges(tmp_path):
    app = create_app(
        make_settings(tmp_path),
        broker=MockBroker(),
        enable_background_jobs=False,
    )

    response = TestClient(app).get("/metrics")

    assert response.status_code == 200
    assert "algobot_positions_open 0" in response.text
    assert "algobot_reconciliation_healthy 0" in response.text
    assert "algobot_kill_switch_enabled 0" in response.text
