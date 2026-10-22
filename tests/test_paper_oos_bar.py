"""Paper OOS bar (operator option B, 2026-10-21): the verdicts and the gates.

* every verdict carries ``paper_oos_passed``: its fee-inclusive pooled OOS part on its own
  (>= 40 OOS trades, > 0 and any R bar; the holdout ignored), and for a minute key both venue
  profiles' OOS parts too;
* the main paper gate admits such a key while ``paper_oos_bar_enabled`` is on; off, or a
  verdict without the field, is the old gate; crypto keeps the old gate;
* the eToro mirror, its +0.03R bar, the 24 h streak, Phase 3 and the deep-scan hints read
  ``passed`` alone (tests/test_paper_only_readers.py: what tagged paper-only results feed).

No wall clock: verdict caches carry no timestamp or a patched one.
"""

from __future__ import annotations

import ast
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.automation.unattended import PaperAutoTradingService
from app.broker.etoro_live_mirror import _StrictEvidence
from app.performance import strategy_evidence
from app.performance.strategy_evidence import (
    VERDICTS_KEY,
    compute_verdicts,
    evidence_blocker,
    paper_oos_only,
    passing_hours,
    refresh_verdicts,
)
from app.storage.db import Database
from app.storage.repositories import RuntimeStateRepository
from tests.conftest import make_settings
from tests.test_etoro_live_mirror import _run, verdicts_cache
from tests.test_intraday_paper_evidence import _row

LACKS = "strategy_lacks_oos_evidence"
ON = SimpleNamespace(require_strategy_oos_evidence=True, paper_oos_bar_enabled=True)
OFF = SimpleNamespace(require_strategy_oos_evidence=True, paper_oos_bar_enabled=False)
ABSENT = SimpleNamespace(require_strategy_oos_evidence=True)  # a settings view without it


def _db(tmp_path):
    db = Database(make_settings(tmp_path))
    db.initialize()
    return db


def _pool(db, strategy, tf, *, trades=(20, 20, 20), oos, holdout_trades=5, holdout, profiles=None):
    for index, count in enumerate(trades):
        _row(
            db,
            symbol=f"S{index}",
            strategy=strategy,
            tf=tf,
            trades=count,
            expectancy=oos,
            holdout_trades=holdout_trades,
            holdout_expectancy=holdout,
            profiles=profiles,
        )


def _verdict_db(tmp_path):
    db = _db(tmp_path)
    # 60 OOS trades at +0.068 R (fee-inclusive), the 15 holdout trades lose: production's case.
    _pool(db, "momentum_breakout", "1d", oos=6.8, holdout=-1.0)
    _pool(db, "ma_crossover", "1d", oos=4.5, holdout_trades=2, holdout=3.0)  # 6 holdout trades
    _pool(db, "trend_following", "1d", oos=-0.5, holdout=2.0)  # the OOS part loses
    _pool(db, "rsi_reversal", "1d", trades=(13, 13, 13), oos=5.0, holdout=-1.0)  # 39 trades
    _pool(db, "relative_strength_momentum", "1d", trades=(13, 13, 14), oos=3.4, holdout=-1.0)
    _pool(db, "inside_bar_narrow_range_breakout", "1d", oos=4.0, holdout=2.0)  # passes in full
    # Minute keys: the fee-inclusive OOS part wins, every holdout loses.
    _pool(
        db,
        "vwap_reclaim",
        "5m",
        oos=3.0,
        holdout=-1.0,
        profiles={"alpaca": (6.0, -1.0), "stress": (1.5, -0.5)},
    )
    _pool(
        db,
        "rsi_vwap_ema_confluence",
        "5m",
        oos=3.0,
        holdout=-1.0,
        profiles={"alpaca": (6.0, -1.0), "stress": (-0.5, -0.5)},
    )
    _pool(db, "ema_trend_stack", "10m", oos=3.0, holdout=-1.0)  # no cost-profile rows
    # Fee-band: loses with eToro's fees, wins on the venue profiles.
    _pool(
        db,
        "intraday_vwap_trend",
        "15m",
        oos=-2.0,
        holdout=1.0,
        profiles={"alpaca": (6.0, 3.0), "stress": (1.5, 0.5)},
    )
    return db


