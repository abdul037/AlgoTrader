"""Generate the daily ledger operations health report."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.ledger.repository import LedgerRepository
from app.runtime_settings import get_settings
from app.storage.db import Database
from app.storage.repositories import RuntimeStateRepository
from app.telegram_notify import TelegramNotifier
from app.utils.time import utc_now


def build_report(repo: LedgerRepository, state: RuntimeStateRepository) -> str:
    pending = repo.pending_match_count()
    stale = repo.pending_match_older_than_count(hours=24)
    last_ledger = state.get("workflow:last_ledger_cycle_at")
    last_screener = _latest_state(
        state,
        [
            "workflow:last_premarket_scan_at",
            "workflow:last_market_open_scan_at",
            "workflow:last_intelligent_scan_at",
            "workflow:last_swing_scan_at",
            "workflow:last_intraday_scan_at",
            "workflow:last_end_of_day_scan_at",
        ],
    )
    last_error = state.get("etoro:last_api_error")
    status = "healthy"
    reason = "all ledger checks normal"
    if stale:
        status = "degraded"
        reason = f"{stale} pending_match outcomes older than 24h"
    if last_error:
        status = "degraded"
        reason = f"last eToro API error: {last_error}"
    if not last_ledger:
        status = "degraded"
        reason = "ledger cycle has not completed yet"

    return "\n".join(
        [
            "Daily ops report",
            f"Generated: {utc_now().isoformat()}",
            f"Status: {status}",
            f"Reason: {reason}",
            f"Last screener: {last_screener or 'never'}",
            f"Last ledger cycle: {last_ledger or 'never'}",
            f"Pending matches: {pending}",
            f"Pending >24h: {stale}",
        ]
    )


def _latest_state(state: RuntimeStateRepository, keys: list[str]) -> str | None:
    values = [state.get(key) for key in keys]
    values = [value for value in values if value]
    return max(values) if values else None


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate daily ledger ops report.")
    parser.add_argument("--no-telegram", action="store_true", help="Do not send the report to Telegram.")
    args = parser.parse_args()

    settings = get_settings()
    db = Database(settings)
    db.initialize()
    report = build_report(LedgerRepository(db), RuntimeStateRepository(db))
    print(report)

    if settings.telegram_enabled and not args.no_telegram:
        TelegramNotifier(settings).send_text(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
