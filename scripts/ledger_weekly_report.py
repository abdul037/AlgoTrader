"""Generate the weekly ledger performance report."""

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
from app.telegram_notify import TelegramNotifier
from app.utils.time import utc_now


def build_report(repo: LedgerRepository) -> str:
    stats = repo.summary_stats()
    lines = [
        "Weekly ledger performance report",
        f"Generated: {utc_now().isoformat()}",
        f"Total outcomes: {int(stats.get('total_outcomes') or 0)}",
        f"Status counts: {stats.get('by_status') or {}}",
        (
            f"Closed: {int(stats.get('closed_count') or 0)} | "
            f"W/L: {int(stats.get('wins') or 0)}/{int(stats.get('losses') or 0)} | "
            f"Win rate: {_fmt_pct(stats.get('win_rate'))}"
        ),
        (
            f"Profit factor: {_fmt_decimal(stats.get('profit_factor'))} | "
            f"Avg R: {_fmt_r(stats.get('avg_r_multiple'))} | "
            f"Avg hold: {_fmt_hours(stats.get('avg_hold_hours'))}"
        ),
        "",
        "By strategy:",
    ]
    strategies = list(stats.get("by_strategy") or [])
    if not strategies:
        lines.append("No strategy outcomes yet.")
    for item in strategies:
        lines.append(
            f"- {item.get('strategy_name')}: "
            f"total {int(item.get('total') or 0)}, "
            f"closed {int(item.get('closed') or 0)}, "
            f"WR {_fmt_pct(item.get('win_rate'))}, "
            f"PF {_fmt_decimal(item.get('profit_factor'))}, "
            f"avgR {_fmt_r(item.get('avg_r_multiple'))}, "
            f"avg hold {_fmt_hours(item.get('avg_hold_hours'))}"
        )
    return "\n".join(lines)


def _fmt_decimal(value: object) -> str:
    if value in (None, ""):
        return "n/a"
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value)


def _fmt_pct(value: object) -> str:
    if value in (None, ""):
        return "n/a"
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return str(value)


def _fmt_r(value: object) -> str:
    if value in (None, ""):
        return "n/a"
    try:
        return f"{float(value):+.2f}R"
    except (TypeError, ValueError):
        return str(value)


def _fmt_hours(value: object) -> str:
    if value in (None, ""):
        return "n/a"
    try:
        hours = float(value)
    except (TypeError, ValueError):
        return str(value)
    if hours < 48:
        return f"{hours:.1f}h"
    return f"{hours / 24.0:.1f}d"


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate weekly ledger performance report.")
    parser.add_argument("--no-telegram", action="store_true", help="Do not send the report to Telegram.")
    parser.add_argument("--output", default=None, help="Optional markdown output path.")
    args = parser.parse_args()

    settings = get_settings()
    db = Database(settings)
    db.initialize()
    report = build_report(LedgerRepository(db))
    print(report)

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(report + "\n", encoding="utf-8")

    if settings.telegram_enabled and not args.no_telegram:
        TelegramNotifier(settings).send_text(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
