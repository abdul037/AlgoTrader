"""The separate paper learning account: offers, the prefilter, the main path's refusal, the
learning classification, the caps and the entry itself (app/learning_account/account.py).

Every test runs the learning thread's ``tick`` by hand on a hand-moved clock, against a real
SQLite runtime_state, a scripted learning broker and the main path's own
``PaperAutoTradingService`` (whose option B guard collaborators raise if touched)."""

from __future__ import annotations

import json
import threading
from datetime import timedelta

import pytest

from app.learning_account import account as account_module
from app.learning_account import store as records
from app.learning_account.account import (
    MAX_ENTRIES_PER_DAY,
    MAX_OPEN,
    MAX_ORDERS_PER_DAY,
    RISK_FRACTION,
    close_client_order_id,
    entry_client_order_id,
    scan_time,
    trade_id_for,
)
from app.learning_account.market import BoundedReads, MarketReads
from tests.learning_account_fakes import (
    CLOSE,
    LEARNING,
    NOW,
    VERDICTS,
    APIError,
    Clock,
    FakeAutomation,
    FakeEngine,
    FakeSafety,
    Rig,
    snapshot,
)


def test_a_fee_band_candidate_is_entered_once_at_half_size_on_the_learning_equity(tmp_path) -> None:
    rig = Rig(tmp_path)
    assert rig.offer(snapshot("NVDA")) == 1
    rig.tick()

    (trade,) = rig.book().values()
    assert trade["status"] == "submitted" and trade["kind"] == "fee_band"
    # 0.98% of $50,000 over a $5 stop = 98 shares = $9,800 (under the $10,000 cap); half: 49.
    assert trade["qty"] == 49 and trade["sizing"]["risk_fraction"] == RISK_FRACTION == 0.5
    assert trade["sizing"]["equity_usd"] == 50_000.0
    ((symbol, qty, target, stop, client_id),) = rig.broker.sent("submit_bracket")
    assert (symbol, qty, target, stop) == ("NVDA", 49, 110.0, 95.0)
    assert client_id == entry_client_order_id(trade["trade_id"]) == trade["client_order_id"]
    assert trade["submitted_at"] == NOW.isoformat()
    (entry,) = rig.logs.named("learning_account_entry")
    assert entry["learning_account"] is True and entry["symbol"] == "NVDA"
    assert all(text.startswith("LEARNING (paper, separate account)") for text in rig.notifier.texts)
    assert rig.provider.calls == [("get_quote", "NVDA")]  # one quote, nothing else on the main client

    rig.offer(snapshot("NVDA"))  # the same symbol again: never two positions in one symbol
    rig.tick(5)
    assert len(rig.broker.sent("submit_bracket")) == 1
    assert "symbol_in_book" in rig.skipped()


def test_order_ids_are_deterministic() -> None:
    candidate = snapshot("NVDA")
    first = trade_id_for(candidate, NOW)
    assert first == trade_id_for(snapshot("NVDA"), NOW) and len(first) == 20
    assert first != trade_id_for(candidate, NOW + timedelta(seconds=1))
    assert first != trade_id_for(snapshot("AMD"), NOW)
    assert first != trade_id_for(snapshot("NVDA", "ma_crossover"), NOW)
    assert entry_client_order_id(first) == f"la-e-{first}"
    close = close_client_order_id(first, 0)
    assert close.startswith("la-x-") and len(close) == 29
    assert close == close_client_order_id(first, 0) != close_client_order_id(first, 1)
    assert len(entry_client_order_id(first)) <= 48  # Alpaca's client id limit


def test_the_scan_time_is_the_stamp_capped_at_now() -> None:
    assert scan_time(NOW.isoformat(), NOW + timedelta(minutes=1)) == NOW
    assert scan_time((NOW + timedelta(minutes=5)).isoformat(), NOW) == NOW  # never later than now
    assert scan_time("2026-10-26T14:00:00", NOW) is None  # naive: nothing is offered
    assert scan_time("garbage", NOW) is None and scan_time(None, NOW) is None


