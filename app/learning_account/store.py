"""The learning account's records: runtime_state keys only, written by compare-and-set.

Every write that two processes could race (the book, the closed list, the lease) is a
compare-and-set on the exact text this process read: one conditional ``UPDATE ... WHERE
state_value = <that text>``, or ``INSERT ... ON CONFLICT DO NOTHING`` when the row is absent.
Its ``rowcount`` is read inside the connection block, before the commit, on SQLite and on
Postgres alike (``Database.connect``; keys are bound parameters, never inlined: a ``:`` in the
SQL would parse as a bind under Postgres). A lost race raises ``BookConflict`` for the book,
and the pass ends without an order.

The lease (``Lease``) is a single row: owner (host:pid:random), heartbeat and a 5-minute
expiry. It is taken by compare-and-set, renewed only when under ``RENEW_BELOW_SECONDS`` remain
or the pass has work, as one conditional UPDATE on the row as this owner last wrote it (no
read), and a lease this process does not hold is read again only after its expiry. Nothing
acts without it: every submit, leg cancel and close asks ``may_act`` first.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
from datetime import datetime, timedelta
from typing import Any

from app.utils.time import utc_now

BOOK_KEY = "learning_account:book"
CLOSED_KEY = "learning_account:closed"
STATUS_KEY = "learning_account:status"
LEASE_KEY = "learning_account:lease"
LEARNING_VERDICTS_KEY = "strategy_evidence:learning_verdicts"
LEARNING_KEYS = (BOOK_KEY, CLOSED_KEY, STATUS_KEY, LEASE_KEY, LEARNING_VERDICTS_KEY)
CLOSED_LIMIT = 500  # newest closed records kept
LEASE_SECONDS = 300.0
RENEW_BELOW_SECONDS = 120.0
ACT_MIN_SECONDS = 30.0  # lease time an order, a leg cancel or a close needs left
PROCESS_OWNER = f"{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(4)}"


class BookConflict(Exception):
    """Another writer changed the book since this pass read it: the pass ends, no order."""


class BookUnreadable(Exception):
    """The book could not be read or parsed: no entries and no reconcile this pass."""


class Store:
    """runtime_state access for the learning keys (``db`` is the app's ``Database``)."""

    def __init__(self, db: Any):
        self.db = db

    def read(self, key: str) -> str | None:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT state_value FROM runtime_state WHERE state_key = ?", (key,)
            ).fetchone()
        return None if row is None else str(row["state_value"])

    def read_many(self, keys: list[str] | tuple[str, ...]) -> dict[str, str | None]:
        keys = list(dict.fromkeys(keys))
        if not keys:  # Postgres rejects IN ()
            return {}
        marks = ", ".join("?" for _ in keys)
        with self.db.connect() as connection:
            rows = connection.execute(
                f"SELECT state_key, state_value FROM runtime_state WHERE state_key IN ({marks})",  # noqa: S608
                tuple(keys),
            ).fetchall()
        found = {str(row["state_key"]): str(row["state_value"]) for row in rows}
        return {key: found.get(key) for key in keys}

    def cas(self, key: str, expected: str | None, value: str) -> bool:
        """Write ``value`` only if the row still holds ``expected`` (None: only if absent)."""

        stamp = utc_now().isoformat()
        with self.db.connect() as connection:
            if expected is None:
                cursor = connection.execute(
                    "INSERT INTO runtime_state (state_key, state_value, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(state_key) DO NOTHING",
                    (key, value, stamp),
                )
            else:
                cursor = connection.execute(
                    "UPDATE runtime_state SET state_value = ?, updated_at = ? "
                    "WHERE state_key = ? AND state_value = ?",
                    (value, stamp, key, expected),
                )
            return int(cursor.rowcount or 0) == 1  # read before the commit

    def put(self, key: str, value: str) -> None:
        """An unconditional write (the status and the learning verdicts: one writer, no race)."""

        with self.db.connect() as connection:
            connection.execute(
                "INSERT INTO runtime_state (state_key, state_value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(state_key) DO UPDATE SET state_value = excluded.state_value, "
                "updated_at = excluded.updated_at",
                (key, value, utc_now().isoformat()),
            )


