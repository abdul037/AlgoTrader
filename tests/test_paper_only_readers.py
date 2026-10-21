"""What tagged paper-only results feed (operator option B, 2026-10-21; app/performance/paper_only.py).

* the eToro mirror refuses a tagged order with a non-room reason, so paper's own room rules
  decide it;
* tagged results stay out of the strategy-health switch, the auto-demotion (and the digest and
  dashboard rows that show it), the tier-3 qualification, the Stage 3 capital record and the
  go-live readiness (its count, profit factor, R scorecard and drawdown: the Phase 4 lock reads
  it); each has an untagged control;
* they stay in the paper ledger, which copies the tag, and the live scorecard lists them.

No wall clock: readiness runs on a fixed ``now`` and the freshness check on a patched clock.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.automation.unattended import PaperAutoTradingService
from app.broker.etoro_live_mirror import PAPER_ONLY_REASON
from app.broker.etoro_live_room import ROOM_REASONS, room_verdict
from app.performance.go_live_readiness import (
    EQUITY_STATE_KEY,
    READINESS_KEY,
    compute_readiness,
    readiness_ready,
)
from app.performance.paper_only import paper_only_report
from app.performance.strategy_performance import demoted_strategy_names
from app.storage.db import Database
from app.storage.repositories import RuntimeStateRepository
from tests.conftest import make_settings
from tests.test_etoro_live_mirror import _proposal, _run
from tests.test_etoro_live_room import _live, _order, _paper_full_context, _risk

TAGS = {"paper_only": True, "paper_only_reason": "oos_only"}


def _db(tmp_path):
    db = Database(make_settings(tmp_path))
    db.initialize()
    return db


# --- the eToro mirror ------------------------------------------------------------------------


def test_a_tagged_paper_only_order_is_refused_with_a_non_room_reason(tmp_path) -> None:
    mirror, _ = _live(tmp_path)
    assert room_verdict(mirror, _order("JPM")) == []  # untagged and copyable: eToro's room decides
    tagged = _order("JPM")
    tagged.metadata.update(TAGS)
    reasons = mirror.blockers(SimpleNamespace(id=None, order=tagged, signal=None), "alpaca")
    assert reasons == [PAPER_ONLY_REASON] == ["etoro_live_paper_only"]
    assert PAPER_ONLY_REASON not in ROOM_REASONS
    room = room_verdict(mirror, tagged)
    assert room is None  # so paper's own room rules decide it
    risk = _risk(tmp_path).validate_order(tagged, _paper_full_context(), etoro_room=room)
    assert not risk.passed and risk.room_authority == "paper"
    assert "Projected gross exposure exceeds the portfolio limit" in risk.reasons
    proposal = _proposal("JPM")
    proposal.order.metadata.update(TAGS)
    assert _run(mirror, proposal) is None and mirror.client.orders == []


# --- readers keyed by strategy name ----------------------------------------------------------


def _trade(strategy, pnl, day, *, paper_only=False):
    payload = dict(TAGS) if paper_only else {}
    return SimpleNamespace(
        strategy_name=strategy,
        realized_pnl_usd=pnl,
        closed_at=f"2026-10-{day:02d}T15:00:00+00:00",
        payload=payload,
    )


def test_paper_only_trades_never_demote_a_strategy_nor_keep_one(tmp_path) -> None:
    losing_paper_only = [_trade("x", -10.0, 1 + i % 20, paper_only=True) for i in range(25)]
    winning = [_trade("x", 25.0, 1 + i, paper_only=False) for i in range(5)]
    assert demoted_strategy_names(losing_paper_only + winning, min_trades=20) == set()
    untagged = [_trade("x", -10.0, 1 + i % 20) for i in range(25)]
    assert demoted_strategy_names(untagged + winning, min_trades=20) == {"x"}  # the control
    losing = [_trade("y", -10.0, 1 + i % 20) for i in range(20)]
    winning_paper_only = [_trade("y", 50.0, 21, paper_only=True) for _ in range(20)]
    assert demoted_strategy_names(losing + winning_paper_only, min_trades=20) == {"y"}


def test_the_digest_and_dashboard_verdict_rows_read_the_trades_the_demotion_reads(tmp_path) -> None:
    from app.dashboard.routes import _strategy_performance
    from app.workflow.operations import _daily_strategy_scorecard_lines

    trades = [_trade("x", -10.0, 1, paper_only=True)] * 3 + [_trade("x", 25.0, 2)]
    repo = SimpleNamespace(list=lambda limit=2000: trades)
    rows = _strategy_performance(
        SimpleNamespace(
            paper_trade_repository=repo, backtest_repository=None, settings=make_settings(tmp_path)
        )
    )
    assert [(r["strategy_name"], r["trades"]) for r in rows] == [("x", 1)]
    service = SimpleNamespace(
        paper_trading=SimpleNamespace(trades=repo),
        market_screener=None,
        settings=make_settings(tmp_path),
    )
    lines = _daily_strategy_scorecard_lines(service)
    assert len(lines) == 2 and "(1 tr," in lines[1]


class _Health:
    def __init__(self):
        self.rows = []

    def upsert_strategy_health(self, **row):
        self.rows.append(row)


def _closed_execution(strategy, pnl, *, paper_only):
    metadata = {"timeframe": "1d", **(TAGS if paper_only else {})}
    return SimpleNamespace(
        request_payload={"strategy_name": strategy, "metadata": metadata},
        response_payload={"broker_execution": {"legs": [{"status": "filled"}]}},
        status="filled",
        realized_pnl_usd=pnl,
    )


def test_paper_only_trades_never_switch_a_strategy_off_by_health(tmp_path) -> None:
    def health(executions):
        safety = _Health()
        service = PaperAutoTradingService(
            settings=make_settings(tmp_path),
            proposal_service=None,
            execution_coordinator=None,
            automation=None,
            reconciliation=None,
            safety_state=safety,
            executions=SimpleNamespace(list=lambda limit=2000: executions),
            run_logs=None,
            notifier=None,
            alpaca_client=None,
        )
        service.refresh_strategy_health()
        return {row["strategy_name"]: (row["active"], row["closed_trades"]) for row in safety.rows}

    tagged = [_closed_execution("x", -10.0, paper_only=True) for _ in range(25)]
    normal = [_closed_execution("x", 30.0, paper_only=False) for _ in range(3)]
    assert health(tagged + normal) == {"x": (True, 3)}
    untagged = [_closed_execution("x", -10.0, paper_only=False) for _ in range(25)]
    assert health(untagged) == {"x": (False, 25)}  # the control: the same losses switch it off


def test_paper_only_trades_never_qualify_a_strategy_for_tier3(tmp_path) -> None:
    from app.automation.reliability import _strategy_evidence_blockers

    def lifecycle(paper_only):
        flags = SimpleNamespace(
            entry_submitted=True,
            entry_filled=True,
            bracket_legs_verified=True,
            exit_filled_or_position_flat=True,
            reconciled=True,
            review_created=True,
            duplicate_order_absent=True,
        )
        request = {"metadata": dict(TAGS) if paper_only else {}}
        return SimpleNamespace(
            strategy_name="x",
            autonomous=True,
            flags=flags,
            realized_pnl_usd=20.0,
            execution=SimpleNamespace(payload={"request": request}),
        )

    settings = make_settings(tmp_path)
    candidate = SimpleNamespace(strategy_name="x")
    tagged = [lifecycle(True) for _ in range(30)]
    assert _strategy_evidence_blockers(
        settings=settings, candidate=candidate, lifecycles=tagged
    ) == ["insufficient_strategy_closed_trade_evidence"]
    normal = [lifecycle(False) for _ in range(30)]
    assert (
        _strategy_evidence_blockers(settings=settings, candidate=candidate, lifecycles=normal) == []
    )


def test_paper_only_trades_stay_out_of_the_stage3_capital_record() -> None:
    from app.performance.stage3 import assess_stage3, track_record_report

    trades = [_trade("x", 500.0, 1 + i % 28, paper_only=True) for i in range(100)]
    trades += [_trade("x", -20.0, 1), _trade("x", 10.0, 2)]
    readiness = assess_stage3(trades, capital_usd=100_000)
    record = track_record_report(trades, capital_usd=100_000)
    assert (readiness.total_trades, readiness.realized_pnl_usd) == (2, -10.0)
    assert (record.total_trades, record.realized_pnl_usd) == (2, -10.0)


# --- Phase 3 go-live readiness (the Phase 4 lock reads it) -----------------------------------

START = datetime(2026, 10, 5, tzinfo=UTC)  # a Monday
NOW = START + timedelta(weeks=5, days=1)
PHASE3 = SimpleNamespace(go_live_phase3_start_date="2026-10-05")


def _execution(db, ident, when, pnl, *, paper_only):
    request = {
        "symbol": "UBER",
        "strategy_name": "momentum_breakout",
        "stop_loss": 99.0,
        "metadata": {"timeframe": "1d", **(TAGS if paper_only else {})},
    }
    response = {"broker_execution": {"filled_avg_price": 100.0, "filled_qty": 100.0}}
    with db.connect() as c:
        c.execute(
            "INSERT INTO executions (id, proposal_id, status, mode, request_json, response_json, realized_pnl_usd, created_at, updated_at) "
            "VALUES (?, 'p', 'filled', 'alpaca_paper', ?, ?, ?, ?, ?)",
            (
                ident,
                json.dumps(request),
                json.dumps(response),
                pnl,
                when.isoformat(),
                when.isoformat(),
            ),
        )


def _reading(db, when, equity):
    with db.connect() as c:
        c.execute(
            "INSERT INTO run_logs (event_type, payload_json, created_at) VALUES (?, ?, ?)",
            (
                "alpaca_reconciliation_ok",
                json.dumps({"account": {"equity": equity}}),
                when.isoformat(),
            ),
        )


def _position(
    db,
    ident,
    opened,
    *,
    pnl,
    status="closed",
    paper_only=True,
    entry=100.0,
    stop=95.0,
    exit_price=None,
):
    # An untagged position says so (the LIKE prefilter alone would let it through).
    payload = {"ledger_source": "alpaca_paper", **(TAGS if paper_only else {"paper_only": False})}
    realized, unrealized = (0.0, pnl) if status == "open" else (pnl, 0.0)
    with db.connect() as c:
        c.execute(
            "INSERT INTO paper_positions (id, symbol, strategy_name, timeframe, side, status, quantity, "
            "entry_price, current_price, stop_loss, opened_at, updated_at, realized_pnl_usd, "
            "unrealized_pnl_usd, payload_json) "
            "VALUES (?, 'UBER', 'momentum_breakout', '1d', 'buy', ?, 10, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ident,
                status,
                entry,
                entry if exit_price is None else exit_price,
                stop,
                opened.isoformat(),
                opened.isoformat(),
                realized,
                unrealized,
                json.dumps(payload),
            ),
        )


def _clean_book(db, *, paper_only):
    for i in range(60):  # 30 wins of +$150, 30 losses of -$100: PF 1.5
        _execution(
            db,
            f"e{i}",
            START + timedelta(hours=i * 6),
            150.0 if i % 2 else -100.0,
            paper_only=paper_only,
        )
    for i, equity in enumerate([100_000, 101_000, 100_500, 102_000]):
        _reading(db, START + timedelta(days=i), equity)


def test_paper_only_trades_count_for_no_part_of_the_go_live_bar(tmp_path, monkeypatch) -> None:
    import app.utils.time as time_module

    control = _db(tmp_path / "control")
    _clean_book(control, paper_only=False)
    ready = compute_readiness(control, PHASE3, RuntimeStateRepository(control), now=NOW)
    assert ready["ready"] is True and ready["paper_only_trades_excluded"] == 0

    db = _db(tmp_path / "tagged")
    _clean_book(db, paper_only=True)
    for i in range(3):  # three eToro-copyable losers
        _execution(db, f"n{i}", START + timedelta(days=20, hours=i), -50.0, paper_only=False)
    state = RuntimeStateRepository(db)
    report = compute_readiness(db, PHASE3, state, now=NOW)
    assert report["paper_only_trades_excluded"] == 60
    assert (report["closed_trades"], report["profit_factor"], report["net_pnl_usd"]) == (
        3,
        0.0,
        -150.0,
    )
    assert report["blockers"][:2] == ["closed_trades_3_below_50", "profit_factor_0.00_below_1.3"]
    assert report["scorecard"] == {
        "momentum_breakout:1d": {"trades": 3, "wins": 0, "pnl": -150.0, "avg_r": -0.5}
    }
    # The Phase 4 live lock reads this report: not ready.
    monkeypatch.setattr(time_module, "utc_now", lambda: NOW)
    state.set(READINESS_KEY, json.dumps(report))
    assert readiness_ready(state) is False
    state.set(READINESS_KEY, json.dumps(ready))
    assert readiness_ready(state) is True


def _drawdown(db, readings, positions=()):
    for day, equity in enumerate(readings):
        _reading(db, START + timedelta(days=day, hours=15), equity)
    for ident, (after_day, pnl, status, tagged) in enumerate(positions):
        when = START + timedelta(days=after_day, hours=16)
        _position(db, f"pp{ident}", when, pnl=pnl, status=status, paper_only=tagged)
    report = compute_readiness(db, PHASE3, RuntimeStateRepository(db), now=NOW)
    blocked = any(b.startswith("max_drawdown_") for b in report["blockers"])
    return report["max_drawdown_pct"], blocked, report


def test_the_drawdown_reads_equity_net_of_paper_only_pnl(tmp_path) -> None:
    # A +$1k paper-only win lifts the peak to $101k, then $3k of eToro-copyable losses.
    counted = _drawdown(
        _db(tmp_path / "a"), [100_000, 101_000, 98_000], [(0, 1_000.0, "closed", False)]
    )
    assert counted[:2] == (round(3_000 / 101_000 * 100, 4), False)  # 2.97%: an untagged win counts
    pct, blocked, report = _drawdown(
        _db(tmp_path / "b"), [100_000, 101_000, 98_000], [(0, 1_000.0, "closed", True)]
    )
    assert (pct, blocked, report["paper_only_pnl_netted_usd"]) == (3.0, True, 1_000.0)
    # A paper-only win inside a dip cannot hide it: 100k -> 98.4k -> (+1.6k) 100k -> 98.4k.
    inside = _drawdown(
        _db(tmp_path / "c"), [100_000, 98_400, 100_000, 98_400], [(1, 1_600.0, "closed", True)]
    )
    assert inside[:2] == (3.2, True)
    # A paper-only loss does not count either, and an open one is netted at its mark.
    assert _drawdown(_db(tmp_path / "d"), [100_000, 96_500], [(0, -3_500.0, "closed", True)])[
        :2
    ] == (0.0, False)
    assert _drawdown(_db(tmp_path / "e"), [100_000, 101_000, 98_000], [(0, 1_000.0, "open", True)])[
        :2
    ] == (3.0, True)


def test_an_old_watermark_is_kept_only_while_no_paper_only_position_preceded_it(tmp_path) -> None:
    old = {
        "since": START.isoformat(),
        "last_ts": (START + timedelta(days=1, hours=15)).isoformat(),
        "peak": 101_000.0,
        "max_dd_pct": 0.5,
    }
    kept = _db(tmp_path / "kept")
    RuntimeStateRepository(kept).set(EQUITY_STATE_KEY, json.dumps(old))
    assert _drawdown(kept, [100_000, 101_000])[0] == 0.5  # the same number: nothing to net
    rebuilt = _db(tmp_path / "rebuilt")
    RuntimeStateRepository(rebuilt).set(EQUITY_STATE_KEY, json.dumps(old))
    assert _drawdown(rebuilt, [100_000, 101_000], [(0, 1_000.0, "closed", True)])[0] == 0.0


# --- the paper ledger keeps them, tagged -------------------------------------------------------


class _Rows:
    def __init__(self):
        self.created = []

    def create(self, record):
        self.created.append(record)

    def update(self, record):
        pass


class _Logs:
    def log(self, *_args, **_kwargs):
        pass


def test_both_paper_ledgers_copy_the_tag_into_the_position_and_its_closed_trade(tmp_path) -> None:
    from app.models.paper import PaperBrokerExecutionRecord
    from app.models.trade import TradeOrder
    from app.paper.broker_ledger import _open_position
    from app.paper.service import PaperTradingService

    service = PaperTradingService(
        settings=make_settings(tmp_path), positions=_Rows(), trades=_Rows(), run_logs=_Logs()
    )
    for metadata, expected in ((dict(TAGS), TAGS), ({}, {})):
        order = TradeOrder(
            symbol="UBER",
            amount_usd=1_000,
            proposed_price=100.0,
            stop_loss=95.0,
            take_profit=110.0,
            strategy_name="momentum_breakout",
            metadata={"timeframe": "1d", **metadata},
        )
        quote = SimpleNamespace(last_execution=100.0, ask=None, bid=None, timestamp=None)
        position = service.open_from_approved_proposal(
            SimpleNamespace(id="p", order=order, signal=None), live_quote=quote
        )
        trade = service._close_position(position, exit_price=110.0, outcome="target_1")
        tags = {k: v for k, v in trade.payload.items() if k.startswith("paper_only")}
        assert (
            {k: v for k, v in position.payload.items() if k.startswith("paper_only")}
            == tags
            == expected
        )

    ledger = SimpleNamespace(positions=_Rows(), logs=_Logs())
    execution = PaperBrokerExecutionRecord(
        execution_id="ex1",
        proposal_id="p",
        symbol="UBER",
        mode="alpaca_paper",
        status="filled",
        filled_qty=10.0,
        entry_fill_price=100.0,
        created_at=START.isoformat(),
        updated_at=START.isoformat(),
        payload={"request": {"stop_loss": 95.0, "metadata": {"timeframe": "1d", **TAGS}}},
    )
    assert {
        k: v
        for k, v in _open_position(ledger, execution).payload.items()
        if k.startswith("paper_only")
    } == TAGS


# --- reporting -------------------------------------------------------------------------------


def test_the_live_scorecard_lists_paper_only_entries_on_their_own(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app(settings=make_settings(tmp_path), enable_background_jobs=False)
    db = app.state.db
    _position(db, "a", START, pnl=100.0, entry=100.0, stop=95.0, exit_price=110.0)  # +2 R
    _position(db, "b", START, pnl=-50.0, entry=100.0, stop=95.0, exit_price=95.0)  # -1 R
    _position(db, "c", START, pnl=0.0, status="open")
    _position(db, "d", START, pnl=500.0, paper_only=False, exit_price=150.0)  # eToro-copyable
    expected = {
        "entries": 3,
        "open": 1,
        "closed": 2,
        "by_strategy": {
            "momentum_breakout:1d": {"entries": 3, "open": 1, "closed": 2, "mean_r": 0.5}
        },
    }
    assert paper_only_report(db) == expected
    assert TestClient(app).get("/performance/live-scorecard").json()["paper_only"] == expected
