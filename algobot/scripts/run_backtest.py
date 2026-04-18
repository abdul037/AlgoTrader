"""Run a strategy backtest from the command line."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backtesting.engine import BacktestEngine
from app.runtime_settings import get_settings
from app.data.market_data import MarketDataService
from app.storage.db import Database
from app.storage.repositories import BacktestRepository
from app.strategies import get_strategy


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a local backtest.")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--strategy", required=True)
    parser.add_argument("--file", required=True, dest="file_path")
    parser.add_argument("--initial-cash", type=float, default=10000.0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = get_settings()
    database = Database(settings)
    database.initialize()

    market_data = MarketDataService()
    data = market_data.load_csv(args.file_path)
    strategy = get_strategy(args.strategy)
    engine = BacktestEngine(BacktestRepository(database))
    result = engine.run(
        symbol=args.symbol,
        strategy=strategy,
        data=data,
        file_path=args.file_path,
        initial_cash=args.initial_cash,
    )
    print(json.dumps(result.model_dump(), indent=2))


if __name__ == "__main__":
    main()