def dumps(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


class Book:
    """The learning account's trades as one row, and the exact text they were read from."""

    def __init__(self, text: str | None, trades: dict[str, dict[str, Any]]):
        self.text = text
        self.trades = trades


def load_book(store: Store) -> Book:
    try:
        text = store.read(BOOK_KEY)
        payload = json.loads(text) if text else {"trades": {}}
        trades = payload.get("trades") if isinstance(payload, dict) else None
    except Exception as exc:  # noqa: BLE001 - unreadable: the pass does nothing
        raise BookUnreadable(f"{type(exc).__name__}: {exc}"[:200]) from exc
    if not isinstance(trades, dict) or not all(isinstance(t, dict) for t in trades.values()):
        raise BookUnreadable("book_malformed")
    return Book(text, trades)


def save_book(store: Store, book: Book, trades: dict[str, dict[str, Any]]) -> Book:
    """Compare-and-set the book on the text ``book`` was read from; ``BookConflict`` if lost."""

    text = dumps({"trades": trades})
    if text == book.text:
        return book
    if not store.cas(BOOK_KEY, book.text, text):
        raise BookConflict("learning_account_book_changed")
    return Book(text, trades)


ADDED, PRESENT = "added", "present"


def append_closed(store: Store, record: dict[str, Any]) -> str:
    """Append one closed record, idempotent per trade id: ``ADDED``, ``PRESENT`` (already
    there), or "" when the list cannot be read or written (the trade then stays
    ``closed_unreported`` and is appended next pass)."""

    try:
        text = store.read(CLOSED_KEY)
        items = json.loads(text) if text else []
        if not isinstance(items, list):
            return ""
        trade_id = record.get("trade_id")
        if any(isinstance(item, dict) and item.get("trade_id") == trade_id for item in items):
            return PRESENT
        return ADDED if store.cas(CLOSED_KEY, text, dumps([*items, record][-CLOSED_LIMIT:])) else ""
    except Exception:  # noqa: BLE001 - retried next pass
        return ""


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value)) if value else None
    except (TypeError, ValueError):
        return None
    return parsed if parsed is not None and parsed.tzinfo is not None else None


class Lease:
    """This process's hold on ``LEASE_KEY`` (``now`` gives aware UTC datetimes)."""

    def __init__(self, store: Store, owner: str, now: Any):
        self.store = store
        self.owner = owner
        self.now = now
        self._text: str | None = None  # the row as this owner last wrote it, while held
        self.expires_at: datetime | None = None
        self._foreign_until: datetime | None = None  # another owner's lease: no read before
        self._row_seen = False  # once seen, taking it needs the exact text of the row

    @property
    def held(self) -> bool:
        return self._text is not None

    def _row(self, now: datetime, *, ttl: float = LEASE_SECONDS) -> tuple[str, datetime]:
        expires = now + timedelta(seconds=ttl)
        text = dumps({"owner": self.owner, "heartbeat_at": now.isoformat(), "expires_at": expires.isoformat()})
        return text, expires

    def ensure(self, *, work: bool) -> bool:
        """Hold the lease for this tick (taking or renewing it as needed); False: no pass."""

        now = self.now()
        if self._text is not None:
            if not work and self.expires_at is not None and (self.expires_at - now).total_seconds() >= RENEW_BELOW_SECONDS:
                return True
            text, expires = self._row(now)
            if self.store.cas(LEASE_KEY, self._text, text):
                self._text, self.expires_at = text, expires
                return True
            self._text, self.expires_at, self._row_seen = None, None, True  # lost: read it next
            return False
        if self._foreign_until is not None and now < self._foreign_until:
            return False
        return self._take(now)

    def _take(self, now: datetime) -> bool:
        text, expires = self._row(now)
        if not self._row_seen:
            if self.store.cas(LEASE_KEY, None, text):
                self._text, self.expires_at, self._foreign_until = text, expires, None
                return True
            self._row_seen = True
        current = self.store.read(LEASE_KEY)
        if current is None:  # deleted meanwhile: insert again next tick
            self._row_seen = False
            return False
        try:
            row = json.loads(current)
        except (TypeError, ValueError):
            row = {}
        until = _parse_time(row.get("expires_at")) if isinstance(row, dict) else None
        if until is not None and until > now and (not isinstance(row, dict) or row.get("owner") != self.owner):
            self._foreign_until = until
            return False
        if self.store.cas(LEASE_KEY, current, text):  # expired, unreadable or our own old row
            self._text, self.expires_at, self._foreign_until = text, expires, None
            return True
        return False

    def may_act(self, stopping: bool = False) -> bool:
        """Whether an order, a leg cancel or a close may be sent now."""

        return (
            not stopping
            and self._text is not None
            and self.expires_at is not None
            and (self.expires_at - self.now()).total_seconds() >= ACT_MIN_SECONDS
        )

    def release(self) -> None:
        """Give the lease up (an expired row the next owner may take at once)."""

        if self._text is None:
            return
        now = self.now()
        text = dumps({"owner": self.owner, "heartbeat_at": now.isoformat(), "expires_at": now.isoformat(), "released": True})
        try:
            self.store.cas(LEASE_KEY, self._text, text)
        finally:
            self._text, self.expires_at = None, None
