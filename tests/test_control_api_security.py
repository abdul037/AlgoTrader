from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import MockBroker, make_settings


def test_control_token_protects_mutation_routes(tmp_path) -> None:
    app = create_app(
        make_settings(tmp_path, control_api_token="control-secret"),
        broker=MockBroker(),
        enable_background_jobs=False,
    )
    client = TestClient(app)

    assert client.get("/automation/status").status_code == 200
    assert client.post("/automation/pause").status_code == 403
    assert client.post("/automation/pause", headers={"X-Control-Token": "wrong"}).status_code == 403
    assert (
        client.post("/automation/pause", headers={"X-Control-Token": "control-secret"}).status_code
        == 200
    )


def test_telegram_webhook_uses_its_separate_authentication(tmp_path) -> None:
    app = create_app(
        make_settings(
            tmp_path,
            control_api_token="control-secret",
            telegram_webhook_secret="telegram-secret",
        ),
        broker=MockBroker(),
        enable_background_jobs=False,
    )
    client = TestClient(app)

    response = client.post(
        "/telegram/webhook",
        json={},
        headers={"X-Telegram-Bot-Api-Secret-Token": "telegram-secret"},
    )

    assert response.status_code == 200
