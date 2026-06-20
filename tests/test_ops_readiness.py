from __future__ import annotations

from app.runtime_settings import get_settings
from app.storage.db import Database
from scripts import ops_readiness
from tests.conftest import make_settings


def _clear_settings_cache() -> None:
    get_settings.cache_clear()


def test_deployment_checks_report_missing_openai_key_without_calling_railway(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "bot.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("RAILWAY_TOKEN", "token")
    monkeypatch.delenv("LEARNING_OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _clear_settings_cache()

    checks = {check.name: check for check in ops_readiness._deployment_checks()}

    assert checks["railway_auth"].status == "pass"
    assert checks["openai_api_key"].status == "fail"
    assert checks["paper_safety_flags"].status == "pass"


def test_deployment_checks_never_print_openai_key_fragments(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "bot.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setenv("RAILWAY_TOKEN", "token")
    monkeypatch.setenv("LEARNING_OPENAI_API_KEY", "sk-proj-secret-fragment")
    _clear_settings_cache()

    checks = {check.name: check for check in ops_readiness._deployment_checks()}

    assert checks["openai_api_key"].status == "pass"
    assert checks["openai_api_key"].detail == "LEARNING_OPENAI_API_KEY/OPENAI_API_KEY is present(23 chars)"
    assert "sk-" not in checks["openai_api_key"].detail
    assert "secret" not in checks["openai_api_key"].detail


def test_database_checks_report_missing_paper_auto_evidence(monkeypatch, tmp_path) -> None:
    settings = make_settings(tmp_path)
    Database(settings).initialize()
    monkeypatch.setenv("DATABASE_URL", settings.database_url)
    _clear_settings_cache()

    checks = {check.name: check for check in ops_readiness._database_checks("2026-06-17T00:00:00+00:00")}

    assert checks["strategy_versions"].status == "fail"
    assert checks["approved_strategies"].status == "fail"
    assert checks["rollout_gates"].status == "fail"
    assert checks["portfolio_risk"].status == "fail"
    assert checks["stage_gate_progress"].detail == "0/9 required gates passed for stage_1_validation"
