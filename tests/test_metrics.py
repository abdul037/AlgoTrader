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
    assert "algobot_strategy_families_total 18" in response.text
    assert "algobot_strategy_specs_total 50" in response.text
    assert "algobot_strategy_families_enhanced_total 6" in response.text
    assert "algobot_strategy_specs_enhanced_total 14" in response.text


def test_process_health_does_not_depend_on_database_heavy_workflow_status(tmp_path, monkeypatch):
    app = create_app(
        make_settings(tmp_path),
        broker=MockBroker(),
        enable_background_jobs=False,
    )
    monkeypatch.setattr(
        app.state.workflow_service,
        "health_summary",
        lambda: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )

    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json()["reason"] == "process_ready"
