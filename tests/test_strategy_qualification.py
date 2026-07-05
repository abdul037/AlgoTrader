from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import MockBroker, make_settings


class FakeBatchBacktestService:
    def run(self, **kwargs):
        return SimpleNamespace(
            generated_at="2026-07-05T12:00:00+00:00",
            symbols_evaluated=2,
            strategy_runs=4,
            provider="alpaca",
            errors=["MSFT 1d weak_strategy: invalid target generated"],
            audit_rankings=[
                {
                    "strategy_name": "strong_strategy",
                    "timeframe": "1d",
                    "runs": 2,
                    "total_trades": 240,
                    "average_expectancy_usd": 4.5,
                    "average_profit_factor": 1.55,
                    "average_sharpe_like": 1.35,
                    "average_max_drawdown_pct": 6.0,
                    "profitable_run_pct": 70.0,
                    "leakage_warning_count": 0,
                    "risk_adjusted_rank_score": 80.0,
                    "promotion_hint": "production_candidate",
                },
                {
                    "strategy_name": "weak_strategy",
                    "timeframe": "1d",
                    "runs": 2,
                    "total_trades": 42,
                    "average_expectancy_usd": -0.5,
                    "average_profit_factor": 0.9,
                    "average_sharpe_like": 0.2,
                    "average_max_drawdown_pct": 14.0,
                    "profitable_run_pct": 40.0,
                    "leakage_warning_count": 0,
                    "risk_adjusted_rank_score": 5.0,
                    "promotion_hint": "research_only",
                },
            ],
        )


def test_strategy_qualification_run_persists_promotion_decisions(tmp_path) -> None:
    app = create_app(
        make_settings(tmp_path, control_api_token="control-secret"),
        broker=MockBroker(),
        enable_background_jobs=False,
    )
    app.state.batch_backtest_service = FakeBatchBacktestService()
    client = TestClient(app)

    assert client.get("/strategies/qualification/status").status_code == 403

    run_response = client.post(
        "/strategies/qualification/run",
        headers={"X-Control-Token": "control-secret"},
        json={"strategy_names": ["strong_strategy", "weak_strategy"], "timeframes": ["1d"], "limit": 2},
    )
    status_response = client.get(
        "/strategies/qualification/status",
        headers={"X-Control-Token": "control-secret"},
    )

    assert run_response.status_code == 200
    payload = run_response.json()
    assert payload["approved_count"] == 1
    assert payload["failed_count"] == 1
    decisions = {item["strategy_name"]: item["decision"] for item in payload["decisions"]}
    assert decisions["strong_strategy"]["approved"] is True
    assert decisions["weak_strategy"]["approved"] is False
    assert "insufficient_out_of_sample_trades" in decisions["weak_strategy"]["blockers"]
    assert "non_positive_expectancy_after_costs" in decisions["weak_strategy"]["blockers"]

    assert status_response.status_code == 200
    status_payload = status_response.json()
    assert status_payload["production_qualified_count"] == 1
    assert len(status_payload["items"]) == 2