def test_an_offer_is_a_copy_in_a_bounded_queue_and_never_starts_a_thread(tmp_path) -> None:
    rig = Rig(tmp_path)
    before = threading.active_count()
    candidate = snapshot("NVDA")
    assert rig.account.offer([candidate] * 250, NOW.isoformat()) == 250
    assert len(rig.account._queue) == 200  # maxlen
    candidate.metadata["alert_eligible"] = False  # the queued copy is not the scan's object
    assert all(item.metadata["alert_eligible"] for _, item in rig.account._queue)
    assert rig.account.offer([candidate], "2026-10-26T14:00:00") == 0  # a naive stamp offers nothing
    assert threading.active_count() == before and rig.account._thread is None
    assert rig.store.reads == [] and rig.store.writes == [] and rig.broker.calls == []


def test_freshness_counts_from_the_scan(tmp_path) -> None:
    rig = Rig(tmp_path)
    scanned = NOW - timedelta(minutes=6)
    rig.offer(snapshot("AMD", "vwap_reclaim", "5m"), snapshot("NVDA"), at=scanned)
    rig.tick()
    assert "stale_candidate" in rig.skipped()  # the 5m one: older than 5 min since its scan
    assert [t["symbol"] for t in rig.book().values()] == ["NVDA"]  # swing: 30 min


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"execution_ready": False}, "not_execution_ready"),
        ({"metadata": {"alert_eligible": False, "backtest_validated": True}}, "not_alert_eligible"),
        ({"metadata": {"alert_eligible": True, "backtest_validated": False}}, "not_backtest_validated"),
        ({"metadata": {"alert_eligible": True, "backtest_validated": True, "signal_classification": "paper_near_miss"}}, "not_strict_valid"),
        ({"signal_role": "entry_short"}, "not_long"),
        ({"score": 64.9}, "score_below_auto_floor"),
        ({"symbol": "BTC/USD"}, "crypto_or_no_symbol"),
        ({"take_profit": None}, "not_strict_valid"),
    ],
)
def test_the_prefilter(tmp_path, change, reason) -> None:
    rig = Rig(tmp_path)
    rig.offer(snapshot("NVDA").model_copy(update=change))
    rig.tick()
    assert rig.skipped() == [reason] and rig.book() == {}
    assert rig.broker.sent("submit_bracket") == []


def test_a_lane_key_goes_after_a_fee_band_one_and_the_nearest_miss_first(tmp_path) -> None:
    learning = {
        **LEARNING,
        "pullback_trend:1d": {"basis": "fee_free", "passed": False, "oos_trades": 40, "oos_expectancy_r": -0.001},
    }
    verdicts = {**VERDICTS, "pullback_trend:1d": dict(VERDICTS["ma_crossover:1d"])}
    rig = Rig(tmp_path, verdicts=verdicts, learning=learning)
    rig.offer(
        snapshot("AMD", "ma_crossover", score=99.0),  # lane, -0.02R
        snapshot("MU", "pullback_trend", score=70.0),  # lane, -0.001R: nearer
        snapshot("NVDA", "momentum_breakout", score=66.0),  # fee band
        snapshot("GOOG", "trend_following"),  # -0.08R: outside the lane
    )
    rig.tick()
    order = [args[0] for args in rig.broker.sent("submit_bracket")]
    assert order == ["NVDA", "MU", "AMD"]
    kinds = {t["symbol"]: t["kind"] for t in rig.book().values()}
    assert kinds == {"NVDA": "fee_band", "MU": "lane", "AMD": "lane"}
    assert "lane_expectancy_below_floor" in rig.skipped()


def test_only_an_evidence_only_refusal_reaches_the_learning_account(tmp_path) -> None:
    rig = Rig(tmp_path, safety=FakeSafety(blacklisted={"AMD"}, inactive={"ma_crossover"}))
    rig.offer(
        snapshot("AMD"),  # blacklisted on the main path too
        snapshot("MU", "ma_crossover"),  # strategy switched off by health
        snapshot("GOOG", "inside_bar_narrow_range_breakout"),  # the main gate takes it
        snapshot("NVDA", "unknown_strategy"),  # no verdict at all: evidence, but nothing to judge
    )
    rig.tick()
    assert rig.book() == {}
    skipped = rig.skipped()
    assert "main_path_blocked:symbol_blacklisted" in skipped
    assert "main_path_blocked:strategy_inactive" in skipped
    assert "passes_the_main_gate" in skipped
    assert "no_evidence_verdict" in skipped


