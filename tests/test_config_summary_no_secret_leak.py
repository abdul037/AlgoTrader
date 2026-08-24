"""Regression guard: the public /config/summary must never expose secrets.

ConfigSummary is a hand-picked, non-secret projection today. This test sets
recognizable secret values and asserts none of them appear anywhere in the
serialized response, so a future careless field addition that leaks a token or
API key fails CI.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import make_settings

_SENTINELS = {
    "alpaca_api_key": "SECRET_ALPACA_KEY_zzz1",
    "alpaca_secret_key": "SECRET_ALPACA_SECRET_zzz2",
    "alpaca_live_api_key": "SECRET_ALPACA_LIVE_KEY_zzz3",
    "alpaca_live_secret_key": "SECRET_ALPACA_LIVE_SECRET_zzz4",
    "telegram_bot_token": "SECRET_TELEGRAM_TOKEN_zzz5",
    "telegram_webhook_secret": "SECRET_WEBHOOK_zzz6",
    "etoro_api_key": "SECRET_ETORO_KEY_zzz7",
    "etoro_user_key": "SECRET_ETORO_USER_zzz8",
    "control_api_token": "SECRET_CONTROL_TOKEN_zzz9",
}


def test_config_summary_does_not_leak_secrets(tmp_path: Path) -> None:
    app = create_app(settings=make_settings(tmp_path, **_SENTINELS), enable_background_jobs=False)
    client = TestClient(app)

    response = client.get("/config/summary")

    assert response.status_code == 200
    body = response.text
    leaked = [name for name, value in _SENTINELS.items() if value in body]
    assert not leaked, f"/config/summary leaked secret values for: {leaked}"
