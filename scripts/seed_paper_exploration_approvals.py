"""Seed paper-exploration approvals for registered scanner strategies."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import AppSettings
from app.models.institutional import PromotionDecision, StrategyVersion
from app.storage.db import Database
from app.storage.repositories import StrategyGovernanceRepository
from app.strategies import STRATEGY_REGISTRY


def _code_version() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        return "runtime-current"


def seed() -> list[str]:
    settings = AppSettings()
    db = Database(settings)
    db.initialize()
    repository = StrategyGovernanceRepository(db)
    created: list[str] = []
    code_version = _code_version()

    for strategy_name in sorted(STRATEGY_REGISTRY):
        if repository.strategy_paper_exploration_approved(strategy_name):
            continue
        version = repository.create_version(
            StrategyVersion(
                strategy_name=strategy_name,
                code_version=code_version,
                parameters={"source": "registered_scanner_strategy"},
                dataset_version="paper-exploration-user-approved-2026-06-24",
                timeframe="multi",
                status="paper_exploration",
            )
        )
        repository.record_decision(
            PromotionDecision(
                strategy_version_id=version.id,
                target_stage="paper_exploration",
                approved=True,
                blockers=[],
                evidence={
                    "scope": "alpaca_paper_exploration_only",
                    "production_approval": False,
                    "live_trading": False,
                    "notes": (
                        "User approved existing scanner strategies for paper exploration. "
                        "This does not qualify the strategy for production or live trading."
                    ),
                },
                decided_by="user_requested_paper_exploration",
            )
        )
        created.append(strategy_name)
    return created


def main() -> int:
    created = seed()
    if created:
        print("Created paper-exploration approvals: " + ", ".join(created))
    else:
        print("Paper-exploration approvals already present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