def test_paper_oos_passed_is_the_fee_inclusive_pooled_oos_part_alone(tmp_path) -> None:
    verdicts = compute_verdicts(_verdict_db(tmp_path), SimpleNamespace())

    def view(key):
        v = verdicts[key]
        return v["passed"], v["reasons"], v["paper_oos_passed"]

    assert view("momentum_breakout:1d") == (False, ["holdout_expectancy_not_positive"], True)
    assert verdicts["momentum_breakout:1d"]["oos_expectancy_r"] == 0.068
    assert view("ma_crossover:1d") == (False, ["too_few_holdout_trades"], True)
    assert view("trend_following:1d") == (False, ["oos_expectancy_not_positive"], False)
    assert view("rsi_reversal:1d")[2] is False  # 39 < 40 OOS trades
    assert view("relative_strength_momentum:1d")[2] is True  # exactly 40
    assert view("inside_bar_narrow_range_breakout:1d") == (True, [], True)
    # Minute keys: Stage 1 holds on this path too (both profiles' OOS parts, holdouts ignored).
    fivem = verdicts["vwap_reclaim:5m"]
    assert (fivem["passed"], fivem["paper_passed"], fivem["paper_oos_passed"]) == (
        False,
        False,
        True,
    )
    assert verdicts["rsi_vwap_ema_confluence:5m"]["paper_oos_passed"] is False  # stress OOS loses
    assert verdicts["ema_trend_stack:10m"]["paper_oos_passed"] is False  # no profile rows
    assert verdicts["intraday_vwap_trend:15m"]["paper_oos_passed"] is False  # eToro's fees count
    # The R bar is part of the OOS judgement: +0.068 R misses a 0.07 R bar.
    strict = compute_verdicts(
        _verdict_db(tmp_path / "r"), SimpleNamespace(strategy_evidence_min_expectancy_r=0.07)
    )
    assert strict["momentum_breakout:1d"]["paper_oos_passed"] is False
    assert strict["ma_crossover:1d"]["paper_oos_passed"] is False  # +0.045 R


def test_the_main_paper_gate_admits_the_oos_only_keys_and_every_other_caller_does_not(
    tmp_path,
) -> None:
    db = _verdict_db(tmp_path)
    state = RuntimeStateRepository(db)
    refresh_verdicts(db, SimpleNamespace(), state)

    def gate(settings, key, **kwargs):
        strategy, timeframe = key.rsplit(":", 1)
        return evidence_blocker(settings, state, strategy=strategy, timeframe=timeframe, **kwargs)

    for key in (
        "momentum_breakout:1d",
        "ma_crossover:1d",
        "relative_strength_momentum:1d",
        "vwap_reclaim:5m",
    ):
        assert gate(ON, key, paper=True) is None, key
        assert gate(OFF, key, paper=True) == LACKS  # off: exactly the old gate
        assert gate(ABSENT, key, paper=True) == LACKS  # a settings view without the switch: off
        assert gate(ON, key) == LACKS  # the eToro mirror, the hints, the shadows: `passed`
        assert gate(ON, key, min_expectancy_r=0.03) == LACKS  # the copy's +0.03 R bar
    for key in (
        "trend_following:1d",
        "rsi_reversal:1d",
        "rsi_vwap_ema_confluence:5m",
        "ema_trend_stack:10m",
        "intraday_vwap_trend:15m",
    ):
        assert gate(ON, key, paper=True) == LACKS, key
    full = "inside_bar_narrow_range_breakout:1d"
    assert gate(ON, full, paper=True) is None and gate(OFF, full, paper=True) is None
    assert gate(ON, full) is None
    # The streak the live copy needs counts `passed` refreshes only.
    payload = json.loads(state.get(VERDICTS_KEY))
    assert sorted(payload["passing_since"]) == [full]
    assert passing_hours(payload, strategy="momentum_breakout", timeframe="1d") == 0.0
    # The eToro mirror's own settings view (Phase 3 shadow and deep scan use it too).
    strict = _StrictEvidence(make_settings(tmp_path))
    assert strict.paper_oos_bar_enabled is True  # the operator's switch is on...
    assert evidence_blocker(strict, state, strategy="momentum_breakout", timeframe="1d") == LACKS