@pytest.mark.parametrize("bar", [True, False])
def test_option_b_keys_stay_with_the_main_path_while_its_bar_is_on(tmp_path, bar) -> None:
    """``relative_strength_momentum:1d`` is OOS-only (paper_oos_passed): with the bar on it is
    the main path's, placed or held back by B's guards alike (the stand-in answers without them);
    with the bar off the main path refuses it for evidence and the learning rules judge it."""

    rig = Rig(tmp_path, paper_oos_bar_enabled=bar)
    rig.offer(snapshot("NVDA", "relative_strength_momentum"))
    rig.tick()
    if bar:
        assert rig.book() == {} and "passes_the_paper_oos_bar" in rig.skipped()
    else:
        assert [t["kind"] for t in rig.book().values()] == ["fee_band"]


def test_the_stand_in_reads_the_verdicts_once_and_never_b_s_guards(tmp_path) -> None:
    rig = Rig(tmp_path)
    rig.offer(snapshot("NVDA", "relative_strength_momentum"), snapshot("AMD", "relative_strength_momentum"), snapshot("MU"))
    rig.tick()  # Boom collaborators (eToro records, paper book, main broker) would raise
    from app.performance.strategy_evidence import VERDICTS_KEY

    assert rig.auto.runtime_state.reads == [VERDICTS_KEY]
    assert [t["symbol"] for t in rig.book().values()] == ["MU"]
    blockers = rig.auto.candidate_proposal_blockers(snapshot("NVDA", "relative_strength_momentum"), paper_only=account_module.MainPathOnly(rig.auto))
    assert blockers == ["paper_only_admitted"]


def test_a_minute_key_passing_with_fees_but_not_paper_passed_is_judged_on_the_alpaca_profile(tmp_path) -> None:
    rig = Rig(tmp_path)
    rig.offer(snapshot("NVDA", "rsi_reversal", "15m"))
    rig.tick()
    (trade,) = rig.book().values()
    assert (trade["kind"], trade["basis"], trade["timeframe"]) == ("fee_band", "alpaca", "15m")


def test_caps_count_from_the_durable_book_closed_trades_included(tmp_path) -> None:
    rig = Rig(tmp_path)
    day = account_module.ny_day(NOW)
    old = {
        "a": {"trade_id": "a", "symbol": "X1", "status": "closed", "day": day, "submitted_at": NOW.isoformat()},
        "b": {"trade_id": "b", "symbol": "X2", "status": "closed_unreported", "day": day, "submitted_at": NOW.isoformat()},
        "c": {"trade_id": "c", "symbol": "X3", "status": "rejected", "day": day, "submitted_at": NOW.isoformat()},
    }
    records.Store(rig.db).put(records.BOOK_KEY, records.dumps({"trades": old}))
    rig.offer(snapshot("NVDA"), snapshot("AMD"))
    rig.tick()
    entered = [t["symbol"] for t in rig.book().values() if t["trade_id"] not in old]
    assert entered == ["NVDA"]  # 2 closed today + 1 = 3; the rejected one is no entry
    assert account_module.day_counts(rig.book(), day) == {"entries": 3, "orders": 4, "open": 1}
    assert "daily_entry_cap" in rig.skipped()
    assert (MAX_ENTRIES_PER_DAY, MAX_ORDERS_PER_DAY, MAX_OPEN) == (3, 6, 4)


