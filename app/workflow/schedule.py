"""Schedule and timing helpers for the workflow service."""

from __future__ import annotations

from datetime import UTC, datetime, time
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.utils.time import utc_now


def is_due(service: Any, state_key: str, interval_minutes: int) -> bool:
    last = service.runtime_state.get(state_key)
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return True
    return (utc_now() - last_dt).total_seconds() >= max(interval_minutes, 1) * 60


def daily_summary_due(service: Any) -> bool:
    last = service.runtime_state.get("workflow:last_daily_summary_at")
    now = utc_now()
    if now.hour < int(service.settings.daily_summary_hour_utc):
        return False
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return True
    return last_dt.astimezone(UTC).date() < now.astimezone(UTC).date()


def ledger_cycle_due(service: Any) -> bool:
    if service.ledger_service is None:
        return False
    if not bool(getattr(service.settings, "ledger_enabled", False)):
        return False
    if not bool(getattr(service.settings, "ledger_cycle_enabled", False)):
        return False
    return service._is_due(
        "workflow:last_ledger_cycle_at",
        int(getattr(service.settings, "ledger_cycle_interval_minutes", 15)),
    )


def last_successful_screener_run_at(service: Any) -> str | None:
    values = [
        service.runtime_state.get(key)
        for key in (
            "workflow:last_premarket_scan_at",
            "workflow:last_market_open_scan_at",
            "workflow:last_intelligent_scan_at",
            "workflow:last_swing_scan_at",
            "workflow:last_intraday_scan_at",
            "workflow:last_end_of_day_scan_at",
        )
    ]
    parsed: list[datetime] = []
    for value in values:
        if not value:
            continue
        try:
            parsed.append(datetime.fromisoformat(value))
        except ValueError:
            continue
    if not parsed:
        return None
    return max(parsed).isoformat()


def named_scan_due(service: Any, state_key: str, enabled: bool, scheduled_time: str) -> bool:
    if not enabled:
        return False
    now_local = service._local_now()
    due_at = service._combine_local_time(now_local, scheduled_time)
    if now_local < due_at:
        return False
    last = service.runtime_state.get(state_key)
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return True
    return last_dt.astimezone(service._schedule_zone()).date() < now_local.date()


def intraday_scan_due(service: Any) -> bool:
    if not service.settings.intraday_repeated_scan_enabled:
        return False
    now_local = service._local_now()
    start = service._combine_local_time(now_local, service.settings.intraday_scan_start_local)
    end = service._combine_local_time(now_local, service.settings.intraday_scan_end_local)
    if now_local < start or now_local > end:
        return False
    return service._is_due("workflow:last_intraday_scan_at", service.settings.intraday_scan_interval_minutes)


def intelligent_scan_due(service: Any) -> bool:
    if not service.settings.intelligent_scan_enabled:
        return False
    now_local = service._local_now()
    start = service._combine_local_time(now_local, service.settings.intelligent_scan_start_local)
    end = service._combine_local_time(now_local, service.settings.intelligent_scan_end_local)
    if now_local < start or now_local > end:
        return False
    return service._is_due("workflow:last_intelligent_scan_at", service.settings.intelligent_scan_interval_minutes)


def local_now(service: Any) -> datetime:
    return utc_now().astimezone(service._schedule_zone())


def schedule_zone(service: Any) -> ZoneInfo:
    try:
        return ZoneInfo(service.settings.schedule_timezone)
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def combine_local_time(service: Any, current: datetime, raw_time: str) -> datetime:
    hour, minute = service._parse_time(raw_time)
    return datetime.combine(current.date(), time(hour=hour, minute=minute), tzinfo=current.tzinfo)


def parse_time(raw_time: str) -> tuple[int, int]:
    try:
        hour_raw, minute_raw = raw_time.strip().split(":", 1)
        return max(0, min(int(hour_raw), 23)), max(0, min(int(minute_raw), 59))
    except Exception:
        return 0, 0
