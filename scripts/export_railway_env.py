"""Create a validated, Git-ignored Railway shadow-mode variable bundle."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.validate_railway_env import validate  # noqa: E402

COPIED_NAMES = (
    "DATABASE_URL",
    "CONTROL_API_TOKEN",
    "ALPACA_API_KEY",
    "ALPACA_SECRET_KEY",
    "ALPACA_BASE_URL",
    "ALPACA_DATA_URL",
    "ALPACA_DATA_FEED",
    "ALLOWED_INSTRUMENTS",
    "BLOCKED_INSTRUMENTS",
    "PRIMARY_MARKET_DATA_PROVIDER",
    "FALLBACK_MARKET_DATA_PROVIDER",
    "SIGNAL_SCAN_UNIVERSE",
    "SCREENER_ACTIVE_STRATEGY_NAMES",
    "SCREENER_TOP_K",
    "MIN_BACKTEST_TRADES_FOR_ALERTS",
    "MIN_BACKTEST_PROFIT_FACTOR",
    "MIN_BACKTEST_ANNUALIZED_RETURN_PCT",
    "MAX_BACKTEST_DRAWDOWN_PCT",
    "REQUIRE_BACKTEST_VALIDATION_FOR_ALERTS",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
    "TELEGRAM_ALLOWED_CHAT_IDS",
    "TELEGRAM_WEBHOOK_SECRET",
)

SHADOW_VALUES = {
    "DEPLOYMENT_STAGE": "shadow",
    "ALPACA_ENABLED": "true",
    "ALPACA_EXPECTED_ACCOUNT_NUMBER": "PA3B287XBZYU",
    "ALPACA_RECONCILIATION_ENABLED": "true",
    "ALPACA_RECONCILIATION_INTERVAL_SECONDS": "60",
    "ALPACA_REQUIRE_BRACKET_ORDERS": "true",
    "EXECUTION_MODE": "paper",
    "PAPER_BROKER": "alpaca",
    "BROKER_FOR_EQUITIES": "alpaca",
    "BROKER_FOR_NON_EQUITIES": "etoro",
    "ETORO_ACCOUNT_MODE": "demo",
    "ENABLE_REAL_TRADING": "false",
    "REQUIRE_APPROVAL": "true",
    "PAPER_SIMULATED_FALLBACK_ENABLED": "false",
    "AUTOMATION_PAUSED_DEFAULT": "false",
    "KILL_SWITCH_ENABLED": "false",
    "KILL_SWITCH_AUTO_CLOSE_POSITIONS": "false",
    "PAPER_AUTO_APPROVE_PROPOSALS": "false",
    "AUTO_EXECUTION_WORKER_ENABLED": "false",
    "PAPER_AUTO_OPERATION_MODE": "shadow",
    "INSTITUTIONAL_PORTFOLIO_CONTROLS_ENABLED": "false",
    "AUTO_PROPOSE_ENABLED": "false",
    "AUTO_EXECUTE_AFTER_APPROVAL": "false",
    "SCREENER_SCHEDULER_ENABLED": "true",
    "LEDGER_CYCLE_ENABLED": "false",
    "MARKET_UNIVERSE_NAME": "top100_us",
    "MARKET_UNIVERSE_TIER": "broad_top100",
    "MARKET_UNIVERSE_LIMIT": "100",
    "WORKFLOW_SCAN_DEFAULT_UNIVERSE_LIMIT": "100",
    "SCALP_SCAN_BATCH_SIZE": "20",
    "INTRADAY_REPEATED_SCAN_ENABLED": "true",
    "INTRADAY_SCAN_INTERVAL_MINUTES": "15",
    "SWING_SCAN_INTERVAL_MINUTES": "60",
    "SCHEDULE_TIMEZONE": "America/New_York",
    "MAX_TRADE_AMOUNT_USD": "1000",
    "MAX_TRADES_PER_DAY": "5",
    "MAX_OPEN_POSITIONS": "3",
    "MAX_DAILY_LOSS_USD": "100",
    "MAX_WEEKLY_LOSS_USD": "300",
    "MAX_RISK_PER_TRADE_PCT": "1",
    "MAX_CONSECUTIVE_LOSSES_BEFORE_COOLDOWN": "2",
    "TELEGRAM_ENABLED": "false",
    "TELEGRAM_POLLING_ENABLED": "false",
}


def build_values(source: dict[str, str | None]) -> dict[str, str]:
    values = {name: str(source.get(name) or "") for name in COPIED_NAMES}
    values.update(SHADOW_VALUES)
    return values


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=".env")
    parser.add_argument("--output", default=".railway.env")
    args = parser.parse_args()

    source_path = Path(args.source)
    output_path = Path(args.output)
    if not source_path.exists():
        raise SystemExit(f"Source environment file does not exist: {source_path}")

    values = build_values(dotenv_values(source_path))
    previous = os.environ.copy()
    os.environ.clear()
    os.environ.update(values)
    try:
        errors = validate()
    finally:
        os.environ.clear()
        os.environ.update(previous)
    if errors:
        raise SystemExit("\n".join(f"Railway environment error: {error}" for error in errors))

    output_path.write_text("".join(f"{name}={value}\n" for name, value in sorted(values.items())))
    output_path.chmod(0o600)
    print(f"Created validated Railway shadow variables in {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