def test_six_orders_a_day_and_four_open(tmp_path) -> None:
    day = account_module.ny_day(NOW)
    rejected = {
        f"r{i}": {"trade_id": f"r{i}", "symbol": f"R{i}", "status": "rejected", "day": day, "submitted_at": NOW.isoformat()}
        for i in range(6)
    }
    assert account_module.cap_reason(rejected, day) == "daily_order_cap"
    live = {f"o{i}": {"trade_id": f"o{i}", "symbol": f"O{i}", "status": "open", "day": "2026-10-23"} for i in range(4)}
    assert account_module.cap_reason(live, day) == "open_position_cap"
    assert account_module.cap_reason(dict(list(live.items())[:3]), day) is None
    unsent = {"n": {"trade_id": "n", "status": "rejected", "day": day, "reason": "not_sent:stopping"}}
    assert account_module.day_counts(unsent, day) == {"entries": 0, "orders": 0, "open": 0}


def test_the_main_kill_switch_or_pause_stops_entries_and_fails_closed(tmp_path) -> None:
    for automation, reason in (
        (FakeAutomation(["automation_kill_switch_enabled"]), "automation_kill_switch_enabled"),
        (FakeAutomation(["automation_paused"]), "automation_paused"),
        (FakeAutomation(fail=True), "automation_unavailable"),
    ):
        rig = Rig(tmp_path / reason, automation=automation)
        rig.offer(snapshot("NVDA"))
        rig.tick()
        assert rig.book() == {} and reason in rig.skipped()
    rig = Rig(tmp_path / "missing")
    rig.account.automation = None
    rig.offer(snapshot("NVDA"))
    rig.tick()
    assert rig.book() == {} and "automation_unavailable" in rig.skipped()


def test_no_entry_while_the_market_is_closed_or_near_the_close_for_intraday(tmp_path) -> None:
    rig = Rig(tmp_path)
    rig.broker.clock_value["is_open"] = False
    rig.offer(snapshot("NVDA"))
    rig.tick()
    assert rig.book() == {} and "market_closed" in rig.skipped()
    rig = Rig(tmp_path / "late", clock=Clock(CLOSE - timedelta(minutes=15)))  # 10 min window + 5
    rig.offer(snapshot("NVDA", "vwap_reclaim", "5m"), snapshot("AMD"))
    rig.tick()
    assert "too_close_to_the_close" in rig.skipped()
    assert [t["symbol"] for t in rig.book().values()] == ["AMD"]  # a swing entry still goes


def test_an_identity_failure_at_entry_refuses_that_candidate_only(tmp_path) -> None:
    rig = Rig(tmp_path)
    rig.tick()  # verified: running
    calls = {"n": 0}
    original = rig.broker.identity

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise TimeoutError("read timed out")
        return original()

    rig.broker.identity = flaky
    rig.offer(snapshot("NVDA"), snapshot("AMD", "ma_crossover"))
    rig.tick(5)
    assert [t["symbol"] for t in rig.book().values()] == ["AMD"]
    assert "identity_not_verified" in rig.skipped() and rig.account.route == "running"


def test_a_different_account_at_entry_stops_entries(tmp_path) -> None:
    rig = Rig(tmp_path)
    rig.tick()
    rig.broker.account["account_number"] = "PA-OTHER"
    rig.offer(snapshot("NVDA"), snapshot("AMD", "ma_crossover"))
    rig.tick(5)
    assert rig.book() == {} and rig.account.route == "account_mismatch"
    assert len(rig.broker.sent("identity")) == 2  # the first candidate's check, then no more


def test_one_quote_any_failure_refuses_and_the_main_checks_apply(tmp_path) -> None:
    rig = Rig(tmp_path)
    rig.provider.quote_error = RuntimeError("feed down")
    rig.offer(snapshot("NVDA"))
    rig.tick()
    assert rig.book() == {} and "quote_unavailable" in rig.skipped()
    assert rig.provider.calls == [("get_quote", "NVDA")]  # no fallback, no retry

    for price, stop, reason in ((101.0, 95.0, "entry_drift_too_large"), (99.8, 99.9, "stop_loss_not_below_entry")):
        rig = Rig(tmp_path / reason)
        rig.provider.price = price
        rig.offer(snapshot("NVDA", stop=stop))
        rig.tick()
        assert rig.book() == {} and reason in rig.skipped()
    rig = Rig(tmp_path / "rr")
    rig.offer(snapshot("NVDA", target=104.0))  # 4 / 5 = 0.8R at the quote
    rig.tick()
    assert "reward_to_risk_below_min_at_quote" in rig.skipped()


