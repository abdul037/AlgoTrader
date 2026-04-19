"""Create a trade proposal from the latest strategy signal in a CSV file."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.approvals.service import ProposalService
from app.broker.etoro_client import EToroClient
from app.runtime_settings import get_settings
from app.data.market_data import MarketDataService
from app.execution.trader import TraderService
from app.risk.guardrails import RiskManager
from app.storage.db import Database
from app.storage.repositories import ExecutionRepository, ProposalRepository, RunLogRepository, SignalRepository
from app.strategies import get_strategy


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create a pending trade proposal from a strategy signal.")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--strategy", required=True)
    parser.add_argument("--file", required=True, dest="file_path")
    parser.add_argument("--amount", type=float, default=None)
    parser.add_argument("--leverage", type=int, default=1)
    parser.add_argument("--notes", default="")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    settings = get_settings()
    database = Database(settings)
    database.initialize()
    broker = EToroClient(settings)
    risk_manager = RiskManager(settings)

    proposal_service = ProposalService(
        settings=settings,
        proposal_repository=ProposalRepository(database),
        signal_repository=SignalRepository(database),
        execution_repository=ExecutionRepository(database),
        run_log_repository=RunLogRepository(database),
        broker=broker,
        risk_manager=risk_manager,
    )
    trader = TraderService(
        settings=settings,
        proposal_service=proposal_service,
        execution_repository=ExecutionRepository(database),
        run_log_repository=RunLogRepository(database),
        broker=broker,
        risk_manager=risk_manager,
    )

    market_data = MarketDataService()
    strategy = get_strategy(args.strategy)
    data = market_data.load_csv(args.file_path)
    signal = strategy.generate_signal(data, args.symbol)
    if signal is None or signal.action.value != "buy":
        raise SystemExit("No current buy signal was generated from the supplied data.")

    proposal = trader.propose_from_signal(
        signal,
        amount_usd=args.amount,
        leverage=args.leverage,
        notes=args.notes,
    )
    print(json.dumps(proposal.model_dump(), indent=2))


if __name__ == "__main__":
    main()
