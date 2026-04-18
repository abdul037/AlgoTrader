from __future__ import annotations

from pathlib import Path

from app.backtesting.engine import BacktestEngine
from app.data.market_data import MarketDataService
from app.storage.db import Database
from app.storage.repositories import BacktestRepository
from app.strategies.ma_crossover import MACrossoverStrategy
from tests.conftest import make_settings


def test_backtest_metrics_are_sane(tmp_path: Path) -> None:
    settings = make_settings(tmp_path)
    database = Database(settings)
    database.initialize()

    csv_path = Path(__file__).resolve().parents[1] / "sample_data" / "nvda.csv"
    data = MarketDataService().load_csv(csv_path)
    engine = BacktestEngine(BacktestRepository(database))
    result = engine.run(
        symbol="NVDA",
        strategy=MACrossoverStrategy(),
        data=data,
        file_path=str(csv_path),
        initial_cash=10000.0,
    )

    assert result.metrics["number_of_trades"] >= 1
    assert result.metrics["max_drawdown_pct"] >= 0
    assert "sharpe_like" in result.metrics
    assert result.ending_cash > 0
