"""Time utilities."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def utc_now() -> datetime:
    """Return the current UTC timestamp."""

    return datetime.now(UTC)


def isoformat_utc(value: datetime) -> str:
    """Return an ISO-8601 UTC string."""

    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def add_minutes(value: datetime, minutes: int) -> datetime:
    """Return a timestamp offset by a number of minutes."""

    return value + timedelta(minutes=minutes)
