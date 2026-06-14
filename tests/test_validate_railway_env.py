from __future__ import annotations

from scripts.validate_railway_env import validate


def valid_shadow_environment(monkeypatch) -> None:
    values = {
        "DATABASE_URL": "postgresql+psycopg://user:password@host/postgres?sslmode=require",
        "CONTROL_API_TOKEN": "control-secret-at-least-32-characters",
        "DEPLOYMENT_STAGE": "shadow",
        "EXECUTION_MODE": "paper",
        "ENABLE_REAL_TRADING": "false",
        "ALPACA_ENABLED": "true",
        "ALPACA_API_KEY": "key",
        "ALPACA_SECRET_KEY": "secret",
        "ALPACA_BASE_URL": "https://paper-api.alpaca.markets/v2",
        "ALPACA_EXPECTED_ACCOUNT_NUMBER": "PA3B287XBZYU",
        "ALPACA_RECONCILIATION_ENABLED": "true",
        "ALPACA_REQUIRE_BRACKET_ORDERS": "true",
        "PAPER_SIMULATED_FALLBACK_ENABLED": "false",
        "AUTOMATION_PAUSED_DEFAULT": "false",
        "KILL_SWITCH_ENABLED": "false",
        "SCREENER_SCHEDULER_ENABLED": "true",
        "PAPER_AUTO_APPROVE_PROPOSALS": "false",
        "AUTO_EXECUTION_WORKER_ENABLED": "false",
        "AUTO_PROPOSE_ENABLED": "false",
        "AUTO_EXECUTE_AFTER_APPROVAL": "false",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)


def test_valid_shadow_environment_passes(monkeypatch) -> None:
    valid_shadow_environment(monkeypatch)

    assert validate() == []


def test_shadow_environment_rejects_unsafe_execution(monkeypatch) -> None:
    valid_shadow_environment(monkeypatch)
    monkeypatch.setenv("AUTO_EXECUTION_WORKER_ENABLED", "true")
    monkeypatch.setenv("ENABLE_REAL_TRADING", "true")

    errors = validate()

    assert "AUTO_EXECUTION_WORKER_ENABLED must be false in shadow mode" in errors
    assert "ENABLE_REAL_TRADING must be false" in errors


def test_shadow_environment_rejects_pause_and_kill_switch_defaults(monkeypatch) -> None:
    valid_shadow_environment(monkeypatch)
    monkeypatch.setenv("AUTOMATION_PAUSED_DEFAULT", "true")
    monkeypatch.setenv("KILL_SWITCH_ENABLED", "true")

    errors = validate()

    assert "AUTOMATION_PAUSED_DEFAULT must be false in shadow mode" in errors
    assert "KILL_SWITCH_ENABLED must be false in shadow mode" in errors


def test_bootstrap_environment_requires_pause_and_kill_switch(monkeypatch) -> None:
    valid_shadow_environment(monkeypatch)
    monkeypatch.setenv("DEPLOYMENT_STAGE", "bootstrap")
    monkeypatch.setenv("AUTOMATION_PAUSED_DEFAULT", "true")
    monkeypatch.setenv("KILL_SWITCH_ENABLED", "true")

    assert validate() == []


def test_environment_rejects_sqlite_and_wrong_account(monkeypatch) -> None:
    valid_shadow_environment(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "sqlite:///./bot.db")
    monkeypatch.setenv("ALPACA_EXPECTED_ACCOUNT_NUMBER", "wrong")

    errors = validate()

    assert "DATABASE_URL must use postgresql+psycopg" in errors
    assert "ALPACA_EXPECTED_ACCOUNT_NUMBER must be PA3B287XBZYU" in errors
