"""Reconciling the learning account (app/learning_account/reconcile.py): fills, exits with R,
the intraday flatten (legs first, a deterministic close id, recovery of a lost one), the
catch-up flatten, unprotected and hand-closed positions, the closed list written first, the
cadence and the untracked-position sweep. The learning broker only: nothing else is called."""

from __future__ import annotations

import json
from datetime import timedelta

from app.learning_account import store as records
from app.learning_account.account import close_client_order_id, day_counts, ny_day
from app.learning_account.reconcile import flatten_due, next_reconcile, r_multiple
from tests.learning_account_fakes import CLOSE, NEXT_OPEN, NOW, Clock, FakeAutomation, Rig, snapshot


def _entered(tmp_path, *candidates, **kwargs):
    rig = Rig(tmp_path, **kwargs)
    rig.offer(*(candidates or (snapshot("NVDA"),)))
    rig.tick()
    return rig


def _only(rig):
    (trade,) = rig.book().values()
    return trade


def test_a_fill_then_a_stop_records_the_exit_with_r_from_the_fills_once(tmp_path) -> None:
    rig = _entered(tmp_path)
    assert rig.account.reconcile_at == NOW + timedelta(seconds=60)  # live: within a minute
    rig.broker.fill_entry("NVDA", 100.2)
    rig.tick(60)
    trade = _only(rig)
    assert (trade["status"], trade["entry_fill"], trade["filled_qty"]) == ("open", 100.2, 49.0)
    assert [leg["status"] for leg in trade["legs"]] == ["new", "held"]
    rig.broker.fill_leg("NVDA", "stop", 95.0, at=NOW + timedelta(minutes=2))
    rig.tick(60)
    trade = _only(rig)
    assert trade["status"] == "closed" and trade["exit_reason"] == "stop_loss"
    assert trade["r"] == r_multiple(100.2, 95.0, 95.0) == -1.0
    assert trade["pnl_usd"] == round((95.0 - 100.2) * 49, 2)
    (closed,) = rig.closed()
    assert closed["trade_id"] == trade["trade_id"] and closed["r"] == -1.0 and closed["kind"] == "fee_band"
    exits = rig.logs.named("learning_account_exit")
    assert len(exits) == 1 and exits[0]["learning_account"] is True
    assert sum("Exit NVDA" in text for text in rig.notifier.texts) == 1
    for _ in range(3):
        rig.tick(60)
    assert len(rig.logs.named("learning_account_exit")) == 1 and len(rig.closed()) == 1
    assert rig.broker.sent("submit_close") == [] and rig.broker.sent("cancel") == []


def test_a_take_profit_exit(tmp_path) -> None:
    rig = _entered(tmp_path)
    rig.broker.fill_entry("NVDA", 100.0)
    rig.broker.fill_leg("NVDA", "limit", 110.0)
    rig.tick(60)
    trade = _only(rig)
    assert (trade["status"], trade["exit_reason"], trade["r"]) == ("closed", "take_profit", 2.0)


def test_a_one_off_closed_list_failure_keeps_the_trade_unreported_until_appended(tmp_path) -> None:
    rig = _entered(tmp_path)
    rig.broker.fill_entry("NVDA", 100.0)
    rig.tick(60)
    rig.broker.fill_leg("NVDA", "limit", 110.0)
    rig.store.fail_cas.add(records.CLOSED_KEY)
    rig.tick(60)
    trade = _only(rig)
    assert trade["status"] == "closed_unreported" and trade["r"] == 2.0
    assert rig.closed() == [] and rig.logs.named("learning_account_exit") == []
    assert day_counts(rig.book(), ny_day(NOW))["entries"] == 1
    rig.tick(600)  # idle cadence: the append is retried
    assert _only(rig)["status"] == "closed" and len(rig.closed()) == 1
    assert len(rig.logs.named("learning_account_exit")) == 1


def test_an_unreadable_closed_list_keeps_the_trade_however_old_and_counting(tmp_path) -> None:
    rig = _entered(tmp_path)
    rig.broker.fill_entry("NVDA", 100.0)
    rig.tick(60)
    records.Store(rig.db).put(records.CLOSED_KEY, "{not a list")
    rig.broker.fill_leg("NVDA", "stop", 95.0)
    rig.tick(60)
    assert _only(rig)["status"] == "closed_unreported"
    assert day_counts(rig.book(), ny_day(NOW))["entries"] == 1
    rig.clock.advance(3 * 86_400)  # days later: still kept, still unreported
    rig.tick(600)
    assert _only(rig)["status"] == "closed_unreported"
    records.Store(rig.db).put(records.CLOSED_KEY, "[]")
    rig.tick(600)
    assert len(rig.closed()) == 1 and len(rig.logs.named("learning_account_exit")) == 1
    assert rig.book() == {}  # appended: a final trade from an earlier day leaves the book


