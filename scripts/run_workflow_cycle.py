"""Run one workflow cycle manually from the command line."""

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
from app.storage.repositories import AlertHistoryRepository, BacktestRepository, RunLogRepository, RuntimeStateRepository, SignalStateRepository, TrackedSignalRepository
from app.telegram_notify import TelegramNotifier
from app.workflow.service import SignalWorkflowService


def main() -> None:
    parser = argparse.ArgumentParser(description="Run one workflow cycle.")
    parser.add_argument("--task", choices=["scheduled", "swing", "intraday", "open-check", "daily-summary"], default="scheduled")
    parser.add_argument("--no-notify", action="store_true", help="Do not send Telegram messages.")
    args = parser.parse_args()

    settings = get_settings()
    db = Database(settings)
    db.initialize()

    notifier = TelegramNotifier(settings)
    market_data_engine = MarketDataEngine(settings)
    screener = MarketScreenerService(
        settings=settings,
        market_data_engine=market_data_engine,
        signal_state_repository=SignalStateRepository(db),
        run_log_repository=RunLogRepository(db),
        backtest_repository=BacktestRepository(db),
        telegram_notifier=notifier,
    )
    workflow = SignalWorkflowService(
        settings=settings,
        market_screener=screener,
        market_data_engine=market_data_engine,
        notifier=notifier,
        tracked_signals=TrackedSignalRepository(db),
        alert_history=AlertHistoryRepository(db),
        runtime_state=RuntimeStateRepository(db),
        run_logs=RunLogRepository(db),
    )

    notify = not args.no_notify
    if args.task == "scheduled":
        result = workflow.run_scheduled_tasks()
    elif args.task == "swing":
        result = workflow.run_swing_scan(notify=notify, force_refresh=True).model_dump()
    elif args.task == "intraday":
        result = workflow.run_intraday_scan(notify=notify, force_refresh=True).model_dump()
    elif args.task == "open-check":
        result = workflow.check_open_signals(notify=notify, force_refresh=True).model_dump()
    else:
        result = workflow.send_daily_summary(notify=notify).model_dump()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