def test_no_entry_when_the_learning_broker_holds_the_symbol(tmp_path) -> None:
    rig = Rig(tmp_path)
    rig.broker.held["NVDA"] = 3.0
    rig.offer(snapshot("NVDA"))
    rig.tick()
    assert rig.book() == {} and "symbol_held_at_broker" in rig.skipped()
    rig = Rig(tmp_path / "unreadable")
    rig.broker.positions_error = RuntimeError("down")
    rig.offer(snapshot("NVDA"))
    rig.tick()
    assert rig.book() == {} and "positions_unreadable" in rig.skipped()


def test_a_trade_too_small_for_one_share_is_refused(tmp_path) -> None:
    rig = Rig(tmp_path)
    rig.broker.account["equity"] = 900.0  # $8.82 of risk, half of it: 0.88 share
    rig.offer(snapshot("NVDA"))
    rig.tick()
    assert rig.book() == {} and "size_below_one_share" in rig.skipped()


def test_market_direction_uses_the_alpaca_provider_without_cache_at_most_every_five_minutes(tmp_path) -> None:
    import pandas as pd

    rig = Rig(tmp_path)
    stamps = pd.date_range("2026-10-23 13:30", periods=78, freq="5min", tz="UTC").append(
        pd.date_range("2026-10-26 13:30", periods=6, freq="5min", tz="UTC")
    )
    closes = [100.0] * 78 + [99.0, 98.5, 98.0, 98.0, 97.9, 97.8]  # down 2.2%, below VWAP
    rig.provider.bars["SPY"] = pd.DataFrame(
        {"timestamp": stamps, "open": closes, "high": closes, "low": closes, "close": closes, "volume": [1e6] * 84}
    )
    rig.offer(snapshot("NVDA", "vwap_reclaim", "5m"))  # NVDA: SPY and QQQ
    rig.tick()
    assert rig.book() == {}
    assert any(reason.startswith("market_direction_down:SPY") for reason in rig.skipped())
    bars = [args for name, args in rig.provider.calls if name == "get_bars"]
    assert bars == [("SPY", "5m", False), ("QQQ", "5m", False)]  # QQQ failed: no block, remembered
    rig.offer(snapshot("NVDA", "vwap_reclaim", "5m"))
    rig.tick(60)
    assert [args for name, args in rig.provider.calls if name == "get_bars"] == bars  # within 5 min
    rig.offer(snapshot("NVDA", "vwap_reclaim", "5m"))
    rig.tick(300)
    assert len([1 for name, _ in rig.provider.calls if name == "get_bars"]) == 4


def test_hung_reads_on_the_main_data_client_stop_new_reads(tmp_path) -> None:
    pending: list = []
    reads = BoundedReads(spawn=pending.append, wait=lambda event, seconds: event.is_set())
    rig = Rig(tmp_path)
    rig.account.market = MarketReads(FakeEngine(rig.provider), rig.settings, now=rig.clock, reads=reads)
    for symbol in ("NVDA", "AMD", "MU"):
        rig.offer(snapshot(symbol))
        rig.tick(5)
    assert reads.hung == 3 and rig.skipped().count("quote_unavailable") == 1  # logged once a day
    rig.offer(snapshot("GOOG"))
    rig.tick(5)
    assert "market_data_reads_hung" in rig.skipped()
    assert len(pending) == 3  # no fourth read started
    assert rig.account.market.direction_reasons("GOOG", "5m") == ["market_data_reads_hung"]
    pending.pop()()  # one hung read returns
    assert reads.hung == 2 and not reads.too_many_hung()
    assert reads.timeout == 10.0


def test_bounded_reads_return_values_and_errors() -> None:
    reads = BoundedReads(spawn=lambda target: target())
    assert reads.call(lambda value: value * 2, 21) == 42
    with pytest.raises(KeyError):
        reads.call(lambda: {}["x"])
    real = BoundedReads(timeout=5.0)
    assert real.call(lambda: "ok") == "ok" and real.hung == 0