def test_intraday_positions_are_flattened_before_the_close_legs_first(tmp_path) -> None:
    rig = _entered(tmp_path, snapshot("NVDA", "vwap_reclaim", "5m"))
    rig.broker.fill_entry("NVDA", 100.0)
    rig.tick(60)
    rig.clock.value = CLOSE - timedelta(minutes=9)
    rig.tick()
    trade = _only(rig)
    assert trade["flatten_reason"] == "session_flatten" and "close_order_id" not in trade
    assert sorted(rig.broker.sent("cancel")) == ["o1-sl", "o1-tp"]
    assert rig.broker.sent("submit_close") == []  # not while a leg may still be live
    rig.tick(60)
    ((symbol, qty, client_id),) = rig.broker.sent("submit_close")
    assert (symbol, qty) == ("NVDA", 49.0)
    assert [leg["status"] for leg in _only(rig)["legs"]] == ["canceled", "canceled"]  # the book follows the legs
    assert client_id == close_client_order_id(trade["trade_id"], 0)
    (flatten,) = rig.logs.named("learning_account_flatten")
    assert flatten["reason"] == "session_flatten" and flatten["learning_account"] is True
    close_id = _only(rig)["close_order_id"]
    rig.tick(60)
    assert _only(rig)["status"] == "open"  # the close is not filled yet
    rig.broker.fill_close(close_id, 100.5)
    rig.tick(60)
    trade = _only(rig)
    assert (trade["status"], trade["exit_reason"], trade["r"]) == ("closed", "session_flatten", 0.1)
    assert len(rig.broker.sent("submit_close")) == 1


def test_a_lost_close_id_is_looked_up_before_anything_is_sold_again(tmp_path) -> None:
    rig = _entered(tmp_path, snapshot("NVDA", "vwap_reclaim", "5m"))
    rig.broker.fill_entry("NVDA", 100.0)
    rig.tick(60)
    rig.clock.value = CLOSE - timedelta(minutes=9)
    rig.tick()
    rig.tick(60)  # the close is sent
    trade = _only(rig)
    lost = {**trade, "close_order_id": None, "close_sent_at": None}  # its book write was lost
    store = records.Store(rig.db)
    store.put(records.BOOK_KEY, records.dumps({"trades": {trade["trade_id"]: lost}}))
    rig.broker.fill_close(trade["close_order_id"], 100.4)  # it filled meanwhile: position gone
    rig.tick(60)
    trade = _only(rig)
    assert trade["status"] == "closed" and trade["exit_price"] == 100.4 and trade["r"] == 0.08
    assert len(rig.broker.sent("submit_close")) == 1  # never sold twice
    assert close_client_order_id(trade["trade_id"], 0) in rig.broker.sent("order_by_client_id")


def test_a_lost_close_id_while_the_close_is_still_working_is_adopted(tmp_path) -> None:
    rig = _entered(tmp_path, snapshot("NVDA", "vwap_reclaim", "5m"))
    rig.broker.fill_entry("NVDA", 100.0)
    rig.tick(60)
    rig.clock.value = CLOSE - timedelta(minutes=9)
    rig.tick()
    rig.tick(60)
    trade = _only(rig)
    store = records.Store(rig.db)
    store.put(records.BOOK_KEY, records.dumps({"trades": {trade["trade_id"]: {**trade, "close_order_id": None}}}))
    rig.tick(60)
    assert _only(rig)["close_order_id"] == trade["close_order_id"]
    assert len(rig.broker.sent("submit_close")) == 1


def test_a_refused_close_is_sent_again_with_the_next_attempt_id(tmp_path) -> None:
    rig = _entered(tmp_path, snapshot("NVDA", "vwap_reclaim", "5m"))
    rig.broker.fill_entry("NVDA", 100.0)
    rig.tick(60)
    rig.clock.value = CLOSE - timedelta(minutes=9)
    rig.tick()
    rig.tick(60)
    first = _only(rig)
    rig.broker.orders[first["close_order_id"]]["status"] = "rejected"
    rig.tick(60)
    assert _only(rig)["close_attempt"] == 1 and _only(rig)["close_order_id"] is None
    rig.tick(60)
    ids = [args[2] for args in rig.broker.sent("submit_close")]
    assert ids == [close_client_order_id(first["trade_id"], 0), close_client_order_id(first["trade_id"], 1)]


