from __future__ import annotations

from app.ledger.repository import LedgerRepository
from app.storage.db import Database
from tests.conftest import make_settings


def test_strategy_audit_groups_outcomes_and_recommends_from_closed_data(tmp_path) -> None:
    db = Database(make_settings(tmp_path))
    db.initialize()
    repo = LedgerRepository(db)

    winner_id = repo.insert_outcome(
        alert_source="test",
        alert_id="a1",
        symbol="NVDA",
        strategy_name="rsi_vwap_ema_confluence",
        timeframe="1d",
        alert_created_at="2026-04-01T00:00:00+00:00",
        alert_entry_price=100.0,
        alert_stop=95.0,
        alert_target=110.0,
        alert_score=92.0,
    )
    repo.mark_closed(
        winner_id,
        closed_at="2026-04-02T00:00:00+00:00",
        close_rate=110.0,
        realized_pnl_usd=100.0,
        realized_r_multiple=2.0,
        outcome_status="target_hit",
    )

    loser_id = repo.insert_outcome(
        alert_source="test",
        alert_id="a2",
        symbol="AMD",
        strategy_name="rsi_vwap_ema_confluence",
        timeframe="1h",
        alert_created_at="2026-04-03T00:00:00+00:00",
        alert_entry_price=100.0,
        alert_stop=95.0,
        alert_target=110.0,
        alert_score=76.0,
    )
    repo.mark_closed(
        loser_id,
        closed_at="2026-04-03T03:00:00+00:00",
        close_rate=95.0,
        realized_pnl_usd=-50.0,
        realized_r_multiple=-1.0,
        outcome_status="stop_hit",
    )

    repo.insert_outcome(
        alert_source="test",
        alert_id="a3",
        symbol="MU",
        strategy_name="rsi_vwap_ema_confluence",
        timeframe="15m",
        alert_created_at="2026-04-04T00:00:00+00:00",
        alert_entry_price=100.0,
        alert_stop=95.0,
        alert_target=110.0,
        alert_score=None,
    )

    audit = repo.strategy_audit(min_closed=2)

    assert audit["overall"]["closed_count"] == 2
    assert audit["overall"]["win_rate"] == 0.5
    assert audit["overall"]["profit_factor"] == 2.0
    assert audit["overall"]["recommendation"] == "reduce"
    strategy = audit["by_strategy"][0]
    assert strategy["name"] == "rsi_vwap_ema_confluence"
    assert strategy["total_alerts"] == 3
    assert strategy["pending_match"] == 1
    buckets = {item["name"]: item for item in audit["by_score_bucket"]}
    assert buckets["90+"]["closed_count"] == 1
    assert buckets["70-80"]["closed_count"] == 1
    assert buckets["no_score"]["pending_match"] == 1
