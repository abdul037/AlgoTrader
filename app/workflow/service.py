"""Scheduled scan workflow, tracked signal monitoring, and summaries."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.models.workflow import WorkflowStatusResponse, WorkflowTaskResponse
from app.utils.time import utc_now
from app.workflow.operations import (
    candidate_with_ledger_outcome,
    check_open_signals_impl,
    close_status,
    copy_candidate_with_metadata,
    ledger_alert_payload,
    run_ledger_cycle_impl,
    run_scan_task,
    send_daily_summary_impl,
    send_scan_alerts,
    track_candidates,
)
from app.workflow.schedule import (
    combine_local_time,
    daily_summary_due,
    intelligent_scan_due,
    intraday_scan_due,
    is_due,
    last_successful_screener_run_at,
    ledger_cycle_due,
    local_now,
    named_scan_due,
    parse_time,
    schedule_zone,
)


class LedgerRecordingError(RuntimeError):
    """Raised when we cannot reliably record an alert in the ledger."""


class SignalWorkflowService:
    """Coordinate scheduled scans, tracked open signals, and daily summaries."""

    LedgerRecordingError = LedgerRecordingError

    def __init__(
        self,
        *,
        settings: Any,
        market_screener: Any,
        market_data_engine: Any,
        notifier: Any,
        tracked_signals: Any,
        alert_history: Any,
        runtime_state: Any,
        run_logs: Any,
        ledger_service: Any | None = None,
    ):
        self.settings = settings
        self.market_screener = market_screener
        self.market_data = market_data_engine
        self.notifier = notifier
        self.tracked_signals = tracked_signals
        self.alert_history = alert_history
        self.runtime_state = runtime_state
        self.run_logs = run_logs
        self.ledger_service = ledger_service

    def run_scheduled_tasks(self) -> dict[str, int]:
        summary = {"alerts_sent": 0, "closed_signals": 0, "ledger_cycles": 0}
        if self._ledger_cycle_due():
            result = self.run_ledger_cycle()
            if result.status == "ok":
                summary["ledger_cycles"] += 1

        if not self.settings.screener_scheduler_enabled:
            return summary

        if self._named_scan_due("workflow:last_premarket_scan_at", self.settings.premarket_scan_enabled, self.settings.premarket_scan_time_local):
            result = self.run_premarket_scan(notify=True, force_refresh=True)
            summary["alerts_sent"] += result.alerts_sent

        if self._named_scan_due("workflow:last_market_open_scan_at", self.settings.market_open_scan_enabled, self.settings.market_open_scan_time_local):
            result = self.run_market_open_scan(notify=True, force_refresh=True)
            summary["alerts_sent"] += result.alerts_sent

        if self._intelligent_scan_due():
            result = self.run_intelligent_scan(notify=True, force_refresh=False)
            summary["alerts_sent"] += result.alerts_sent

        if self._intraday_scan_due():
            result = self.run_intraday_scan(notify=True, force_refresh=False)
            summary["alerts_sent"] += result.alerts_sent

        if self._is_due("workflow:last_open_signal_check_at", self.settings.open_signal_check_interval_minutes):
            result = self.check_open_signals(notify=True)
            summary["alerts_sent"] += result.alerts_sent
            summary["closed_signals"] += result.closed_signals

        if self._named_scan_due("workflow:last_end_of_day_scan_at", self.settings.end_of_day_scan_enabled, self.settings.end_of_day_scan_time_local):
            result = self.run_end_of_day_scan(notify=True, force_refresh=True)
            summary["alerts_sent"] += result.alerts_sent

        if self._daily_summary_due():
            result = self.send_daily_summary(notify=True)
            summary["alerts_sent"] += result.alerts_sent
        return summary

    def run_premarket_scan(self, *, notify: bool = True, force_refresh: bool = False) -> WorkflowTaskResponse:
        return self._execute_guarded(
            "premarket_scan",
            lambda: self._run_scan_task(
                task="premarket_scan",
                state_key="workflow:last_premarket_scan_at",
                origin="premarket_scan",
                timeframes=list(self.settings.screener_default_timeframes),
                notify=notify,
                force_refresh=force_refresh,
            ),
        )

    def run_market_open_scan(self, *, notify: bool = True, force_refresh: bool = False) -> WorkflowTaskResponse:
        return self._execute_guarded(
            "market_open_scan",
            lambda: self._run_scan_task(
                task="market_open_scan",
                state_key="workflow:last_market_open_scan_at",
                origin="market_open_scan",
                timeframes=list(self.settings.screener_intraday_timeframes),
                notify=notify,
                force_refresh=force_refresh,
            ),
        )

    def run_swing_scan(self, *, notify: bool = True, force_refresh: bool = False) -> WorkflowTaskResponse:
        return self._execute_guarded(
            "swing_scan",
            lambda: self._run_scan_task(
                task="swing_scan",
                state_key="workflow:last_swing_scan_at",
                origin="swing_scan",
                timeframes=list(self.settings.screener_default_timeframes),
                notify=notify,
                force_refresh=force_refresh,
            ),
        )

    def run_intelligent_scan(self, *, notify: bool = True, force_refresh: bool = False) -> WorkflowTaskResponse:
        return self._execute_guarded(
            "intelligent_scan",
            lambda: self._run_scan_task(
                task="intelligent_scan",
                state_key="workflow:last_intelligent_scan_at",
                origin="intelligent_scan",
                timeframes=list(self.settings.intelligent_scan_timeframes),
                notify=notify,
                force_refresh=force_refresh,
            ),
        )

    def run_intraday_scan(self, *, notify: bool = True, force_refresh: bool = False) -> WorkflowTaskResponse:
        return self._execute_guarded(
            "intraday_scan",
            lambda: self._run_scan_task(
                task="intraday_scan",
                state_key="workflow:last_intraday_scan_at",
                origin="intraday_scan",
                timeframes=list(self.settings.screener_intraday_timeframes),
                notify=notify,
                force_refresh=force_refresh,
            ),
        )

    def run_end_of_day_scan(self, *, notify: bool = True, force_refresh: bool = False) -> WorkflowTaskResponse:
        return self._execute_guarded(
            "end_of_day_scan",
            lambda: self._run_scan_task(
                task="end_of_day_scan",
                state_key="workflow:last_end_of_day_scan_at",
                origin="end_of_day_scan",
                timeframes=list(self.settings.screener_default_timeframes),
                notify=notify,
                force_refresh=force_refresh,
            ),
        )

    def check_open_signals(self, *, notify: bool = True, force_refresh: bool = False) -> WorkflowTaskResponse:
        return self._execute_guarded(
            "open_signal_check",
            lambda: self._check_open_signals_impl(notify=notify, force_refresh=force_refresh),
        )

    def send_daily_summary(self, *, notify: bool = True) -> WorkflowTaskResponse:
        return self._execute_guarded(
            "daily_summary",
            lambda: self._send_daily_summary_impl(notify=notify),
        )

    def run_ledger_cycle(self) -> WorkflowTaskResponse:
        return self._execute_guarded(
            "ledger_cycle",
            self._run_ledger_cycle_impl,
        )

    def status(self) -> WorkflowStatusResponse:
        return WorkflowStatusResponse(
            scheduler_enabled=bool(self.settings.screener_scheduler_enabled),
            schedule_timezone=self.settings.schedule_timezone,
            last_premarket_scan_at=self.runtime_state.get("workflow:last_premarket_scan_at"),
            last_market_open_scan_at=self.runtime_state.get("workflow:last_market_open_scan_at"),
            last_intelligent_scan_at=self.runtime_state.get("workflow:last_intelligent_scan_at"),
            last_swing_scan_at=self.runtime_state.get("workflow:last_swing_scan_at"),
            last_intraday_scan_at=self.runtime_state.get("workflow:last_intraday_scan_at"),
            last_end_of_day_scan_at=self.runtime_state.get("workflow:last_end_of_day_scan_at"),
            last_open_signal_check_at=self.runtime_state.get("workflow:last_open_signal_check_at"),
            last_ledger_cycle_at=self.runtime_state.get("workflow:last_ledger_cycle_at"),
            last_daily_summary_at=self.runtime_state.get("workflow:last_daily_summary_at"),
            open_signals=len(self.tracked_signals.list(status="open", limit=500)),
            alert_history_count=self.alert_history.count(),
        )

    def health_summary(self) -> dict[str, Any]:
        pending_count = 0
        stale_pending_count = 0
        if self.ledger_service is not None:
            repository = getattr(self.ledger_service, "repository", None)
            if repository is not None:
                pending_count = int(repository.pending_match_count())
                stale_pending_count = int(repository.pending_match_older_than_count(hours=24))

        last_etoro_error = self.runtime_state.get("etoro:last_api_error")
        last_etoro_error_at = self.runtime_state.get("etoro:last_api_error_at")
        status = "ok"
        reasons: list[str] = []
        if stale_pending_count > 0:
            status = "warning"
            reasons.append("stale_pending_matches")
        if last_etoro_error:
            status = "warning"
            reasons.append("etoro_api_errors")
        return {
            "status": status,
            "reasons": reasons,
            "scheduler_enabled": bool(self.settings.screener_scheduler_enabled),
            "ledger_enabled": bool(getattr(self.settings, "ledger_enabled", False)),
            "last_successful_screener_run_at": self._last_successful_screener_run_at(),
            "last_etoro_error": last_etoro_error,
            "last_etoro_error_at": last_etoro_error_at,
            "pending_match_count": pending_count,
            "stale_pending_match_count": stale_pending_count,
        }

    def _run_scan_task(self, **kwargs: Any) -> WorkflowTaskResponse:
        return run_scan_task(self, **kwargs)

    def _track_candidates(self, response: Any, *, origin: str) -> None:
        track_candidates(self, response, origin=origin)

    @staticmethod
    def _close_status(snapshot: Any, price: float) -> str | None:
        return close_status(snapshot, price)

    def _is_due(self, state_key: str, interval_minutes: int) -> bool:
        return is_due(self, state_key, interval_minutes)

    def _daily_summary_due(self) -> bool:
        return daily_summary_due(self)

    def _ledger_cycle_due(self) -> bool:
        return ledger_cycle_due(self)

    def _check_open_signals_impl(self, *, notify: bool, force_refresh: bool) -> WorkflowTaskResponse:
        return check_open_signals_impl(self, notify=notify, force_refresh=force_refresh)

    def _send_daily_summary_impl(self, *, notify: bool) -> WorkflowTaskResponse:
        return send_daily_summary_impl(self, notify=notify)

    def _send_scan_alerts(self, *, task: str, response: Any, notify: bool) -> int:
        return send_scan_alerts(self, task=task, response=response, notify=notify)

    def _candidate_with_ledger_outcome(self, *, task: str, candidate: Any) -> Any:
        return candidate_with_ledger_outcome(self, task=task, candidate=candidate)

    @staticmethod
    def _ledger_alert_payload(
        *,
        task: str,
        candidate: Any,
        generated_at: str,
        target: float | None,
    ) -> dict[str, Any]:
        return ledger_alert_payload(task=task, candidate=candidate, generated_at=generated_at, target=target)

    @staticmethod
    def _copy_candidate_with_metadata(candidate: Any, metadata_updates: dict[str, Any]) -> Any:
        return copy_candidate_with_metadata(candidate, metadata_updates)

    def _run_ledger_cycle_impl(self) -> WorkflowTaskResponse:
        return run_ledger_cycle_impl(self)

    def _execute_guarded(self, task: str, runner) -> WorkflowTaskResponse:
        if not self._acquire_lock(task):
            return WorkflowTaskResponse(
                task=task,
                status="skipped",
                detail=f"{task.replace('_', ' ').title()} skipped because a prior run is still active.",
                skipped=True,
            )
        started_at = utc_now().isoformat()
        self.run_logs.log(f"workflow_{task}_started", {"started_at": started_at})
        try:
            return runner()
        except Exception as exc:
            self.run_logs.log(f"workflow_{task}_error", {"error": str(exc)})
            return WorkflowTaskResponse(
                task=task,
                status="error",
                detail=f"{task.replace('_', ' ').title()} failed: {exc}",
                errors=[str(exc)],
            )
        finally:
            self.runtime_state.set(self._lock_key(task), "")

    def _acquire_lock(self, task: str) -> bool:
        lock_key = self._lock_key(task)
        current = self.runtime_state.get(lock_key)
        if current:
            try:
                started_at = datetime.fromisoformat(current)
            except ValueError:
                started_at = None
            if started_at is not None and (utc_now() - started_at) < timedelta(minutes=max(int(self.settings.workflow_lock_timeout_minutes), 1)):
                return False
        self.runtime_state.set(lock_key, utc_now().isoformat())
        return True

    @staticmethod
    def _lock_key(task: str) -> str:
        return f"workflow:lock:{task}"

    def _last_successful_screener_run_at(self) -> str | None:
        return last_successful_screener_run_at(self)

    def _named_scan_due(self, state_key: str, enabled: bool, scheduled_time: str) -> bool:
        return named_scan_due(self, state_key, enabled, scheduled_time)

    def _intraday_scan_due(self) -> bool:
        return intraday_scan_due(self)

    def _intelligent_scan_due(self) -> bool:
        return intelligent_scan_due(self)

    def _local_now(self) -> datetime:
        return local_now(self)

    def _schedule_zone(self):
        return schedule_zone(self)

    def _combine_local_time(self, current: datetime, raw_time: str) -> datetime:
        return combine_local_time(self, current, raw_time)

    @staticmethod
    def _parse_time(raw_time: str) -> tuple[int, int]:
        return parse_time(raw_time)
