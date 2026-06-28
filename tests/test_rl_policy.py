from __future__ import annotations

from types import SimpleNamespace

from app.live_signal_schema import LiveSignalSnapshot, SignalState
from app.models.approval import TradeProposal
from app.models.rl_policy import RLPolicyVersion
from app.models.screener import ScanDecisionRecord
from app.models.strategy_lab import StrategyLabBacktestRecord
from app.rl_policy.service import RLPolicyService
from app.storage.db import Database
from app.storage.repositories import RLPolicyRepository
from app.utils.time import utc_now
from tests.conftest import make_settings


class FakeScanDecisions:
    def __init__(self, rows: list[ScanDecisionRecord]):
        self.rows = rows

    def list(self, *, limit: int = 100, **_kwargs):
        return self.rows[:limit]


class FakeStrategyLabRepository:
    def __init__(self, backtests: dict[str, StrategyLabBacktestRecord] | None = None):
        self.backtests = backtests or {}

    def list_generated(self, *, limit: int = 1000):
        return [SimpleNamespace(id=generated_id) for generated_id in list(self.backtests)[:limit]]

    def latest_backtest(self, generated_id: str):
        return self.backtests.get(generated_id)


class FakeGovernance:
    def __init__(self, approved: set[str]):
        self.approved = approved

    def strategy_paper_exploration_approved(self, strategy_name: str) -> bool:
        return strategy_name in self.approved

    def strategy_production_approved(self, _strategy_name: str) -> bool:
        return False


class FakeProposalService:
    def __init__(self):
        self.created = []

    def create_proposal(self, request):
        self.created.append(request)
        return TradeProposal(order=request.to_order(), signal=request.signal, notes=request.notes)


class FakeAutoTrading:
    def __init__(self):
        self.processed = []

    def approve_enqueue_execute(self, proposal, snapshot):
        self.processed.append((proposal, snapshot))
        return SimpleNamespace(id="queue-1", status="processed")


class FakeRunLogs:
    def __init__(self):
        self.events = []

    def log(self, event_type: str, payload: dict):
        self.events.append((event_type, payload))


def _settings(tmp_path, **overrides):
    values = {
        "rl_policy_enabled": True,
        "rl_policy_training_enabled": True,
        "rl_policy_paper_proposals_enabled": True,
        "rl_policy_max_notional_usd": 500.0,
        "rl_policy_max_proposals_per_day": 1,
        "rl_policy_min_backtest_trades": 150,
        "rl_policy_min_profit_factor": 1.20,
        "rl_policy_max_drawdown_pct": 12.0,
        "execution_mode": "paper",
        "enable_real_trading": False,
        "paper_broker": "alpaca",
        "alpaca_expected_account_number": "PA3B287XBZYU",
        "alpaca_require_bracket_orders": True,
        "max_trade_amount_usd": 1000.0,
    }
    values.update(overrides)
    return make_settings(tmp_path, **values)


def _repository(settings) -> RLPolicyRepository:
    db = Database(settings)
    db.initialize()
    return RLPolicyRepository(db)


def _snapshot(strategy_name: str = "generated_policy_test") -> LiveSignalSnapshot:
    return LiveSignalSnapshot(
        symbol="AAPL",
        strategy_name=strategy_name,
        state=SignalState.BUY,
        timeframe="1d",
        current_price=100.0,
        entry_price=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        signal_role="entry_long",
        rationale="Deterministic scanner candidate selected by RL policy",
        score=70.0,
        tradable=True,
        execution_ready=True,
        supported=True,
        asset_class="equity",
        metadata={"alert_eligible": True},
    )


def _scan_decision(snapshot: LiveSignalSnapshot, *, row_id: int = 1) -> ScanDecisionRecord:
    return ScanDecisionRecord(
        id=row_id,
        scan_task="scheduled_all",
        symbol=snapshot.symbol,
        strategy_name=snapshot.strategy_name,
        timeframe=snapshot.timeframe,
        status="candidate",
        final_score=snapshot.score,
        alert_eligible=True,
        freshness="fresh",
        reason_codes=[],
        rejection_reasons=[],
        payload=snapshot.model_dump(mode="json"),
        created_at=utc_now().isoformat(),
    )


