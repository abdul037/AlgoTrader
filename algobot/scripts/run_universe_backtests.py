"""Run batch backtests across the configured market universe."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.data.engine import MarketDataEngine
from app.runtime_settings import get_settings
from app.screener.service import BatchBacktestService
from app.storage.db import Database
from app.storage.repositories import BacktestRepository, RunLogRepository


def main() -> None:
    parser = argparse.ArgumentParser(description="Run batch backtests across the configured market universe.")
    parser.add_argument("--symbols", help="Comma-separated symbol override.")
    parser.add_argument("--timeframes", default="1d", help="Comma-separated timeframes.")
    parser.add_argument("--strategies", help="Comma-separated strategy names.")
    parser.add_argument("--limit", type=int, default=None, help="Cap the number of symbols from the universe.")
    parser.add_argument("--provider", default=None, help="Force a data provider, e.g. yfinance or etoro.")
    parser.add_argument("--initial-cash", type=float, default=10000.0, help="Starting capital per run.")
    parser.add_argument("--force-refresh", action="store_true", help="Bypass the data cache.")
    args = parser.parse_args()

    settings = get_settings()
    db = Database(settings)
    db.initialize()

    service = BatchBacktestService(
        settings=settings,
        market_data_engine=MarketDataEngine(settings),
        backtest_repository=BacktestRepository(db),
        run_log_repository=RunLogRepository(db),
    )
    summary = service.run(
        symbols=[item.strip().upper() for item in args.symbols.split(",") if item.strip()] if args.symbols else None,
        timeframes=[item.strip().lower() for item in args.timeframes.split(",") if item.strip()],
        strategy_names=[item.strip() for item in args.strategies.split(",") if item.strip()] if args.strategies else None,
        provider=args.provider,
        initial_cash=args.initial_cash,
        limit=args.limit,
        force_refresh=args.force_refresh,
    )
    print(json.dumps(summary.model_dump(), indent=2))


if __name__ == "__main__":
    main()
