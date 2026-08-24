from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.automation.scheduler_worker import HEARTBEAT_KEY
from app.main import create_app
from app.utils.time import utc_now
from tests.conftest import make_settings


def _no_worker_settings(tmp_path: Path, **overrides):
    """Isolated settings where the startup feature-gate launches no worker.

    All cadence feature flags are off so create_app's startup returns before
    starting the scheduler worker, letting us drive the heartbeat by hand.
    Each call gets its own sqlite database (via make_settings) so the runtime
    state written by one test never leaks into another.
    """

    base = dict(
        screener_scheduler_enabled=False,
        telegram_hourly_alerts_enabled=False,
        ledger_cycle_enabled=False,
        etoro_demo_v2_enabled=False,
        learning_worker_enabled=False,
        paper_position_refresh_enabled=False,
    )
    base.update(overrides)
    return make_settings(tmp_path, **base)


def test_readiness_ok_when_worker_not_managed_here(tmp_path: Path) -> None:
    # Background jobs disabled entirely -> the web node manages no worker but is
    # still a healthy API server.
    app = create_app(settings=_no_worker_settings(tmp_path), enable_background_jobs=False)
    client = TestClient(app)

    response = client.get("/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["database"] == "ok"
    assert body["checks"]["scheduler"] == "not_managed_here"


def test_readiness_not_ready_when_heartbeat_missing(tmp_path: Path) -> None:
    # Background jobs enabled and not polling -> a worker is expected, but the
    # feature-gate prevents startup from launching one, so no heartbeat exists.
    app = create_app(settings=_no_worker_settings(tmp_path), enable_background_jobs=True)
    with TestClient(app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["scheduler"] == "no_heartbeat"


def test_readiness_ok_with_fresh_heartbeat(tmp_path: Path) -> None:
    app = create_app(settings=_no_worker_settings(tmp_path), enable_background_jobs=True)
    with TestClient(app) as client:
        app.state.workflow_service.runtime_state.set(HEARTBEAT_KEY, utc_now().isoformat())
        response = client.get("/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["checks"]["scheduler"].startswith("ok")


def test_readiness_includes_trade_stream_status_when_present(tmp_path: Path) -> None:
    from types import SimpleNamespace

    app = create_app(settings=_no_worker_settings(tmp_path), enable_background_jobs=False)
    app.state.alpaca_trade_stream = SimpleNamespace(
        status=lambda: {"running": True, "connected": True, "ingested": 3}
    )
    client = TestClient(app)

    body = client.get("/health/ready").json()

    assert body["checks"]["trade_stream"] == {"running": True, "connected": True, "ingested": 3}


def test_readiness_not_ready_with_stale_heartbeat(tmp_path: Path) -> None:
    app = create_app(
        settings=_no_worker_settings(tmp_path, scheduler_heartbeat_max_age_seconds=60),
        enable_background_jobs=True,
    )
    with TestClient(app) as client:
        stale = utc_now().replace(year=2000).isoformat()
        app.state.workflow_service.runtime_state.set(HEARTBEAT_KEY, stale)
        response = client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["checks"]["scheduler"].startswith("stale")