def test_an_intraday_position_from_an_earlier_day_is_closed_at_the_next_session(tmp_path) -> None:
    rig = _entered(tmp_path, snapshot("NVDA", "vwap_reclaim", "5m"))
    rig.broker.fill_entry("NVDA", 100.0)
    rig.tick(60)
    rig.broker.clock_value = {"is_open": False, "next_open": NEXT_OPEN, "next_close": CLOSE + timedelta(days=1)}
    rig.clock.value = CLOSE + timedelta(hours=1)  # after the close, still held (e.g. a missed flatten)
    rig.tick()
    assert rig.broker.sent("cancel") == [] and "flatten_reason" not in _only(rig)
    assert rig.account.reconcile_at == rig.clock() + timedelta(minutes=15)  # closed: every 15 min
    rig.broker.clock_value = {"is_open": True, "next_open": NEXT_OPEN + timedelta(days=1), "next_close": CLOSE + timedelta(days=1)}
    rig.clock.value = NEXT_OPEN + timedelta(minutes=1)
    rig.tick()
    assert _only(rig)["flatten_reason"] == "catch_up_flatten"
    rig.tick(60)
    assert len(rig.broker.sent("submit_close")) == 1


def test_an_unprotected_position_is_closed_at_market(tmp_path) -> None:
    rig = _entered(tmp_path)
    rig.broker.fill_entry("NVDA", 100.0)
    rig.tick(60)
    for leg in rig.broker.entry("NVDA")["legs"]:
        leg["status"] = "expired"
    rig.tick(60)
    trade = _only(rig)
    assert trade["flatten_reason"] == "unprotected" and len(rig.broker.sent("submit_close")) == 1
    assert rig.broker.sent("cancel") == []  # every leg had ended


def test_a_position_closed_by_hand_is_closed_outside(tmp_path) -> None:
    rig = _entered(tmp_path)
    rig.broker.fill_entry("NVDA", 100.0)
    rig.tick(60)
    rig.broker.held.pop("NVDA")  # sold on the dashboard; the legs were left
    rig.tick(60)
    trade = _only(rig)
    assert (trade["status"], trade["exit_reason"], trade["r"]) == ("closed_outside", "closed_outside", None)
    assert rig.closed()[0]["exit_reason"] == "closed_outside"
    assert rig.broker.sent("submit_close") == []


def test_a_trade_written_but_never_sent_or_never_found_is_resolved(tmp_path) -> None:
    rig = Rig(tmp_path)
    day = ny_day(NOW)
    trades = {
        "s": {"trade_id": "s", "symbol": "AAA", "status": "submitting", "day": day, "client_order_id": "la-e-s"},
        "u": {"trade_id": "u", "symbol": "BBB", "status": "unknown", "day": day, "client_order_id": "la-e-u",
              "submitted_at": NOW.isoformat()},
        "f": {"trade_id": "f", "symbol": "CCC", "status": "submitting", "day": day, "client_order_id": "la-e-f"},
    }
    found = rig.broker.submit_bracket(symbol="CCC", qty=3, take_profit=110, stop_loss=95, client_order_id="la-e-f")
    rig.broker.calls.clear()
    records.Store(rig.db).put(records.BOOK_KEY, records.dumps({"trades": trades}))
    rig.tick()
    book = rig.book()
    assert (book["s"]["status"], book["s"]["reason"]) == ("rejected", "not_sent:lost")
    assert book["u"]["status"] == "unknown"  # within its grace: the slot is kept
    assert (book["f"]["status"], book["f"]["broker_order_id"]) == ("submitted", found["id"])
    rig.tick(600)
    assert rig.book()["u"]["status"] == "rejected" and rig.book()["u"]["reason"] == "not_found_at_broker"


def test_an_entry_cancelled_without_a_fill_frees_its_slot(tmp_path) -> None:
    rig = _entered(tmp_path)
    rig.broker.entry("NVDA")["status"] = "canceled"
    rig.tick(60)
    trade = _only(rig)
    assert (trade["status"], trade["reason"]) == ("rejected", "entry_canceled")
    assert day_counts(rig.book(), ny_day(NOW)) == {"entries": 0, "orders": 1, "open": 0}


