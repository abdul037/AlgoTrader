from __future__ import annotations

from fastapi.testclient import TestClient

from app.runtime_settings import AppSettings
from app.main import create_app


class FakeNotifier:
    def __init__(self):
        self.webhook_calls: list[tuple[str, str | None, bool]] = []

    def set_webhook(self, webhook_url: str, *, secret_token: str | None = None, drop_pending_updates: bool = False):
        self.webhook_calls.append((webhook_url, secret_token, drop_pending_updates))
        return {"ok": True, "description": "Webhook was set."}

    def get_webhook_info(self):
        return {
            "ok": True,
            "result": {
                "url": "https://example.com/telegram/webhook",
                "pending_update_count": 2,
                "has_custom_certificate": False,
            },
        }

    def send_text(self, message: str, *, chat_id: str | None = None) -> bool:
        return True


class FakeTelegramService:
    def __init__(self):
        self.updates: list[dict] = []

    def handle_update(self, update: dict) -> bool:
        self.updates.append(update)
        return True

    def send_due_alerts(self) -> int:
        return 0


def _build_app():
    settings = AppSettings(
        telegram_enabled=True,
        telegram_bot_token="token",
        telegram_chat_id="7329410595",
        telegram_allowed_chat_ids=["7329410595"],
        telegram_webhook_secret="secret-token",
        telegram_webhook_url="https://example.com/telegram/webhook",
    )
    notifier = FakeNotifier()
    service = FakeTelegramService()
    app = create_app(
        settings=settings,
        telegram_notifier=notifier,
        enable_background_jobs=False,
    )
    app.state.telegram_command_service = service
    return app, notifier, service


def test_telegram_webhook_route_processes_update() -> None:
    app, _, service = _build_app()
    client = TestClient(app)

    response = client.post(
        "/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "secret-token"},
        json={"update_id": 1, "message": {"chat": {"id": 7329410595}, "text": "/start"}},
    )

    assert response.status_code == 200
    assert response.json()["processed"] is True
    assert service.updates[0]["message"]["text"] == "/start"


def test_telegram_webhook_route_rejects_bad_secret() -> None:
    app, _, _ = _build_app()
    client = TestClient(app)

    response = client.post(
        "/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"},
        json={"update_id": 1, "message": {"chat": {"id": 7329410595}, "text": "/start"}},
    )

    assert response.status_code == 403


def test_register_telegram_webhook_uses_configured_url() -> None:
    app, notifier, _ = _build_app()
    client = TestClient(app)

    response = client.post("/telegram/webhook/register", json={})

    assert response.status_code == 200
    assert response.json()["webhook_url"] == "https://example.com/telegram/webhook"
    assert notifier.webhook_calls == [
        ("https://example.com/telegram/webhook", "secret-token", False)
    ]


def test_telegram_webhook_status_returns_provider_state() -> None:
    app, _, _ = _build_app()
    client = TestClient(app)

    response = client.get("/telegram/webhook/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["webhook_url"] == "https://example.com/telegram/webhook"
    assert payload["pending_update_count"] == 2