def test_stop_is_checked_before_each_candidate(tmp_path) -> None:
    rig = Rig(tmp_path)
    rig.broker.on_submit = lambda symbol: rig.account._stopping.set()
    rig.offer(snapshot("NVDA"), snapshot("AMD", "ma_crossover"))
    rig.tick()
    assert [args[0] for args in rig.broker.sent("submit_bracket")] == ["NVDA"]
    assert [t["symbol"] for t in rig.book().values()] == ["NVDA"]  # AMD was never even written


def test_a_trade_recorded_but_not_sent_is_rejected_and_frees_its_slot(tmp_path) -> None:
    rig = Rig(tmp_path)
    rig.tick()

    def stop_after_the_write(key):
        if key == records.BOOK_KEY:
            rig.account._stopping.set()

    rig.store.before_cas = stop_after_the_write
    rig.offer(snapshot("NVDA"))
    rig.tick(5)
    (trade,) = rig.book().values()
    assert (trade["status"], trade["reason"]) == ("rejected", "not_sent:stopping")
    assert rig.broker.sent("submit_bracket") == []
    assert account_module.day_counts(rig.book(), account_module.ny_day(NOW)) == {"entries": 0, "orders": 0, "open": 0}


def test_no_send_without_thirty_seconds_of_lease(tmp_path) -> None:
    rig = Rig(tmp_path)
    rig.tick()
    rig.account.lease.expires_at = rig.clock() + timedelta(seconds=29)
    rig.account.lease.ensure = lambda work: True  # as if just checked
    rig.offer(snapshot("NVDA"))
    rig.tick(0)
    (trade,) = rig.book().values()
    assert trade["reason"] == "not_sent:lease" and rig.broker.sent("submit_bracket") == []


def test_a_refused_send_is_rejected_and_an_unknown_one_keeps_its_slot(tmp_path) -> None:
    rig = Rig(tmp_path)
    rig.broker.submit_error = APIError("insufficient buying power", status_code=403)
    rig.offer(snapshot("NVDA"))
    rig.tick()
    (trade,) = rig.book().values()
    assert trade["status"] == "rejected" and trade["submitted_at"] == NOW.isoformat()
    rig = Rig(tmp_path / "unknown")
    rig.broker.submit_error = TimeoutError("read timed out")
    rig.offer(snapshot("NVDA"))
    rig.tick()
    (trade,) = rig.book().values()
    assert trade["status"] == "unknown"
    assert account_module.day_counts(rig.book(), account_module.ny_day(NOW))["open"] == 1
    from app.learning_account.client import refused

    assert not refused(APIError("client_order_id must be unique", status_code=422))
    assert not refused(APIError("rate limited", status_code=429)) and refused(APIError("bad qty", status_code=422))


def test_a_book_race_ends_the_pass_without_an_order(tmp_path) -> None:
    rig = Rig(tmp_path)
    rig.tick()
    other = records.Store(rig.db)

    def someone_else_writes(key):
        if key == records.BOOK_KEY:
            rig.store.before_cas = None
            other.put(records.BOOK_KEY, records.dumps({"trades": {}, "by": "another"}))

    rig.store.before_cas = someone_else_writes
    rig.offer(snapshot("NVDA"))
    rig.tick(5)
    assert rig.broker.sent("submit_bracket") == []
    assert json.loads(other.read(records.BOOK_KEY))["by"] == "another"
    assert any(p.get("error") == "book_changed" for p in rig.logs.named("learning_account_status"))


