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
        "gated_features", "scan_funnel",
    ):
        assert key in data
    # The auto-propose funnel rollup is present and well-shaped on an empty DB.
    funnel = data["scan_funnel"]
    assert funnel["scans"] == 0
    assert funnel["executed"] == 0
    assert funnel["dropped"] == {}
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


def test_dashboard_data_survives_non_finite_numbers(tmp_path: Path) -> None:
    """2026-11-26: a scan that ran while the market was closed stored an infinite number and
    every /dashboard/data request answered 500 ("Out of range float values are not JSON
    compliant: inf"). Such numbers are sent as null; everything else is unchanged."""

    from app.dashboard.routes import _json_safe

    inf, nan = float("inf"), float("nan")
    assert _json_safe({"a": [1.5, inf, -inf, nan, 0.0], "b": (inf, "inf"), "c": {"d": nan}}) == {
        "a": [1.5, None, None, None, 0.0],
        "b": [None, "inf"],
        "c": {"d": None},
    }
    assert _json_safe([3, True, None, "x", 2.25]) == [3, True, None, "x", 2.25]

    app = create_app(settings=_settings(tmp_path), enable_background_jobs=False)
    row = {
        "symbol": "NVDA",
        "final_score": nan,
        "payload": {"relative_volume": inf, "reward_to_risk": 2.4, "levels": [inf, 101.5]},
    }
    app.state.scan_decision_repository.list = lambda limit=30: [row]
    response = TestClient(app).get("/dashboard/data")
    assert response.status_code == 200
    (scan,) = response.json()["scans"]
    assert scan == {
        "symbol": "NVDA",
        "final_score": None,
        "payload": {"relative_volume": None, "reward_to_risk": 2.4, "levels": [None, 101.5]},
    }


def test_version_endpoint(tmp_path: Path) -> None:
    app = create_app(settings=_settings(tmp_path), enable_background_jobs=False)
    client = TestClient(app)

    response = client.get("/version")

    assert response.status_code == 200
    body = response.json()
    assert "commit" in body and "branch" in body
