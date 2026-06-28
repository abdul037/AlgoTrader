from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.models.strategy_lab import (
    StrategyBacktestRequest,
    StrategyConceptPackRequest,
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
from app.strategy_lab.dsl import GeneratedRuleStrategy, _with_indicators
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


def test_strategy_lab_dsl_supports_expanded_safe_indicators() -> None:
    dsl = StrategyLabDsl(
        name="generated_expanded_indicator_test",
        description="Expanded indicator coverage",
        timeframe="1d",
        indicators=[
            StrategyLabIndicator(name="atr_14", kind="atr", source="close", period=14),
            StrategyLabIndicator(name="roc_10", kind="roc", source="close", period=10),
            StrategyLabIndicator(name="bb_upper_20", kind="bb_upper", source="close", period=20),
            StrategyLabIndicator(name="bb_lower_20", kind="bb_lower", source="close", period=20),
            StrategyLabIndicator(name="bb_width_20", kind="bb_width", source="close", period=20),
            StrategyLabIndicator(name="donchian_high_20", kind="donchian_high", source="high", period=20),
            StrategyLabIndicator(name="donchian_low_20", kind="donchian_low", source="low", period=20),
            StrategyLabIndicator(name="rv_20", kind="relative_volume", source="volume", period=20),
        ],
        entry_conditions=[StrategyLabCondition(kind="above", left="close", right="donchian_high_20")],
        stop_loss_pct=2.0,
        take_profit_pct=4.0,
    )

    enriched = _with_indicators(_frame(80), dsl)
    last = enriched.iloc[-1]

    for column in [
        "atr_14",
        "roc_10",
        "bb_upper_20",
        "bb_lower_20",
        "bb_width_20",
        "donchian_high_20",
        "donchian_low_20",
        "rv_20",
    ]:
        assert pd.notna(last[column])
    assert last["bb_upper_20"] > last["bb_lower_20"]
    assert last["donchian_high_20"] == enriched["high"].iloc[-21:-1].max()
    assert last["donchian_low_20"] == enriched["low"].iloc[-21:-1].min()


def test_strategy_lab_concept_pack_creates_twenty_five_idempotently(tmp_path) -> None:
    service, repository, _governance = _service(tmp_path)

    first = service.generate_concept_pack(StrategyConceptPackRequest())
    second = service.generate_concept_pack(StrategyConceptPackRequest())
    records = repository.list_generated(limit=100)
    names = {record.name for record in records}

    assert first["requested"] == 25
    assert len(first["created"]) == 25
    assert len(second["created"]) == 0
    assert len(second["existing"]) == 25
    assert len(records) == 25
    assert any("momentum" in name for name in names)
    assert any("volume_expansion" in name for name in names)
    assert any("regime" in name for name in names)


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
