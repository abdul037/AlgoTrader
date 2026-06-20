"""Report deployment, learning, and paper-auto readiness blockers.

This script is intentionally read-only. It is safe to run locally or in Railway
because it never submits orders, writes rollout gates, or mutates state.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.institutional.service import ROLLOUT_GATES  # noqa: E402
from app.runtime_settings import get_settings  # noqa: E402


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


def _mask(value: str | None) -> str:
    if not value:
        return "missing"
    if len(value) <= 8:
        return "present"
    return f"present:{value[:4]}...{value[-4:]}"


def _railway_auth_status() -> Check:
    token = os.environ.get("RAILWAY_TOKEN") or os.environ.get("RAILWAY_API_TOKEN")
    if token:
        return Check("railway_auth", "pass", "Railway token is present in the process environment")
    try:
        result = subprocess.run(
            ["npx", "-y", "@railway/cli@4.27.3", "whoami"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except Exception as exc:  # noqa: BLE001
        return Check("railway_auth", "fail", f"Railway CLI auth check failed: {exc}")
    output = "\n".join(part.strip() for part in (result.stdout, result.stderr) if part.strip())
    if result.returncode == 0:
        return Check("railway_auth", "pass", output or "Railway CLI is authenticated")
    return Check("railway_auth", "fail", output or "Railway CLI is not authenticated")


def _scalar(engine: Engine, sql: str, params: dict[str, Any] | None = None) -> Any:
    with engine.connect() as connection:
        return connection.execute(text(sql), params or {}).scalar()


def _has_table(engine: Engine, table: str) -> bool:
    return table in inspect(engine).get_table_names()


def _count(engine: Engine, table: str, where: str = "", params: dict[str, Any] | None = None) -> int | None:
    if not _has_table(engine, table):
        return None
    sql = f"SELECT COUNT(*) FROM {table}"
    if where:
        sql += f" WHERE {where}"
    value = _scalar(engine, sql, params)
    return int(value or 0)


def _deployment_checks() -> list[Check]:
    settings = get_settings()
    checks = [_railway_auth_status()]
    learning_key = str(getattr(settings, "learning_openai_api_key", "") or os.environ.get("OPENAI_API_KEY", ""))
    checks.append(
        Check(
            "openai_api_key",
            "pass" if learning_key.strip() else "fail",
            f"LEARNING_OPENAI_API_KEY/OPENAI_API_KEY is {_mask(learning_key)}",
        )
    )
    database_url = str(settings.database_url or "")
    checks.append(
        Check(
            "database_url",
            "pass" if database_url.startswith("postgresql+psycopg://") else "fail",
            "DATABASE_URL uses PostgreSQL+psycopg" if database_url.startswith("postgresql+psycopg://") else "DATABASE_URL is missing or not PostgreSQL+psycopg",
        )
    )
    checks.append(
        Check(
            "paper_safety_flags",
            "pass" if settings.execution_mode == "paper" and not settings.enable_real_trading else "fail",
            f"execution_mode={settings.execution_mode}, enable_real_trading={settings.enable_real_trading}",
        )
    )
    checks.append(
        Check(
            "paper_auto_flags",
            "info",
            (
                f"PAPER_AUTO_APPROVE_PROPOSALS={settings.paper_auto_approve_proposals}, "
                f"AUTO_EXECUTION_WORKER_ENABLED={settings.auto_execution_worker_enabled}, "
                f"PAPER_AUTO_OPERATION_MODE={settings.paper_auto_operation_mode}"
            ),
        )
    )
    return checks


def _database_checks(since: str) -> list[Check]:
    settings = get_settings()
    if not str(settings.database_url or "").strip():
        return [Check("database", "fail", "DATABASE_URL is not configured")]
    engine = create_engine(str(settings.database_url))
    checks: list[Check] = []

    version_count = _count(engine, "strategy_versions")
    approved_count = _count(
        engine,
        "promotion_decisions",
        "approved = 1 AND target_stage = 'production_candidate'",
    )
    gate_count = _count(engine, "rollout_gate_evidence")
    risk_count = _count(engine, "portfolio_risk_snapshots")
    checks.extend(
        [
            Check("strategy_versions", "pass" if version_count else "fail", f"{version_count or 0} persisted strategy versions"),
            Check("approved_strategies", "pass" if approved_count else "fail", f"{approved_count or 0} production-approved strategy promotions"),
            Check("rollout_gates", "pass" if gate_count else "fail", f"{gate_count or 0} rollout gate records"),
            Check("portfolio_risk", "pass" if risk_count else "fail", f"{risk_count or 0} portfolio risk snapshots"),
        ]
    )

    stage = str(getattr(settings, "rollout_stage", "stage_1_validation"))
    required_gates = ROLLOUT_GATES.get(stage, ())
    passed_gates = _count(
        engine,
        "rollout_gate_evidence",
        "stage = :stage AND status = 'passed'",
        {"stage": stage},
    )
    checks.append(
        Check(
            "stage_gate_progress",
            "pass" if required_gates and passed_gates == len(required_gates) else "fail",
            f"{passed_gates or 0}/{len(required_gates)} required gates passed for {stage}",
        )
    )

    since_iso = _parse_since(since)
    recent_counts = {
        "scan_decisions": _count(engine, "scan_decisions", "created_at >= :since", {"since": since_iso}),
        "approvals": _count(engine, "approvals", "created_at >= :since", {"since": since_iso}),
        "execution_queue": _count(engine, "execution_queue", "created_at >= :since", {"since": since_iso}),
        "executions": _count(engine, "executions", "created_at >= :since", {"since": since_iso}),
        "learning_decisions": _count(engine, "learning_decision_snapshots"),
        "learning_reviews": _count(engine, "learning_trade_reviews"),
    }
    checks.extend(
        Check(name, "info", f"{count or 0} records" + (f" since {since_iso}" if name in {"scan_decisions", "approvals", "execution_queue", "executions"} else ""))
        for name, count in recent_counts.items()
    )
    return checks


def _parse_since(value: str) -> str:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def _print(checks: list[Check]) -> int:
    failures = [check for check in checks if check.status == "fail"]
    for check in checks:
        print(f"{check.status.upper():<4} {check.name}: {check.detail}")
    if failures:
        print()
        print("Blocked readiness items:")
        for check in failures:
            print(f"- {check.name}: {check.detail}")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Report AlgoBot deployment and paper-auto readiness.")
    parser.add_argument("--since", default="2026-06-17T00:00:00+00:00")
    args = parser.parse_args()
    checks = [*_deployment_checks(), *_database_checks(args.since)]
    return _print(checks)


if __name__ == "__main__":
    raise SystemExit(main())
