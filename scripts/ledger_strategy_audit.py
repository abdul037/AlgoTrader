"""Generate a ledger-backed strategy quality audit.

This report does not tune thresholds, train models, or place trades. It only
summarizes actual ledger outcomes so strategy changes are driven by evidence.
"""

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


def build_report(repo: LedgerRepository, *, min_closed: int = 20) -> str:
    audit = repo.strategy_audit(min_closed=min_closed)
    overall = audit["overall"]
    lines = [
        "Strategy quality audit",
        f"Generated: {audit['generated_at']}",
        f"Decision floor: {audit['min_closed_for_decision']} closed outcomes per bucket",
        "",
        "Overall:",
        _format_metric_line(overall),
        f"Recommendation: {overall['recommendation']} - {overall['recommendation_reason']}",
        "",
        "By strategy:",
    ]
    _append_section(lines, audit["by_strategy"])
    lines.append("")
    lines.append("By score bucket:")
    _append_section(lines, audit["by_score_bucket"])
    lines.append("")
    lines.append("By timeframe:")
    _append_section(lines, audit["by_timeframe"])
    lines.append("")
    lines.append("By symbol:")
    _append_section(lines, audit["by_symbol"], limit=12)
    return "\n".join(lines)


def _append_section(lines: list[str], items: list[dict], *, limit: int = 8) -> None:
    if not items:
        lines.append("- no data")
        return
    for item in items[:limit]:
        lines.append(f"- {_format_metric_line(item)}")


def _format_metric_line(item: dict) -> str:
    return (
        f"{item['name']}: alerts {int(item.get('total_alerts') or 0)}, "
        f"matched {int(item.get('matched_count') or 0)} ({_fmt_pct(item.get('match_rate'))}), "
        f"closed {int(item.get('closed_count') or 0)}, "
        f"WR {_fmt_pct(item.get('win_rate'))}, "
        f"PF {_fmt_decimal(item.get('profit_factor'))}, "
        f"avgR {_fmt_r(item.get('avg_r_multiple'))}, "
        f"avgScore {_fmt_decimal(item.get('avg_score'))}, "
        f"rec {item.get('recommendation')} ({item.get('recommendation_reason')})"
    )


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


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate ledger-backed strategy audit.")
    parser.add_argument("--min-closed", type=int, default=20, help="Closed outcomes needed before keep/reduce decisions.")
    parser.add_argument("--no-telegram", action="store_true", help="Do not send the report to Telegram.")
    parser.add_argument("--output", default=None, help="Optional markdown output path.")
    args = parser.parse_args()

    settings = get_settings()
    db = Database(settings)
    db.initialize()
    report = build_report(LedgerRepository(db), min_closed=max(args.min_closed, 1))
    print(report)

    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(report + "\n", encoding="utf-8")

    if settings.telegram_enabled and not args.no_telegram:
        TelegramNotifier(settings).send_text(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