def test_learning_verdicts_refresh_at_most_every_thirty_minutes_with_a_backoff(tmp_path, monkeypatch) -> None:
    calls: list = []

    def compute(db, settings, strategies, *, now=None):
        calls.append(set(strategies))
        if len(calls) == 2:
            raise ValueError("bad row")
        return LEARNING

    monkeypatch.setattr(account_module, "compute_learning_verdicts", compute)
    rig = Rig(tmp_path, learning=False)  # nothing stored yet
    rig.provider.quote_error = RuntimeError("no quotes")  # so no entry fills the day's caps
    rig.offer(snapshot("NVDA"))
    rig.tick()
    assert calls == [{"momentum_breakout"}]
    stored = json.loads(records.Store(rig.db).read(records.LEARNING_VERDICTS_KEY))
    assert stored["strategies"] == ["momentum_breakout"] and stored["verdicts"] == LEARNING
    rig.offer(snapshot("AMD", "ma_crossover"))
    rig.tick(29 * 60)
    assert len(calls) == 1  # within 30 min: the stored ones serve
    rig.offer(snapshot("MU", "ma_crossover"))
    rig.tick(60)
    assert calls[1] == {"momentum_breakout", "ma_crossover"}  # waiting plus stored: and it failed
    failures = rig.logs.named("learning_account_verdicts_failed")
    assert [f["error_type"] for f in failures] == ["ValueError"]
    rig.offer(snapshot("GOOG", "ma_crossover"))
    rig.tick(9 * 60)
    assert len(calls) == 2  # 10 min of backoff
    rig.offer(snapshot("GOOG", "ma_crossover"))
    rig.tick(60)
    assert len(calls) == 3


