"""The learning account's records: compare-and-set writes, the single-owner lease, the
closed list, and two processes on one real SQLite runtime_state (app/learning_account/store.py)."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from app.learning_account import store as records
from app.learning_account.account import day_counts, ny_day
from tests.learning_account_fakes import NOW, Clock, CountingStore, Rig, database, settings_for, snapshot


@pytest.fixture
def store(tmp_path):
    return CountingStore(database(settings_for(tmp_path)))


def test_compare_and_set_inserts_only_when_absent_and_updates_only_the_text_read(store) -> None:
    assert store.cas("k", None, "one") is True
    assert store.cas("k", None, "two") is False  # present: ON CONFLICT DO NOTHING
    assert store.cas("k", "stale", "two") is False
    assert store.read("k") == "one"
    assert store.cas("k", "one", "two") is True and store.read("k") == "two"
    assert store.read_many([]) == {} and store.reads.count("k") == 2  # no IN () query
    assert store.read_many(["k", "missing"]) == {"k": "two", "missing": None}


def test_the_book_is_written_by_compare_and_set_and_a_lost_race_raises(store) -> None:
    book = records.load_book(store)
    assert book.text is None and book.trades == {}
    saved = records.save_book(store, book, {"t": {"trade_id": "t", "status": "open"}})
    assert records.load_book(store).trades == saved.trades
    assert records.save_book(store, saved, dict(saved.trades)) is saved  # nothing to write
    store.put(records.BOOK_KEY, records.dumps({"trades": {}}))  # someone else wrote it
    with pytest.raises(records.BookConflict):
        records.save_book(store, saved, {})
    with pytest.raises(records.BookConflict):
        records.save_book(store, book, {"x": {}})  # it is no longer absent
    store.put(records.BOOK_KEY, "{not json")
    with pytest.raises(records.BookUnreadable):
        records.load_book(store)
    store.put(records.BOOK_KEY, json.dumps({"trades": ["a"]}))
    with pytest.raises(records.BookUnreadable):
        records.load_book(store)


def test_the_closed_list_appends_once_per_trade_and_keeps_the_newest_500(store) -> None:
    assert records.append_closed(store, {"trade_id": "a"}) == records.ADDED
    assert records.append_closed(store, {"trade_id": "a", "r": 9}) == records.PRESENT
    assert json.loads(store.read(records.CLOSED_KEY)) == [{"trade_id": "a"}]
    store.put(records.CLOSED_KEY, records.dumps([{"trade_id": str(i)} for i in range(500)]))
    assert records.append_closed(store, {"trade_id": "new"}) == records.ADDED
    kept = json.loads(store.read(records.CLOSED_KEY))
    assert len(kept) == records.CLOSED_LIMIT == 500 and kept[0]["trade_id"] == "1" and kept[-1]["trade_id"] == "new"
    store.put(records.CLOSED_KEY, "{broken")
    assert records.append_closed(store, {"trade_id": "b"}) == ""
    store.put(records.CLOSED_KEY, json.dumps({"not": "a list"}))
    assert records.append_closed(store, {"trade_id": "b"}) == ""
    store.put(records.CLOSED_KEY, "[]")
    store.fail_cas.add(records.CLOSED_KEY)
    assert records.append_closed(store, {"trade_id": "b"}) == ""  # a one-off write failure
    assert records.append_closed(store, {"trade_id": "b"}) == records.ADDED


def test_the_lease_has_one_owner_and_is_read_only_after_its_expiry(store) -> None:
    clock = Clock()
    mine = records.Lease(store, "a", clock)
    theirs = records.Lease(store, "b", clock)
    assert mine.ensure(work=True) and store.reads == []  # an absent row is inserted, no read
    assert not theirs.ensure(work=True) and store.reads == [records.LEASE_KEY]
    for _ in range(4):  # within its expiry: no read at all
        clock.advance(60)
        assert not theirs.ensure(work=True)
    assert store.reads == [records.LEASE_KEY]
    clock.advance(61)  # 301 s: the holder has not renewed: expired
    assert theirs.ensure(work=True) and store.reads == [records.LEASE_KEY] * 2
    assert not mine.ensure(work=True)  # its renewal is a conditional update: lost
    assert not mine.held and not mine.may_act()
    row = json.loads(store.read(records.LEASE_KEY))
    assert row["owner"] == "b" and row["heartbeat_at"] == clock().isoformat()
    assert row["expires_at"] == (clock() + timedelta(seconds=records.LEASE_SECONDS)).isoformat()


def test_the_lease_is_renewed_only_when_under_two_minutes_remain_or_there_is_work(store) -> None:
    clock = Clock()
    lease = records.Lease(store, "a", clock)
    assert lease.ensure(work=False)
    writes = len(store.writes)
    clock.advance(180)  # exactly 2 min left: not under
    assert lease.ensure(work=False) and len(store.writes) == writes
    assert lease.ensure(work=True) and len(store.writes) == writes + 1  # work renews it
    clock.advance(181)
    assert lease.ensure(work=False) and len(store.writes) == writes + 2
    assert store.reads == []  # a held lease is never read
    assert lease.may_act()
    clock.advance(271)  # 29 s left
    assert not lease.may_act() and lease.held
    assert not lease.may_act(stopping=True)


def test_a_released_or_unreadable_lease_is_taken_at_once(store) -> None:
    clock = Clock()
    first, second = records.Lease(store, "a", clock), records.Lease(store, "b", clock)
    assert first.ensure(work=True) and not second.ensure(work=True)
    first.release()
    assert not first.held and json.loads(store.read(records.LEASE_KEY))["released"] is True
    second._foreign_until = None  # it would otherwise wait for the expiry it read
    assert second.ensure(work=True)
    store.put(records.LEASE_KEY, "garbage")
    third = records.Lease(store, "c", clock)
    assert third.ensure(work=True)


def test_ten_idle_ticks_cost_one_read_and_four_writes(tmp_path) -> None:
    rig = Rig(tmp_path)
    rig.store.reads.clear()
    rig.store.writes.clear()
    for index in range(10):
        rig.tick(60 if index else 0)
    # The book once (the first reconcile), the status once (verified), the lease taken once and
    # renewed twice (under 2 min left at 4 and 8 min). Nothing else touches the database.
    assert rig.store.reads == [records.BOOK_KEY]
    assert rig.store.writes == [records.STATUS_KEY, records.LEASE_KEY, records.LEASE_KEY, records.LEASE_KEY]


def _second(rig, owner="host-b:2:bbbb"):
    return Rig(None, settings=rig.settings, db=rig.db, broker=rig.broker, clock=rig.clock, owner=owner, seed=False)


def test_two_accounts_on_one_state_never_exceed_three_a_day_nor_lose_a_trade(tmp_path) -> None:
    a = Rig(tmp_path)
    b = _second(a)
    a.tick()
    b.tick()  # verified, but the lease is a's: no pass
    assert a.account.lease.held and not b.account.lease.held
    a.offer(snapshot("NVDA"), snapshot("AMD", "ma_crossover"))
    a.tick(5)
    b.offer(snapshot("MU"), snapshot("GOOG", "ma_crossover"))
    b.tick(5)
    assert sorted(t["symbol"] for t in a.book().values()) == ["AMD", "NVDA"]
    # a stops ticking (a crashed process keeps its row): b takes over only after the expiry.
    b.clock.advance(records.LEASE_SECONDS)
    b.offer(snapshot("MU"), snapshot("GOOG", "ma_crossover"), snapshot("GOOGL", "ma_crossover"))
    b.tick(1)
    assert b.account.lease.held
    trades = b.book()
    assert day_counts(trades, ny_day(NOW))["entries"] == 3
    assert len(a.broker.sent("submit_bracket")) == 3  # one order per trade in the book
    assert {args[4] for args in a.broker.sent("submit_bracket")} == {t["client_order_id"] for t in trades.values()}
    # a comes back: its renewal is a conditional update on the row it last wrote, and fails.
    a.offer(snapshot("GOOGL", "ma_crossover"))
    a.tick(1)
    assert not a.account.lease.held and len(a.broker.sent("submit_bracket")) == 3


def test_a_book_race_between_two_holders_loses_no_trade_and_sends_no_extra_order(tmp_path) -> None:
    a = Rig(tmp_path)
    b = _second(a)
    a.tick()
    b.account.route = "running"  # as if b believed it held the lease too (a stale clock)
    b.account.lease._text, b.account.lease.expires_at = "x", b.clock() + timedelta(minutes=5)
    b.account.lease.ensure = lambda work: True

    def b_enters_first(key):
        if key == records.BOOK_KEY and not b.book():
            a.store.before_cas = None
            b.offer(snapshot("MU"))
            b.tick()

    a.store.before_cas = b_enters_first
    a.offer(snapshot("NVDA"))
    a.tick(5)
    trades = a.book()
    assert [t["symbol"] for t in trades.values()] == ["MU"]  # a's write lost the race: no order
    assert [args[0] for args in a.broker.sent("submit_bracket")] == ["MU"]
    a.offer(snapshot("NVDA"))
    a.tick(5)  # the next pass reads the book again and enters
    assert sorted(t["symbol"] for t in a.book().values()) == ["MU", "NVDA"]


def test_the_record_constants() -> None:
    assert (records.LEASE_SECONDS, records.RENEW_BELOW_SECONDS, records.ACT_MIN_SECONDS) == (300.0, 120.0, 30.0)
    assert records.CLOSED_LIMIT == 500
    assert records.LEARNING_KEYS == (
        "learning_account:book", "learning_account:closed", "learning_account:status", "learning_account:lease",
        "strategy_evidence:learning_verdicts",
    )


def test_an_empty_read_never_reaches_the_database() -> None:
    class NoDatabase:
        def connect(self):
            raise AssertionError("Postgres rejects IN ()")

    assert records.Store(NoDatabase()).read_many([]) == {}


def test_its_own_unexpired_row_is_taken_back_and_an_unchanged_book_is_not_written(store) -> None:
    clock = Clock()
    store.put(records.LEASE_KEY, records.dumps({"owner": "a", "expires_at": (clock() + timedelta(minutes=4)).isoformat()}))
    lease = records.Lease(store, "a", clock)
    assert lease.ensure(work=True) and lease.held
    other = records.Lease(store, "b", clock)
    assert not other.ensure(work=True)
    book = records.save_book(store, records.load_book(store), {"t": {"trade_id": "t"}})
    writes = len(store.writes)
    assert records.save_book(store, book, {"t": {"trade_id": "t"}}) is book and len(store.writes) == writes


def test_nothing_may_act_while_stopping(store) -> None:
    lease = records.Lease(store, "a", Clock())
    assert lease.ensure(work=True)
    assert lease.may_act() and lease.may_act(stopping=False)
    assert not lease.may_act(stopping=True)


def test_the_status_is_written_only_when_the_route_changes(tmp_path) -> None:
    rig = Rig(tmp_path)
    for index in range(40):  # 40 idle minutes: the identity is read again every 15 min
        rig.tick(60 if index else 0)
    assert rig.store.writes.count(records.STATUS_KEY) == 1
    assert len(rig.broker.sent("identity")) == 3