def _service(tmp_path, *, settings=None, repository=None, scan_rows=None, backtests=None, approved=None):
    settings = settings or _settings(tmp_path)
    repository = repository or _repository(settings)
    proposal_service = FakeProposalService()
    auto_trading = FakeAutoTrading()
    service = RLPolicyService(
        settings=settings,
        repository=repository,
        scan_decisions=FakeScanDecisions(scan_rows or []),
        strategy_lab=SimpleNamespace(repository=FakeStrategyLabRepository(backtests)),
        strategy_governance=FakeGovernance(approved or set()),
        proposal_service=proposal_service,
        auto_trading=auto_trading,
        automation=SimpleNamespace(execution_blockers=lambda: []),
        reconciliation=SimpleNamespace(account_verified=lambda: True),
        safety_state=SimpleNamespace(is_blacklisted=lambda _symbol: False),
        alpaca_client=SimpleNamespace(is_regular_market_open=lambda: True),
        run_logs=FakeRunLogs(),
    )
    return service, repository, proposal_service, auto_trading


def test_rl_training_promotes_only_policy_arms_passing_gates(tmp_path) -> None:
    snapshot = _snapshot()
    backtest = StrategyLabBacktestRecord(
        generated_strategy_id="gen-1",
        status="passed",
        metrics={
            "number_of_trades": 150,
            "profit_factor": 1.25,
            "max_drawdown_pct": 7.5,
            "expectancy_usd": 1.4,
        },
        results=[{"strategy_name": snapshot.strategy_name, "timeframe": snapshot.timeframe}],
    )
    service, _repo, _proposal_service, _auto_trading = _service(
        tmp_path,
        scan_rows=[_scan_decision(snapshot)],
        backtests={"gen-1": backtest},
        approved={snapshot.strategy_name},
    )

    policy = service.train()

    assert policy.status == "paper_candidate"
    assert policy.policy["eligible_arms"] == [f"{snapshot.strategy_name}:1d"]
    assert policy.policy["constraints"]["paper_only"] is True
    assert policy.policy["constraints"]["max_notional_usd"] == 500.0


def test_rl_policy_proposal_is_capped_metadata_tagged_and_idempotent(tmp_path) -> None:
    snapshot = _snapshot()
    settings = _settings(tmp_path, max_trade_amount_usd=1000.0, rl_policy_max_notional_usd=500.0)
    repository = _repository(settings)
    repository.record_version(
        RLPolicyVersion(
            status="paper_candidate",
            dataset_version="rl-dataset-test",
            row_count=200,
            accepted_rows=20,
            policy={
                "eligible_arms": [f"{snapshot.strategy_name}:1d"],
                "arms": {f"{snapshot.strategy_name}:1d": {"reward_score": 8.0}},
            },
        )
    )
    service, _repo, proposal_service, auto_trading = _service(
        tmp_path,
        settings=settings,
        repository=repository,
        scan_rows=[_scan_decision(snapshot)],
        approved={snapshot.strategy_name},
    )

    first = service.propose()
    second = service.propose()

    assert first.status == "queued"
    assert second.id == first.id
    assert len(proposal_service.created) == 1
    assert len(auto_trading.processed) == 1
    request = proposal_service.created[0]
    assert request.amount_usd == 500.0
    assert request.stop_loss == snapshot.stop_loss
    assert request.take_profit == snapshot.take_profit
    assert request.metadata["source"] == "rl_policy"
    assert request.metadata["policy_version_id"] == first.policy_version_id
    assert request.metadata["training_dataset_version"] == "rl-dataset-test"


def test_rl_policy_blocks_live_mode_without_creating_proposal(tmp_path) -> None:
    snapshot = _snapshot()
    settings = _settings(tmp_path, execution_mode="live", enable_real_trading=True)
    repository = _repository(settings)
    repository.record_version(
        RLPolicyVersion(
            status="paper_candidate",
            dataset_version="rl-dataset-test",
            row_count=200,
            accepted_rows=20,
            policy={
                "eligible_arms": [f"{snapshot.strategy_name}:1d"],
                "arms": {f"{snapshot.strategy_name}:1d": {"reward_score": 8.0}},
            },
        )
    )
    service, _repo, proposal_service, auto_trading = _service(
        tmp_path,
        settings=settings,
        repository=repository,
        scan_rows=[_scan_decision(snapshot)],
        approved={snapshot.strategy_name},
    )

    proposal = service.propose()

    assert proposal.status == "blocked"
    assert "paper_only_policy" in proposal.blockers
    assert proposal_service.created == []
    assert auto_trading.processed == []
