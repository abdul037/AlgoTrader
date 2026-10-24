"""Parity: with the learning account configured and trading, every main-account and eToro
decision equals d2c7faf's (the deployed head) without it.

``tests/learning_account_parity_golden.json`` was captured from a fresh ``git archive d2c7faf``
tree with this same driver (tests/learning_account_parity.py, copied in unchanged; no learning
account exists there): ``python -m tests.learning_account_parity <out>``. It holds the proposals
and approvals, the execution queue, the executions, the scan decisions, the paper tables, the
tracked signals, the learning (ML) tables, runtime_state, run_logs, the main orders, every
``run_scan_task`` result and every call on the main Alpaca client. Here the scenario runs with
the learning account configured and its thread's passes between the phases; less only the
learning account's own keys and its ``learning_account``-tagged run logs, everything must equal
the golden. The main client's extra calls are exactly one ``get_quote`` per learning entry (its
read-only market data), made only inside the learning passes.

Re-capture only from the deployed head, with the driver copied in unchanged, and say so.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.learning_account_parity import CANDIDATES, run_scenario

GOLDEN = Path(__file__).with_name("learning_account_parity_golden.json")


@pytest.fixture(scope="module")
def golden():
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def with_learning(tmp_path_factory):
    return run_scenario(tmp_path_factory.mktemp("on"), learning=True)


def test_main_and_etoro_decisions_with_the_learning_account_equal_d2c7faf(golden, with_learning) -> None:
    result = dict(with_learning)
    result.pop("learning")
    moved = sorted(key for key in set(golden) | set(result) if golden.get(key) != result.get(key))
    assert not moved, f"main-path records moved with the learning account: {moved}"
    assert result == golden


def test_without_the_learning_account_this_tree_equals_d2c7faf(golden, tmp_path) -> None:
    assert run_scenario(tmp_path, learning=False) == golden


def test_the_scenario_is_not_vacuous(golden, with_learning) -> None:
    logs = golden["run_logs"]
    blocked = {
        (row["payload"]["symbol"], tuple(row["payload"]["blockers"]))
        for row in logs
        if row["event_type"] == "auto_proposal_safety_blocked"
    }
    # Option B's guards held one back (eToro holds AMD) and its pass limit another (XOM).
    assert ("AMD", ("paper_only_etoro_live_symbol_held",)) in blocked
    assert ("XOM", ("paper_only_pass_limit",)) in blocked
    for symbol in ("META", "MSFT", "TSLA", "ORCL", "AVGO"):
        assert (symbol, ("strategy_lacks_oos_evidence",)) in blocked
    orders = [order["symbol"] for order in golden["main_orders"]]
    assert orders == ["NVDA", "JPM", "UNH", "XOM"]  # the main entry and three paper-only ones
    queued = [row for row in golden["approvals"] if "paper_only" in row.get("payload_json", "") or "paper_only" in json.dumps(row)]
    assert queued, "no paper-only proposal reached the queue"
    assert any(row["event_type"] == "kill_switch_emergency_stop" for row in logs)
    verdicts = json.loads(next(r["state_value"] for r in golden["runtime_state"] if r["state_key"] == "strategy_evidence:verdicts"))
    oos_only = sorted(key for key, v in verdicts["verdicts"].items() if v["paper_oos_passed"] and not v["passed"])
    assert oos_only == ["ema_trend_stack:1h", "momentum_breakout:1d", "vwap_reclaim:5m"]
    assert verdicts["verdicts"]["rsi_reversal:15m"]["passed"] and not verdicts["verdicts"]["rsi_reversal:15m"]["paper_passed"]
    assert {s for group in CANDIDATES.values() for _, _, s, _ in group} >= {"NVDA", "AMD", "JPM", "XOM", "UNH"}


def test_the_learning_account_traded_only_what_the_main_path_refused_for_evidence(with_learning) -> None:
    learning = with_learning["learning"]
    trades = sorted((t["symbol"], t["strategy_name"], t["timeframe"], t["kind"]) for t in learning["book"]["trades"].values())
    assert trades == [
        ("META", "ma_crossover", "1d", "fee_band"),
        ("MSFT", "trend_following", "1d", "lane"),
        ("ORCL", "rsi_reversal", "15m", "fee_band"),
    ]
    sent = [call[1] for call in learning["broker_calls"] if call[0] == "submit_bracket"]
    assert sent == ["META", "MSFT", "ORCL"]
    assert not [call for call in learning["broker_calls"] if call[0] in {"submit_close", "cancel"}]  # never flattened
    # The main client: exactly one quote per learning entry, inside the learning passes only.
    assert learning["extra_main_calls"] == [["get_quote", "META"], ["get_quote", "MSFT"], ["get_quote", "ORCL"]]
    skipped = dict((reason, symbol) for reason, symbol in learning["skipped"])
    assert skipped["passes_the_main_gate"] == "NVDA"
    assert skipped["passes_the_paper_oos_bar"] == "AMD"  # B's, though B's guard held it back
    assert "automation_kill_switch_enabled" in skipped  # the main kill switch stops its entries
    held = {"NVDA", "AMD", "JPM", "XOM", "UNH"}  # the main path's (and option B's) symbols
    assert not held & {t["symbol"] for t in learning["book"]["trades"].values()}
