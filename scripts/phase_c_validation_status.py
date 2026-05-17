"""Read-only Phase C validation status report.

This script does not submit orders or change automation state. It summarizes
whether the Alpaca paper validation exit criteria have evidence in the local
database and runtime API.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize Phase C Alpaca paper validation status.")
    parser.add_argument("--db", default=str(ROOT / "etoro_bot.db"), help="SQLite database path")
    parser.add_argument("--base-url", default="http://127.0.0.1:8011", help="FastAPI base URL")
    parser.add_argument("--output", help="Optional markdown output path")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Database not found: {db_path}", file=sys.stderr)
        return 1

    report = build_report(db_path=db_path, base_url=args.base_url)
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(report, encoding="utf-8")
        print(f"Wrote {output}")
    else:
        print(report)
    return 0


def build_report(*, db_path: Path, base_url: str) -> str:
    now_local = datetime.now().astimezone()
    now_ny = datetime.now(ZoneInfo("America/New_York"))
    automation = fetch_json(f"{base_url.rstrip('/')}/automation/status")
    health = fetch_json(f"{base_url.rstrip('/')}/health")
    queue_rows = query_rows(
        db_path,
        """
        SELECT id, proposal_id, symbol, strategy_name, status, mode, ready_for_execution,
               validation_reason, updated_at, payload_json
        FROM execution_queue
        ORDER BY updated_at DESC
        LIMIT 10
        """,
    )
    execution_rows = query_rows(
        db_path,
        """
        SELECT id, proposal_id, status, mode, broker_order_id, request_json, response_json,
               error_message, created_at, updated_at
        FROM executions
        ORDER BY created_at DESC
        LIMIT 20
        """,
    )
    log_rows = query_rows(
        db_path,
        """
        SELECT event_type, payload_json, created_at
        FROM run_logs
        ORDER BY created_at DESC
        LIMIT 50
        """,
    )
    checks = derive_checks(queue_rows=queue_rows, execution_rows=execution_rows, log_rows=log_rows)
    market_status = market_window_status(now_ny)
    lines = [
        "# Phase C Validation Status",
        "",
        f"- Generated local: {now_local.isoformat(timespec='seconds')}",
        f"- Generated New York: {now_ny.isoformat(timespec='seconds')}",
        f"- US regular market window: {market_status}",
        f"- FastAPI health: {health.get('status', 'unavailable') if isinstance(health, dict) else 'unavailable'}",
        (
            "- Automation: "
            f"paused={automation.get('paused', 'n/a') if isinstance(automation, dict) else 'n/a'}, "
            f"kill_switch={automation.get('kill_switch_enabled', 'n/a') if isinstance(automation, dict) else 'n/a'}, "
            f"auto_propose={automation.get('auto_propose_enabled', 'n/a') if isinstance(automation, dict) else 'n/a'}, "
            f"auto_execute={automation.get('auto_execute_after_approval', 'n/a') if isinstance(automation, dict) else 'n/a'}, "
            f"reason={automation.get('reason', 'n/a') if isinstance(automation, dict) else 'n/a'}"
        ),
        "",
        "## Checklist",
        "",
    ]
    for check in checks:
        lines.append(f"- **{check.status}** {check.name}: {check.detail}")
    lines.extend(["", "## Recent Execution Queue", ""])
    if queue_rows:
        lines.append("| queue_id | symbol | strategy | status | ready | reason | updated_at |")
        lines.append("|---|---|---|---|---:|---|---|")
        for row in queue_rows[:5]:
            lines.append(
                f"| {row['id']} | {row['symbol']} | {row['strategy_name']} | {row['status']} | "
                f"{row['ready_for_execution']} | {row['validation_reason'] or ''} | {row['updated_at']} |"
            )
    else:
        lines.append("No execution queue records found.")
    lines.extend(["", "## Recent Executions", ""])
    if execution_rows:
        lines.append("| execution_id | proposal | strategy | status | mode | broker_order_id | filled_qty | created_at |")
        lines.append("|---|---|---|---|---|---|---:|---|")
        for row in execution_rows[:5]:
            request = parse_json(row.get("request_json"))
            response = parse_json(row.get("response_json"))
            filled_qty = broker_filled_qty(response)
            broker_order_id = str(row.get("broker_order_id") or "")
            if broker_order_id:
                broker_order_id = broker_order_id[:8] + "..."
            lines.append(
                f"| {row['id']} | {row['proposal_id']} | {request.get('strategy_name', '')} | "
                f"{row['status']} | {row['mode']} | {broker_order_id} | {filled_qty} | {row['created_at']} |"
            )
    else:
        lines.append("No execution records found.")
    lines.extend(
        [
            "",
            "## Next Action",
            "",
            next_action(market_status=market_status, automation=automation, checks=checks),
            "",
        ]
    )
    return "\n".join(lines)


def derive_checks(
    *,
    queue_rows: list[dict[str, Any]],
    execution_rows: list[dict[str, Any]],
    log_rows: list[dict[str, Any]],
) -> list[Check]:
    smoke_execution = first_execution(execution_rows, strategy_name="manual_smoke")
    strategy_execution = first_strategy_execution(execution_rows)
    strategy_fill = first_strategy_fill(execution_rows)
    duplicate_order_hint = has_duplicate_broker_order_ids(execution_rows)
    kill_switch_drill = any(
        row.get("event_type") == "kill_switch_emergency_stop"
        and "phase c" in str(row.get("payload_json") or "").lower()
        for row in log_rows
    )
    unexplained_blocks = [
        row
        for row in queue_rows
        if row.get("status") == "blocked"
        and row.get("strategy_name") != "manual_smoke"
    ]
    return [
        Check(
            "Alpaca paper smoke routing proof",
            "PASS" if smoke_execution else "PENDING",
            "manual_smoke reached Alpaca Paper" if smoke_execution else "no manual_smoke Alpaca execution found",
        ),
        Check(
            "Strategy-approved Alpaca paper order",
            "PASS" if strategy_execution else "PENDING",
            "strategy execution reached Alpaca Paper" if strategy_execution else "no non-smoke Alpaca execution found",
        ),
        Check(
            "Market-hours paper fill",
            "PASS" if strategy_fill else "PENDING",
            "non-smoke strategy execution has filled quantity > 0" if strategy_fill else "no filled non-smoke strategy execution found",
        ),
        Check(
            "Idempotency evidence",
            "REVIEW" if strategy_execution and not duplicate_order_hint else "PENDING",
            (
                "strategy execution exists; manually verify same queue re-process created no duplicate Alpaca order"
                if strategy_execution and not duplicate_order_hint
                else "no strategy execution yet, or duplicate broker order ids require review"
            ),
        ),
        Check(
            "Kill switch drill after strategy order",
            "PASS" if kill_switch_drill else "PENDING",
            "phase c emergency stop logged" if kill_switch_drill else "no phase c kill_switch_emergency_stop log found",
        ),
        Check(
            "48-hour observation",
            "REVIEW" if not unexplained_blocks else "PENDING",
            (
                "no recent non-smoke blocked queue records in last 10 rows; manual 48h log review still required"
                if not unexplained_blocks
                else f"{len(unexplained_blocks)} recent non-smoke blocked queue record(s) need review"
            ),
        ),
    ]


def first_execution(rows: list[dict[str, Any]], *, strategy_name: str) -> dict[str, Any] | None:
    for row in rows:
        request = parse_json(row.get("request_json"))
        response = parse_json(row.get("response_json"))
        if request.get("strategy_name") == strategy_name and response.get("broker") == "alpaca":
            return row
    return None


def first_strategy_execution(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    for row in rows:
        request = parse_json(row.get("request_json"))
        response = parse_json(row.get("response_json"))
        strategy_name = str(request.get("strategy_name") or "")
        if strategy_name and strategy_name != "manual_smoke" and response.get("broker") == "alpaca":
            return row
    return None


def first_strategy_fill(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    for row in rows:
        request = parse_json(row.get("request_json"))
        response = parse_json(row.get("response_json"))
        strategy_name = str(request.get("strategy_name") or "")
        if strategy_name and strategy_name != "manual_smoke" and broker_filled_qty(response) > 0:
            return row
    return None


def has_duplicate_broker_order_ids(rows: list[dict[str, Any]]) -> bool:
    seen: set[str] = set()
    for row in rows:
        broker_order_id = str(row.get("broker_order_id") or "")
        if not broker_order_id:
            continue
        if broker_order_id in seen:
            return True
        seen.add(broker_order_id)
    return False


def broker_filled_qty(response: dict[str, Any]) -> float:
    nested = response.get("broker_execution") if isinstance(response.get("broker_execution"), dict) else {}
    payload = nested.get("response_payload") if isinstance(nested.get("response_payload"), dict) else {}
    try:
        return float(payload.get("filled_qty") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def market_window_status(now_ny: datetime) -> str:
    minutes = now_ny.hour * 60 + now_ny.minute
    open_minutes = 9 * 60 + 30
    close_minutes = 16 * 60
    if now_ny.weekday() >= 5:
        return "closed_weekend"
    if open_minutes <= minutes < close_minutes:
        return "open_regular_hours"
    return "closed_outside_regular_hours"


def next_action(*, market_status: str, automation: Any, checks: list[Check]) -> str:
    if market_status != "open_regular_hours":
        return "Wait for US regular market hours before attempting the strategy-approved paper fill."
    if isinstance(automation, dict) and (automation.get("paused") or automation.get("kill_switch_enabled")):
        return "Use /resume_auto phase c validation, confirm /auto_status is safe, then scan for a real strategy setup."
    if any(check.name == "Strategy-approved Alpaca paper order" and check.status == "PENDING" for check in checks):
        return "Run /scan 5 and /propose_top 1000 10; approve/enqueue/process only if a real strategy proposal appears."
    return "Continue idempotency, kill-switch drill, and 48-hour observation evidence capture."


def query_rows(db_path: Path, sql: str) -> list[dict[str, Any]]:
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(sql).fetchall()]


def fetch_json(url: str) -> dict[str, Any] | None:
    try:
        with urlopen(url, timeout=2.0) as response:  # noqa: S310 - local operator URL only.
            return json.loads(response.read().decode("utf-8"))
    except (OSError, URLError, TimeoutError, json.JSONDecodeError):
        return None


def parse_json(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(str(raw))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


if __name__ == "__main__":
    raise SystemExit(main())