def test_each_trade_is_handled_on_its_own(tmp_path) -> None:
    rig = _entered(tmp_path, snapshot("NVDA"), snapshot("AMD", "ma_crossover"))
    rig.broker.fill_entry("AMD", 50.0)
    original = rig.broker.order

    def broken(broker_order_id):
        if broker_order_id == rig.broker.entry("NVDA")["id"]:
            raise RuntimeError("timeout")
        return original(broker_order_id)

    rig.broker.order = broken
    rig.tick(60)
    by_symbol = {t["symbol"]: t for t in rig.book().values()}
    assert by_symbol["AMD"]["status"] == "open" and by_symbol["NVDA"]["status"] == "submitted"
    assert any(p.get("trade_id") == by_symbol["NVDA"]["trade_id"] for p in rig.logs.named("learning_account_status"))


def test_the_kill_switch_stops_entries_but_never_flattens(tmp_path) -> None:
    rig = _entered(tmp_path, snapshot("NVDA", "vwap_reclaim", "5m"))
    rig.broker.fill_entry("NVDA", 100.0)
    rig.account.automation = FakeAutomation(["automation_kill_switch_enabled"])
    rig.offer(snapshot("AMD"))
    rig.tick(60)
    assert _only(rig)["status"] == "open" and rig.broker.sent("cancel") == []
    assert len(rig.broker.sent("submit_bracket")) == 1
    rig.broker.fill_leg("NVDA", "stop", 95.0)
    rig.tick(60)
    assert _only(rig)["status"] == "closed"  # reconcile still records the exit


def test_an_unreadable_book_means_no_entries_and_no_reconcile(tmp_path) -> None:
    rig = _entered(tmp_path)
    records.Store(rig.db).put(records.BOOK_KEY, "{broken")
    rig.broker.calls.clear()
    rig.offer(snapshot("AMD"))
    rig.tick(60)
    assert [call for call, _ in rig.broker.calls] == []
    assert any(p.get("error", "").startswith("JSONDecodeError") for p in rig.logs.named("learning_account_status"))


def test_reconcile_only_after_an_identity_read_fails_later(tmp_path) -> None:
    rig = _entered(tmp_path)
    rig.broker.identity_error = TimeoutError("down")
    rig.clock.advance(15 * 60)  # the periodic identity read fails
    rig.broker.fill_entry("NVDA", 100.0)
    rig.offer(snapshot("AMD"))
    rig.tick()
    assert rig.account.route == "verification_failed"
    assert _only(rig)["status"] == "open"  # reconciled
    assert len(rig.broker.sent("submit_bracket")) == 1  # no entry
    fresh = Rig(tmp_path / "never", broker=None)
    fresh.broker.identity_error = TimeoutError("down")
    fresh.tick()
    assert fresh.account.route == "verification_failed" and fresh.store.reads == []  # no pass at all
    assert fresh.account._verify_at == fresh.clock() + timedelta(seconds=120)


def test_the_cadence() -> None:
    live = records.Book(None, {"t": {"status": "open"}})
    idle = records.Book(None, {"t": {"status": "closed"}})
    pending = records.Book(None, {"t": {"status": "open", "flatten_reason": "session_flatten"}})
    open_clock = {"is_open": True, "next_open": NEXT_OPEN, "next_close": CLOSE}
    closed_clock = {"is_open": False, "next_open": NOW + timedelta(hours=3), "next_close": CLOSE}
    assert next_reconcile(live, open_clock, NOW) == NOW + timedelta(seconds=60)
    assert next_reconcile(idle, open_clock, NOW) == NOW + timedelta(seconds=600)
    assert next_reconcile(live, closed_clock, NOW) == NOW + timedelta(seconds=900)
    soon = {**closed_clock, "next_open": NOW + timedelta(minutes=4)}
    assert next_reconcile(live, soon, NOW) == NOW + timedelta(minutes=4)
    assert next_reconcile(pending, closed_clock, NOW) == NOW + timedelta(seconds=60)
    assert next_reconcile(live, None, NOW) == NOW + timedelta(seconds=60)


