from __future__ import annotations

from scripts.export_railway_env import build_values
from scripts.validate_railway_env import validate


def test_export_builds_valid_shadow_environment(monkeypatch) -> None:
    values = build_values(
        {
            "DATABASE_URL": "postgresql+psycopg://user:password@host/postgres?sslmode=require",
            "ALPACA_API_KEY": "key",
            "ALPACA_SECRET_KEY": "secret",
            "ALPACA_BASE_URL": "https://paper-api.alpaca.markets/v2",
        }
    )
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    assert validate() == []
    assert values["DEPLOYMENT_STAGE"] == "shadow"
    assert values["KILL_SWITCH_ENABLED"] == "true"
    assert values["PAPER_AUTO_APPROVE_PROPOSALS"] == "false"
    assert values["AUTO_EXECUTION_WORKER_ENABLED"] == "false"
    assert "ETORO_API_KEY" not in values
    assert "POSTGRES_BACKUP_URL" not in values