def test_verdicts_are_computed_after_the_cheap_gates_and_never_for_nothing(tmp_path, monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(account_module, "compute_learning_verdicts", lambda *a, **k: calls.append(1) or LEARNING)
    rig = Rig(tmp_path, learning=False, automation=FakeAutomation(["automation_paused"]))
    rig.offer(snapshot("NVDA"))
    rig.tick()
    assert calls == []
    rig = Rig(tmp_path / "no-candidates", learning=False)
    rig.offer(snapshot("NVDA", score=10.0))
    rig.tick()
    assert calls == []


def test_stale_learning_verdicts_refuse(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(account_module, "compute_learning_verdicts", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    rig = Rig(tmp_path, learning=False)
    old = {"computed_at": (NOW - timedelta(hours=25)).isoformat(), "strategies": ["momentum_breakout"], "verdicts": LEARNING}
    records.Store(rig.db).put(records.LEARNING_VERDICTS_KEY, records.dumps(old))
    rig.offer(snapshot("NVDA"))
    rig.tick()
    assert rig.book() == {} and "learning_verdicts_unavailable" in rig.skipped()


def test_skips_are_logged_once_per_reason_per_day(tmp_path) -> None:
    rig = Rig(tmp_path)
    for _ in range(3):
        rig.offer(snapshot("NVDA", score=10.0), snapshot("AMD", score=10.0))
        rig.tick(60)
    assert rig.skipped() == ["score_below_auto_floor"]
    rig.clock.value = NOW + timedelta(days=1)
    rig.offer(snapshot("NVDA", score=10.0))
    rig.tick()
    assert rig.skipped() == ["score_below_auto_floor"] * 2
    assert all(p["learning_account"] is True for _, p in rig.logs.events)


def test_a_trade_being_sent_counts_as_an_entry_and_an_order() -> None:
    day = account_module.ny_day(NOW)
    sending = {"s": {"trade_id": "s", "status": "submitting", "day": day}}
    assert account_module.day_counts(sending, day) == {"entries": 1, "orders": 1, "open": 1}
    other_day = {"s": {"trade_id": "s", "status": "closed", "day": "2026-10-23", "submitted_at": "x"}}
    assert account_module.day_counts(other_day, day) == {"entries": 0, "orders": 0, "open": 0}


def test_nothing_is_offered_after_stop_and_the_constants(tmp_path) -> None:
    rig = Rig(tmp_path)
    rig.account.stop()
    assert rig.offer(snapshot("NVDA")) == 0 and len(rig.account._queue) == 0
    assert (account_module.TICK_SECONDS, account_module.FIRST_PASS_DELAY_SECONDS, account_module.QUEUE_LIMIT) == (60.0, 30.0, 200)
    assert (account_module.INTRADAY_FRESH_SECONDS, account_module.SWING_FRESH_SECONDS) == (300.0, 1800.0)
    assert (account_module.VERDICTS_EVERY_SECONDS, account_module.VERDICTS_BACKOFF_SECONDS) == (1800.0, 600.0)
    assert (account_module.REVERIFY_SECONDS, account_module.ENTRY_CLOSE_BUFFER_MINUTES) == (900.0, 5.0)


def test_settings_that_refuse_stop_every_pass(tmp_path) -> None:
    rig = Rig(tmp_path)
    rig.account.settings = rig.settings.model_copy(update={"learning_account_enabled": False})
    rig.offer(snapshot("NVDA"))
    rig.tick()
    assert rig.account.route == "disabled:setting_off" and rig.broker.calls == []
    assert rig.store.reads == [] and rig.store.writes == [records.STATUS_KEY]
    assert len(rig.account._queue) == 0


def test_an_unknown_market_clock_refuses_entries(tmp_path) -> None:
    rig = Rig(tmp_path)

    def broken():
        raise TimeoutError("clock")

    rig.broker.clock = broken
    rig.offer(snapshot("NVDA"))
    rig.tick()
    assert rig.book() == {} and "market_clock_unavailable" in rig.skipped()


def test_the_quote_must_be_a_direct_fresh_alpaca_quote(tmp_path) -> None:
    from app.live_signal_schema import MarketQuote

    cases = (
        ({"source": "yfinance"}, "quote_provider_not_alpaca"),
        ({"quote_derived_from_history": True}, "quote_not_direct"),
        ({"data_age_seconds": 121.0}, "quote_too_old"),
        ({"last_execution": None, "ask": None, "bid": None}, "quote_missing"),
    )
    for change, reason in cases:
        rig = Rig(tmp_path / reason)
        quote = MarketQuote(symbol="NVDA", bid=99.99, ask=100.01, last_execution=100.0, source="alpaca", data_age_seconds=1.0)
        changed = quote.model_copy(update=change)
        rig.provider.get_quote = lambda symbol, _q=changed: _q
        rig.offer(snapshot("NVDA"))
        rig.tick()
        assert rig.book() == {} and reason in rig.skipped(), reason


def test_no_main_data_provider_refuses_with_no_fallback(tmp_path) -> None:
    rig = Rig(tmp_path)
    rig.account.market = MarketReads(FakeEngine(None), rig.settings, now=rig.clock, reads=BoundedReads(spawn=lambda t: t()))
    rig.offer(snapshot("NVDA"))
    rig.tick()
    assert rig.book() == {} and "quote_unavailable" in rig.skipped()


def test_full_caps_stop_the_pass_before_any_verdict_or_main_path_read(tmp_path, monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(account_module, "compute_learning_verdicts", lambda *a, **k: calls.append("verdicts") or LEARNING)
    rig = Rig(tmp_path, learning=False)
    day = account_module.ny_day(NOW)
    full = {f"c{i}": {"trade_id": f"c{i}", "symbol": f"C{i}", "status": "closed", "day": day} for i in range(3)}
    records.Store(rig.db).put(records.BOOK_KEY, records.dumps({"trades": full}))
    rig.auto.candidate_proposal_blockers = lambda *a, **k: calls.append("main") or []
    rig.offer(snapshot("NVDA"))
    rig.tick()
    assert calls == [] and rig.skipped() == ["daily_entry_cap"]
    assert ("clock", None) in rig.broker.calls  # the reconcile's; no entry clock read was needed


def test_an_intraday_entry_needs_a_known_close(tmp_path) -> None:
    rig = Rig(tmp_path)
    rig.broker.clock_value["next_close"] = None
    rig.offer(snapshot("NVDA", "vwap_reclaim", "5m"), snapshot("AMD"))
    rig.tick()
    assert "too_close_to_the_close" in rig.skipped()
    assert [t["symbol"] for t in rig.book().values()] == ["AMD"]


def test_no_provider_is_a_refusal_never_a_fallback() -> None:
    from app.learning_account.market import MarketDataUnavailable

    reads = MarketReads(FakeEngine(None), None, now=lambda: NOW, reads=BoundedReads(spawn=lambda t: t()))
    with pytest.raises(MarketDataUnavailable):
        reads.quote("NVDA")
    with pytest.raises(MarketDataUnavailable):
        reads.get_history("SPY")