def test_flatten_due(tmp_path) -> None:
    rig = Rig(tmp_path)
    open_clock = {"is_open": True, "next_close": CLOSE}
    trade = {"timeframe": "1h", "filled_at": NOW.isoformat()}
    assert flatten_due(rig.settings, trade, open_clock, CLOSE - timedelta(minutes=11)) is None
    assert flatten_due(rig.settings, trade, open_clock, CLOSE - timedelta(minutes=10)) == "session_flatten"
    assert flatten_due(rig.settings, {**trade, "timeframe": "1d"}, open_clock, CLOSE - timedelta(minutes=1)) is None
    yesterday = {**trade, "filled_at": (NOW - timedelta(days=1)).isoformat()}
    assert flatten_due(rig.settings, yesterday, open_clock, NOW) == "catch_up_flatten"
    assert flatten_due(rig.settings, yesterday, {"is_open": False}, NOW) is None


def test_untracked_positions_are_reported_once_a_day_and_never_closed(tmp_path) -> None:
    rig = Rig(tmp_path)
    rig.broker.held["TSLA"] = 7.0
    rig.tick()
    (alert,) = rig.logs.named("learning_account_untracked_position")
    assert alert == {"symbol": "TSLA", "qty": 7.0, "learning_account": True}
    assert sum("Untracked position" in text for text in rig.notifier.texts) == 1
    assert rig.account.sweep_at == NOW + timedelta(minutes=15)
    rig.tick(15 * 60)
    assert len(rig.logs.named("learning_account_untracked_position")) == 1
    rig.clock.value = NOW + timedelta(days=1)
    rig.tick()
    assert len(rig.logs.named("learning_account_untracked_position")) == 2
    assert rig.broker.sent("submit_close") == [] and rig.broker.sent("cancel") == []


def test_only_the_learning_broker_is_called(tmp_path) -> None:
    rig = _entered(tmp_path)
    rig.broker.fill_entry("NVDA", 100.0)
    rig.tick(60)
    rig.broker.fill_leg("NVDA", "stop", 95.0)
    rig.tick(60)
    # The main path's collaborators are Boom objects: any touch would have raised. The main
    # data client saw one quote, for the one entry.
    assert rig.provider.calls == [("get_quote", "NVDA")]
    assert json.loads(json.dumps(rig.closed()))[0]["symbol"] == "NVDA"
    assert Clock  # noqa: B018 - the fakes' clock is the only clock


def test_r_needs_an_entry_above_its_stop() -> None:
    assert r_multiple(95.0, 100.0, 95.0) is None and r_multiple(94.0, 100.0, 95.0) is None
    assert r_multiple(None, 100.0, 95.0) is None
    assert r_multiple(100.0, 97.5, 95.0) == -0.5


def test_no_leg_cancel_or_close_without_thirty_seconds_of_lease(tmp_path) -> None:
    rig = _entered(tmp_path, snapshot("NVDA", "vwap_reclaim", "5m"))
    rig.broker.fill_entry("NVDA", 100.0)
    rig.tick(60)
    rig.clock.value = CLOSE - timedelta(minutes=9)
    lease = rig.account.lease
    original = lease.ensure

    def short(work):
        held = original(work=work)
        lease.expires_at = rig.clock() + timedelta(seconds=29)
        return held

    lease.ensure = short
    rig.tick()
    assert _only(rig)["flatten_reason"] == "session_flatten"  # recorded, nothing sent
    assert rig.broker.sent("cancel") == [] and rig.broker.sent("submit_close") == []


def test_a_pending_flatten_waits_for_the_session_and_closes_only_what_is_held(tmp_path) -> None:
    rig = _entered(tmp_path, snapshot("NVDA", "vwap_reclaim", "5m"))
    rig.broker.fill_entry("NVDA", 100.0)
    rig.tick(60)
    rig.clock.value = CLOSE - timedelta(minutes=9)
    rig.tick()  # legs cancelled
    rig.broker.clock_value = {"is_open": False, "next_open": NEXT_OPEN, "next_close": CLOSE + timedelta(days=1)}
    rig.broker.held["NVDA"] = 30.0  # part of it went another way
    rig.tick(60)
    assert rig.broker.sent("submit_close") == []  # not while the market is closed
    assert rig.account.reconcile_at == rig.clock() + timedelta(seconds=60)  # a flatten is pending
    rig.broker.clock_value = {"is_open": True, "next_open": NEXT_OPEN + timedelta(days=1), "next_close": CLOSE + timedelta(days=1)}
    rig.clock.value = NEXT_OPEN + timedelta(minutes=1)
    rig.tick()
    assert [args[:2] for args in rig.broker.sent("submit_close")] == [("NVDA", 30.0)]


