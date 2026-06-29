from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.strategies.enhancement import StrategyEnhancementService
from app.strategies.routes import router as strategy_enhancement_router
from tests.conftest import make_settings


class FakeScanDecisionRepository:
    def __init__(self):
        self.items = [
            SimpleNamespace(
                created_at="2026-06-29T18:00:00+00:00",
                symbol="NVDA",
                strategy_name="volatility_contraction_breakout",
                timeframe="15m",
                status="no_signal",
                final_score=54.0,
                rejection_reasons=["relative_volume_too_low"],
                reason_codes=["relative_volume_too_low"],
            ),
            SimpleNamespace(
                created_at="2026-06-29T18:01:00+00:00",
                symbol="AAPL",
                strategy_name="rsi_vwap_ema_confluence",
                timeframe="5m",
                status="rejected",
                final_score=49.0,
                rejection_reasons=["indicator_confluence_too_low"],
                reason_codes=["indicator_confluence_too_low"],
            ),
        ]

    def list(self, *, limit: int = 100, **_kwargs):
        return self.items[:limit]


class FakeGovernance:
    @staticmethod
    def approved_paper_exploration_strategies():
        return ["rsi_vwap_ema_confluence"]


def _client(tmp_path) -> TestClient:
    app = FastAPI()
    settings = make_settings(
        tmp_path,
        control_api_token="control-secret",
        paper_scanner_exploration_enabled=True,
        paper_exploration_signal_profile="balanced_loose",
    )
    app.state.settings = settings
    app.state.strategy_enhancement_service = StrategyEnhancementService(
        settings=settings,
        scan_decisions=FakeScanDecisionRepository(),
        strategy_governance=FakeGovernance(),
    )
    app.include_router(strategy_enhancement_router)
    return TestClient(app)


def test_strategy_enhancement_routes_are_control_token_protected(tmp_path) -> None:
    client = _client(tmp_path)

    assert client.get("/strategies/enhancement/status").status_code == 403
    response = client.get("/strategies/enhancement/status", headers={"X-Control-Token": "control-secret"})

    assert response.status_code == 200
    assert response.json()["profile_active"] is True


def test_strategy_enhancement_near_misses_and_tuning_are_read_only(tmp_path) -> None:
    client = _client(tmp_path)
    headers = {"X-Control-Token": "control-secret"}

    near_misses = client.get("/strategies/enhancement/near-misses", headers=headers).json()
    tuning = client.post("/strategies/enhancement/run-paper-tuning", headers=headers).json()

    assert near_misses["rows_analyzed"] == 2
    assert near_misses["top_reasons"]["relative_volume_too_low"] == 1
    assert tuning["dry_run"] is True
    assert tuning["mutated"] is False
    assert "broker" in tuning["blocked_changes"]
