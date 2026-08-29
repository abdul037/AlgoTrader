from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import make_settings


def _settings(tmp_path: Path, **overrides):
    base = dict(
        screener_scheduler_enabled=False,
        telegram_hourly_alerts_enabled=False,
        ledger_cycle_enabled=False,
        etoro_demo_v2_enabled=False,
        learning_worker_enabled=False,
        paper_position_refresh_enabled=False,
        alpaca_expected_account_number="PA3B287XBZYU",
    )
    base.update(overrides)
    return make_settings(tmp_path, **base)


def test_dashboard_page_served(tmp_path: Path) -> None:
    app = create_app(settings=_settings(tmp_path), enable_background_jobs=False)
    client = TestClient(app)

    response = client.get("/dashboard")

    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "AlgoTrader" in response.text
    assert "/dashboard/data" in response.text  # the page polls the data endpoint


def test_dashboard_data_snapshot_shape(tmp_path: Path) -> None:
    app = create_app(settings=_settings(tmp_path), enable_background_jobs=False)
    client = TestClient(app)

    data = client.get("/dashboard/data").json()

    # All sections present and JSON-serialisable even on a fresh, empty database.
    for key in (
        "config", "automation", "flags", "trades", "proposals", "scans",
        "positions", "strategy_performance", "pnl_series", "stage3",
        "gated_features",
    ):
        assert key in data
    # Gated-feature verdicts are present and well-shaped even on an empty DB.
    gated = data["gated_features"]
    assert gated is not None
    assert {f["feature"] for f in gated["features"]} == {
        "regime_router", "drawdown_governor", "cross_sectional_momentum",
    }
    assert gated["overall"] in {"GO", "REVIEW", "NEED-DATA", "NO-GO"}
    assert isinstance(data["pnl_series"], list)
    assert data["config"]["alpaca_account"] == "PA3B287XBZYU"
    assert isinstance(data["trades"], list)
    assert isinstance(data["scans"], list)
    assert isinstance(data["strategy_performance"], list)
    assert "commit" in data["build"]


def test_version_endpoint(tmp_path: Path) -> None:
    app = create_app(settings=_settings(tmp_path), enable_background_jobs=False)
    client = TestClient(app)

    response = client.get("/version")

    assert response.status_code == 200
    body = response.json()
    assert "commit" in body and "branch" in body
