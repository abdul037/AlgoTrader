"""Evaluate live eToro signals without placing any order."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.broker.etoro_market_data import EtoroMarketDataClient
from app.runtime_settings import get_settings
from app.notifications.telegram import TelegramNotifier
from app.signals.service import LiveSignalService
from app.storage.db import Database
from app.storage.repositories import RunLogRepository, SignalRepository, SignalStateRepository


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check live eToro market-data signals.")
    parser.add_argument("--symbol", help="Evaluate one symbol, for example NVDA.")
    parser.add_argument("--scan", action="store_true", help="Run a ranked universe scan.")
    parser.add_argument("--limit", type=int, default=20, help="Scan result limit.")
    parser.add_argument("--notify", action="store_true", help="Send Telegram alerts on state changes.")
    parser.add_argument("--commit", action="store_true", help="Persist the latest state to SQLite.")
    parser.add_argument("--supported-only", action="store_true", help="Return only symbols the bot can execute.")
    return parser.parse_args()


def build_service() -> LiveSignalService:
    settings = get_settings()
    database = Database(settings)
    database.initialize()
    return LiveSignalService(
        settings=settings,
        market_data_client=EtoroMarketDataClient(settings),
        signal_repository=SignalRepository(database),
        signal_state_repository=SignalStateRepository(database),
        run_log_repository=RunLogRepository(database),
        telegram_notifier=TelegramNotifier(settings),
    )


def main() -> None:
    args = parse_args()
    service = build_service()

    if args.scan:
        result = service.scan_market(
            limit=args.limit,
            supported_only=args.supported_only,
            commit=args.commit or args.notify,
            notify=args.notify,
        )
        print(json.dumps(result.model_dump(), indent=2))
        return

    if not args.symbol:
        raise SystemExit("Provide --symbol SYMBOL or use --scan")

    result = service.get_latest_signal(
        args.symbol,
        commit=args.commit or args.notify,
        notify=args.notify,
    )
    print(json.dumps(result.model_dump(), indent=2))


if __name__ == "__main__":
    main()
