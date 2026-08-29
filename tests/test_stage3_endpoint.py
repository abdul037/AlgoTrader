"""The /performance/stage3 endpoint returns the full Stage 3 picture and never
reports real trading as enabled by default."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import make_settings


def _client(tmp_path: Path) -> TestClient:
    settings = make_settings(
        tmp_path,
        screener_scheduler_enabled=False,
        telegram_hourly_alerts_enabled=False,
        ledger_cycle_enabled=False,
        etoro_demo_v2_enabled=False,
        learning_worker_enabled=False,
        paper_position_refresh_enabled=False,
        control_api_token="",  # open for the test
    )
    return TestClient(create_app(settings=settings, enable_background_jobs=False))


def test_stage3_endpoint_shape(tmp_path: Path) -> None:
    data = _client(tmp_path).get("/performance/stage3").json()
    for key in (
        "capital_usd", "daily_target_usd", "ready_for_capital_decision", "gates",
        "blockers", "track_record", "capital_plan", "deployment_ladder", "preflight",
    ):
        assert key in data
    # Empty DB -> not ready, and real trading is never reported as enabled.
    assert data["ready_for_capital_decision"] is False
    assert data["preflight"]["real_trading_currently_enabled"] is False
    assert "sharpe" in data["track_record"]
