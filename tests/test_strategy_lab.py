from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models.strategy_lab import (
    StrategyBacktestRequest,
    StrategyGenerationRequest,
    StrategyLabCondition,
    StrategyLabDsl,
    StrategyLabIndicator,
    StrategyPromotionRequest,
)
from app.storage.db import Database
from app.storage.repositories import (
    BacktestRepository,
    RunLogRepository,
    StrategyGovernanceRepository,
    StrategyLabRepository,
)
from app.strategy_lab.dsl import GeneratedRuleStrategy
from app.strategy_lab.routes import router as strategy_lab_router
from app.strategy_lab.service import StrategyLabService
from tests.conftest import make_settings


class FakeMarketData:
    def __init__(self, frame: pd.DataFrame):
        self.frame = frame

    def get_history(self, symbol: str, **_kwargs):
        return self.frame.copy()


def _frame(length: int = 80) -> pd.DataFrame:
    timestamps = pd.date_range(datetime(2026, 1, 1, tzinfo=UTC), periods=length, freq="1D")
    rows = []
    for index, timestamp in enumerate(timestamps):
        close = 100.0 + index
        rows.append(
            {
                "timestamp": timestamp,
                "open": close - 0.25,
                "high": close + 5.0,
                "low": close - 1.0,
                "close": close,
                "volume": 1_000_000 + index,
            }
        )
    return pd.DataFrame(rows)


def _dsl(name: str = "generated_test_trend") -> StrategyLabDsl:
    return StrategyLabDsl(
        name=name,
        description="Generated test trend continuation strategy",
        timeframe="1d",
        indicators=[
            StrategyLabIndicator(name="sma_fast", kind="sma", source="close", period=3),
            StrategyLabIndicator(name="sma_slow", kind="sma", source="close", period=5),
        ],
        entry_conditions=[
            StrategyLabCondition(kind="above", left="sma_fast", right="sma_slow"),
        ],
        stop_loss_pct=2.0,
        take_profit_pct=4.0,
        max_hold_bars=10,
        confidence=0.7,
    )


def _service(tmp_path, *, frame: pd.DataFrame | None = None) -> tuple[StrategyLabService, StrategyLabRepository, StrategyGovernanceRepository]:
    settings = make_settings(
        tmp_path,
        strategy_lab_enabled=True,
        strategy_lab_generation_enabled=True,
        strategy_lab_paper_trading_enabled=True,
        strategy_lab_min_backtest_trades=1,
        strategy_lab_min_profit_factor=1.0,
        strategy_lab_max_drawdown_pct=100.0,
        market_universe_symbols=["AAPL"],
    )
    db = Database(settings)
    db.initialize()
    lab_repository = StrategyLabRepository(db)
    governance = StrategyGovernanceRepository(db)
    service = StrategyLabService(
        settings=settings,
        repository=lab_repository,
        market_data_engine=FakeMarketData(frame or _frame()),
        backtest_repository=BacktestRepository(db),
        run_log_repository=RunLogRepository(db),
        strategy_governance=governance,
    )
    return service, lab_repository, governance


def test_generated_strategy_dsl_rejects_unavailable_operands() -> None:
    with pytest.raises(ValueError, match="right operand"):
        StrategyLabDsl(
            name="unsafe",
            description="bad",
            timeframe="1d",
            indicators=[StrategyLabIndicator(name="sma_fast", kind="sma", source="close", period=3)],
            entry_conditions=[StrategyLabCondition(kind="above", left="sma_fast", right="account_secret")],
            stop_loss_pct=2.0,
            take_profit_pct=4.0,
        )


def test_generated_rule_strategy_builds_long_only_valid_signal() -> None:
    strategy = GeneratedRuleStrategy(_dsl())

    signal = strategy.generate_signal(_frame(30), "AAPL")

    assert signal is not None
    assert signal.strategy_name == "generated_test_trend"
    assert signal.action.value == "buy"
    assert signal.stop_loss < signal.price < signal.take_profit
    assert signal.metadata["strategy_lab_generated"] is True


def test_strategy_lab_backtest_and_promote_paper_records_governance(tmp_path) -> None:
    service, repository, governance = _service(tmp_path)
    generated = service.generate(StrategyGenerationRequest(dsl=_dsl(), source="test"))

    backtest = service.backtest(generated.id, StrategyBacktestRequest(symbols=["AAPL"], limit=1))
    promoted = service.promote_paper(generated.id, StrategyPromotionRequest(decided_by="test"))

    assert backtest.status == "passed"
    assert repository.get_generated(generated.id).status == "paper_generated"
    assert governance.strategy_paper_exploration_approved(generated.name) is True
    assert promoted["strategy"].name == generated.name
    active_specs = service.active_specs(timeframe="1d")
    assert [spec.name for spec in active_specs] == [generated.name]
    assert isinstance(service.build_strategy_for_spec(active_specs[0]), GeneratedRuleStrategy)


def test_strategy_lab_routes_are_control_token_protected(tmp_path) -> None:
    app = FastAPI()
    app.state.settings = make_settings(tmp_path, control_api_token="control-secret", strategy_lab_enabled=True)
    app.state.strategy_lab_service = SimpleNamespace(status=lambda: {"enabled": True})
    app.include_router(strategy_lab_router)
    client = TestClient(app)

    assert client.get("/strategy-lab/status").status_code == 403
    response = client.get("/strategy-lab/status", headers={"X-Control-Token": "control-secret"})

    assert response.status_code == 200
    assert response.json()["enabled"] is True