def test_a_verdict_without_the_field_fails_closed_and_staleness_still_applies(monkeypatch) -> None:
    def gate(verdict, computed_at=None):
        payload = {"verdicts": {"s:1d": verdict}}
        if computed_at is not None:
            payload["computed_at"] = computed_at
        state = SimpleNamespace(get={VERDICTS_KEY: json.dumps(payload)}.get)
        return evidence_blocker(ON, state, strategy="s", timeframe="1d", paper=True)

    assert gate({"passed": False}) == LACKS  # written before the field existed
    assert gate({"passed": False, "paper_oos_passed": False}) == LACKS
    assert gate({"passed": False, "paper_oos_passed": True}) is None
    now = datetime(2026, 10, 21, 15, 0, tzinfo=UTC)
    monkeypatch.setattr(strategy_evidence, "utc_now", lambda: now)
    day_old = (now - timedelta(hours=25)).isoformat()
    assert gate({"passed": False, "paper_oos_passed": True}, day_old) == "strategy_evidence_stale"
    assert not paper_oos_only(ON, None, "1d") and not paper_oos_only(ON, ["x"], "1d")


def test_crypto_keeps_the_old_gate_on_the_main_account(tmp_path) -> None:
    verdicts = {"momentum_breakout:1d": {"passed": False, "paper_oos_passed": True}}
    service = PaperAutoTradingService(
        settings=make_settings(tmp_path, require_strategy_oos_evidence=True),
        proposal_service=None,
        execution_coordinator=None,
        automation=None,
        reconciliation=None,
        safety_state=SimpleNamespace(
            is_blacklisted=lambda _s: False, strategy_active=lambda _s: True
        ),
        executions=None,
        run_logs=None,
        notifier=None,
        alpaca_client=None,
        runtime_state=SimpleNamespace(get={VERDICTS_KEY: json.dumps({"verdicts": verdicts})}.get),
    )
    candidate = SimpleNamespace(symbol="BTCUSD", strategy_name="momentum_breakout", timeframe="1d")
    assert service.candidate_proposal_blockers(candidate) == [LACKS]


def test_only_the_main_paper_gate_asks_for_the_paper_path() -> None:
    # Every other evidence_blocker caller (the eToro mirror, its room check, the Phase 3 shadow,
    # the deep-scan hints) keeps reading `passed` alone: none passes `paper`.
    app = Path(__file__).resolve().parent.parent / "app"
    callers: dict[str, set[str]] = {}
    for path in app.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) in {
                "evidence_blocker",
                "paper_oos_only",
            }:
                kind = node.func.id + (
                    "(paper=)" if any(k.arg == "paper" for k in node.keywords) else ""
                )
                callers.setdefault(kind, set()).add(path.relative_to(app.parent).as_posix())
    assert callers["evidence_blocker(paper=)"] == {"app/automation/unattended.py"}
    assert callers["paper_oos_only"] == {
        "app/workflow/paper_only.py",
        "app/workflow/swing_focus.py",
    }


# --- the eToro mirror ------------------------------------------------------------------------


def test_the_mirror_refuses_an_oos_only_strategy_on_its_evidence_check(tmp_path) -> None:
    from tests.test_etoro_live_mirror import _mirror, _state

    state = _state()
    verdict = {"passed": False, "paper_oos_passed": True, "oos_expectancy_r": 0.068}
    state.set(VERDICTS_KEY, verdicts_cache({"momentum_breakout:1d": verdict}))
    mirror, logs = _mirror(tmp_path, state=state)
    assert _run(mirror) is None and mirror.client.orders == []
    reasons = logs.events[-1][1]["reasons"]
    assert LACKS in reasons and "etoro_live_evidence_streak_too_short" in reasons


def test_the_boot_snapshot_shows_the_switch(tmp_path) -> None:
    from app.automation.policy_snapshot import effective_execution_policy

    assert effective_execution_policy(make_settings(tmp_path))["paper_oos_bar_enabled"] is True
    off = make_settings(tmp_path, paper_oos_bar_enabled=False)
    assert effective_execution_policy(off)["paper_oos_bar_enabled"] is False
    assert effective_execution_policy(SimpleNamespace())["paper_oos_bar_enabled"] is False


@pytest.mark.parametrize("enabled", [True, False])
def test_the_setting_defaults_on_and_off_is_the_old_gate(tmp_path, enabled) -> None:
    settings = make_settings(
        tmp_path,
        require_strategy_oos_evidence=True,
        **({} if enabled else {"paper_oos_bar_enabled": False}),
    )
    state = SimpleNamespace(
        get={
            VERDICTS_KEY: json.dumps(
                {"verdicts": {"m:1d": {"passed": False, "paper_oos_passed": True}}}
            )
        }.get
    )
    result = evidence_blocker(settings, state, strategy="m", timeframe="1d", paper=True)
    assert result == (None if enabled else LACKS)
