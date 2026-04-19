"""Run a market universe scan from the command line."""

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
from app.screener.service import MarketScreenerService
from app.storage.db import Database
from app.storage.repositories import BacktestRepository, RunLogRepository, SignalStateRepository
from app.telegram_notify import TelegramNotifier


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the market universe screener.")
    parser.add_argument("--symbols", help="Comma-separated symbols override.")
    parser.add_argument("--timeframes", default=None, help="Comma-separated timeframes, e.g. 1d,1h,15m.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum number of candidates to return.")
    parser.add_argument("--validated-only", action="store_true", help="Only keep signals backed by passing backtests.")
    parser.add_argument("--notify", action="store_true", help="Send the ranked summary to Telegram.")
    parser.add_argument("--force-refresh", action="store_true", help="Bypass data cache.")
    args = parser.parse_args()

    settings = get_settings()
    db = Database(settings)
    db.initialize()

    service = MarketScreenerService(
        settings=settings,
        market_data_engine=MarketDataEngine(settings),
        signal_state_repository=SignalStateRepository(db),
        run_log_repository=RunLogRepository(db),
        backtest_repository=BacktestRepository(db),
        telegram_notifier=TelegramNotifier(settings),
    )
    result = service.scan_universe(
        symbols=[item.strip().upper() for item in args.symbols.split(",") if item.strip()] if args.symbols else None,
        timeframes=[item.strip().lower() for item in args.timeframes.split(",") if item.strip()] if args.timeframes else None,
        limit=args.limit,
        validated_only=args.validated_only,
        notify=args.notify,
        force_refresh=args.force_refresh,
    )
    print(json.dumps(result.model_dump(), indent=2))


if __name__ == "__main__":
    main()
