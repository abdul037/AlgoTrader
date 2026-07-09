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
                final_score=56.0,
                rejection_reasons=["relative_volume_too_low"],
                reason_codes=["relative_volume_too_low"],
                payload={
                    "current_price": 100.0,
                    "entry_price": 100.0,
                    "stop_loss": 98.0,
                    "take_profit": 104.0,
                    "risk_reward_ratio": 2.0,
                    "direction_label": "buy",
                    "signal_role": "entry_long",
                    "measurements": {"relative_volume": 0.78, "spread_bps": 5.0, "verified": True},
                    "metadata": {"market_data_verified": True},
                },
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
                payload={"measurements": {"relative_volume": 0.92}},
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
        paper_near_miss_promotion_enabled=True,
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
    assert near_misses["near_miss_promotable_count"] == 1
    assert "unsupported_reason:indicator_confluence_too_low" in near_misses["near_miss_top_blocked_reasons"]
    assert near_misses["examples"][0]["near_miss_promotable"] is True
    assert near_misses["examples"][1]["promotion_blockers"]
    assert tuning["dry_run"] is True
    assert tuning["mutated"] is False
    assert "broker" in tuning["blocked_changes"]