def test_a_close_the_broker_refuses_is_sent_again_with_the_next_id(tmp_path) -> None:
    from tests.learning_account_fakes import APIError

    rig = _entered(tmp_path)
    rig.broker.fill_entry("NVDA", 100.0)
    rig.tick(60)
    for leg in rig.broker.entry("NVDA")["legs"]:
        leg["status"] = "expired"
    rig.broker.close_error = APIError("insufficient qty", status_code=403)
    rig.tick(60)
    assert _only(rig)["close_attempt"] == 1
    rig.broker.close_error = TimeoutError("timeout")  # unknown: looked up next pass, same attempt
    rig.tick(60)
    assert _only(rig)["close_attempt"] == 1 and "close_order_id" not in _only(rig)


def test_unreadable_positions_never_mean_closed(tmp_path) -> None:
    rig = _entered(tmp_path)
    rig.broker.fill_entry("NVDA", 100.0)
    rig.tick(60)
    rig.broker.held.pop("NVDA")
    rig.broker.positions_error = RuntimeError("down")
    rig.tick(60)
    assert _only(rig)["status"] == "open"
    assert rig.logs.named("learning_account_untracked_position") == []


def test_tracked_positions_are_not_untracked(tmp_path) -> None:
    rig = _entered(tmp_path)
    rig.broker.fill_entry("NVDA", 100.0)
    rig.tick(15 * 60)
    assert rig.logs.named("learning_account_untracked_position") == []
    assert rig.account.positions_seen[0] == rig.clock()


def test_an_unchanged_open_trade_costs_no_book_write(tmp_path) -> None:
    rig = _entered(tmp_path)
    rig.broker.fill_entry("NVDA", 100.0)
    rig.tick(60)
    rig.store.writes.clear()
    rig.tick(60)
    assert records.BOOK_KEY not in rig.store.writes


def test_an_order_read_without_legs_is_no_proof_of_an_unprotected_position(tmp_path) -> None:
    rig = _entered(tmp_path)
    rig.broker.fill_entry("NVDA", 100.0)
    rig.tick(60)
    rig.broker.entry("NVDA")["legs"] = []
    rig.tick(60)
    assert "flatten_reason" not in _only(rig) and rig.broker.sent("submit_close") == []


def test_no_leg_cancel_or_close_while_stopping(tmp_path) -> None:
    rig = _entered(tmp_path, snapshot("NVDA", "vwap_reclaim", "5m"))
    rig.broker.fill_entry("NVDA", 100.0)
    rig.tick(60)
    rig.clock.value = CLOSE - timedelta(minutes=9)
    rig.account._stopping.set()
    rig.tick()
    assert _only(rig)["flatten_reason"] == "session_flatten"
    assert rig.broker.sent("cancel") == [] and rig.broker.sent("submit_close") == []


def test_the_flatten_window_is_the_setting_or_ten_minutes(tmp_path) -> None:
    from app.learning_account.reconcile import flatten_minutes

    assert flatten_minutes(Rig(tmp_path / "a", intraday_flatten_minutes_before_close=15.0).settings) == 15.0
    assert flatten_minutes(Rig(tmp_path / "b", intraday_flatten_minutes_before_close=0.0).settings) == 10.0


def test_no_close_without_thirty_seconds_of_lease_even_with_every_leg_ended(tmp_path) -> None:
    rig = _entered(tmp_path)
    rig.broker.fill_entry("NVDA", 100.0)
    rig.tick(60)
    for leg in rig.broker.entry("NVDA")["legs"]:
        leg["status"] = "expired"
    lease = rig.account.lease
    original = lease.ensure

    def short(work):
        held = original(work=work)
        lease.expires_at = rig.clock() + timedelta(seconds=29)
        return held

    lease.ensure = short
    rig.tick(60)
    assert _only(rig)["flatten_reason"] == "unprotected" and rig.broker.sent("submit_close") == []


def test_a_fill_seen_after_the_positions_read_is_not_mistaken_for_a_hand_close(tmp_path) -> None:
    rig = _entered(tmp_path)
    original = rig.broker.positions

    def before_the_fill():
        held = original()
        rig.broker.fill_entry("NVDA", 100.0)  # it fills between the positions read and the order read
        return held

    rig.broker.positions = before_the_fill
    rig.tick(60)
    trade = _only(rig)
    assert trade["status"] == "open" and trade["entry_fill"] == 100.0
