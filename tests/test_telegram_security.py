from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app
from app.runtime_settings import AppSettings


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str | None]] = []

    def send_text(self, message: str, *, chat_id: str | None = None) -> bool:
        self.sent.append((message, chat_id))
        return True

    def get_webhook_info(self):
        return {"ok": True, "result": {"url": "https://example.com/telegram/webhook"}}

    def set_webhook(self, webhook_url: str, *, secret_token: str | None = None, drop_pending_updates: bool = False):
        return {"ok": True, "description": "Webhook was set."}


class FakeLogs:
    def __init__(self) -> None:
        self.items: list[tuple[str, dict]] = []

    def log(self, event_type: str, payload: dict) -> None:
        self.items.append((event_type, payload))


class FakeTelegramService:
    def __init__(self) -> None:
        self.updates: list[dict] = []
        self.logs = FakeLogs()

    def handle_update(self, update: dict) -> bool:
        self.updates.append(update)
        return True

    def send_due_alerts(self) -> int:
        return 0


def _telegram_update(chat_id: int, text: str = "/start") -> dict:
    return {
        "update_id": chat_id,
        "message": {
            "from": {"id": chat_id},
            "chat": {"id": chat_id},
            "text": text,
        },
    }


def _build_client(
    *,
    secret: str | None = "secret-token",
    allowed_chat_ids: list[int] | None = None,
    rate_limit: int = 30,
) -> tuple[TestClient, FakeNotifier, FakeTelegramService]:
    settings = AppSettings(
        telegram_enabled=True,
        telegram_bot_token="token",
        telegram_chat_id="1001",
        telegram_webhook_secret=secret,
        telegram_allowed_chat_ids=allowed_chat_ids or [],
        telegram_rate_limit_per_minute=rate_limit,
    )
    notifier = FakeNotifier()
    service = FakeTelegramService()
    app = create_app(
        settings=settings,
        telegram_notifier=notifier,
        enable_background_jobs=False,
    )
    app.state.telegram_command_service = service
    return TestClient(app), notifier, service


def test_webhook_rejects_missing_secret() -> None:
    client, _, service = _build_client(secret="secret-token")

    response = client.post("/telegram/webhook", json=_telegram_update(1001))

    assert response.status_code == 403
    assert response.json() == {"detail": "invalid_secret"}
    assert service.updates == []


def test_webhook_rejects_wrong_secret() -> None:
    client, _, service = _build_client(secret="secret-token")

    response = client.post(
        "/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "wrong-secret"},
        json=_telegram_update(1001),
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "invalid_secret"}
    assert service.updates == []


def test_webhook_silently_ignores_unauthorized_chat() -> None:
    client, notifier, service = _build_client(secret="secret-token", allowed_chat_ids=[1001])

    response = client.post(
        "/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "secret-token"},
        json=_telegram_update(2002),
    )

    assert response.status_code == 200
    assert response.content == b""
    assert service.updates == []
    assert notifier.sent == []
    assert service.logs.items == [("telegram_unauthorized_chat", {"chat_id": "2002"})]


def test_webhook_processes_authorized_chat() -> None:
    client, notifier, service = _build_client(secret="secret-token", allowed_chat_ids=[1001])
    update = _telegram_update(1001, "/health")

    response = client.post(
        "/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": "secret-token"},
        json=update,
    )

    assert response.status_code == 200
    assert response.json()["processed"] is True
    assert service.updates == [update]
    assert notifier.sent == []


def test_rate_limit_blocks_after_threshold() -> None:
    client, notifier, service = _build_client(
        secret="secret-token",
        allowed_chat_ids=[3003],
        rate_limit=30,
    )
    headers = {"X-Telegram-Bot-Api-Secret-Token": "secret-token"}

    for index in range(30):
        response = client.post(
            "/telegram/webhook",
            headers=headers,
            json=_telegram_update(3003, f"/scan {index}"),
        )
        assert response.status_code == 200
        assert response.json()["processed"] is True

    blocked = client.post(
        "/telegram/webhook",
        headers=headers,
        json=_telegram_update(3003, "/scan blocked"),
    )
    blocked_again = client.post(
        "/telegram/webhook",
        headers=headers,
        json=_telegram_update(3003, "/scan blocked again"),
    )

    assert blocked.status_code == 200
    assert blocked.json()["detail"] == "rate_limited"
    assert blocked_again.status_code == 200
    assert blocked_again.json()["detail"] == "rate_limited"
    assert len(service.updates) == 30
    assert notifier.sent == [("rate_limit_exceeded", "3003")]
