"""Scheduled scan workflow, tracked signal monitoring, and summaries."""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.models.workflow import WorkflowStatusResponse, WorkflowTaskResponse
from app.universe import resolve_universe
from app.utils.time import utc_now


class SignalWorkflowService:
    """Coordinate scheduled scans, tracked open signals, and daily summaries."""

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
        """Run any due scheduled scans and checks."""

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

    def _run_scan_task(
        self,
        *,
        task: str,
        state_key: str,
        origin: str,
        timeframes: list[str],
        notify: bool,
        force_refresh: bool,
    ) -> WorkflowTaskResponse:
        kwargs = {
            "symbols": resolve_universe(
                self.settings,
                limit=int(getattr(self.settings, "workflow_scan_default_universe_limit", 25) or 25),
            ),
            "timeframes": timeframes,
            "limit": self.settings.screener_top_k,
            "validated_only": self.settings.require_backtest_validation_for_alerts,
            "notify": False,
            "force_refresh": force_refresh,
        }
        if "scan_task" in inspect.signature(self.market_screener.scan_universe).parameters:
            kwargs["scan_task"] = task
        response = self.market_screener.scan_universe(**kwargs)
        alerts_sent = self._send_scan_alerts(task=task, response=response, notify=notify)
        self._track_candidates(response, origin=origin)
        self.runtime_state.set(state_key, utc_now().isoformat())
        self.run_logs.log(
            f"workflow_{task}_completed",
            {
                "started_at": utc_now().isoformat(),
                "candidates": len(response.candidates),
                "symbols_scanned": response.evaluated_symbols,
                "symbols_passed": [item.symbol for item in response.candidates],
                "alerts_sent": alerts_sent,
                "timeframes": timeframes,
                "errors": response.errors,
            },
        )
        return WorkflowTaskResponse(
            task=task,
            status="ok",
            detail=f"{task.replace('_', ' ').title()} completed.",
            alerts_sent=alerts_sent,
            candidates=len(response.candidates),
            open_signals=len(self.tracked_signals.list(status="open", limit=500)),
            errors=list(response.errors),
        )

    def _track_candidates(self, response: Any, *, origin: str) -> None:
        if not self.settings.track_alerted_signals:
            return
        for candidate in response.candidates:
            if candidate.state.value == "none":
                continue
            if not bool(
                candidate.metadata.get(
                    "alert_eligible",
                    (candidate.direction_label or candidate.state.value) != "watchlist",
                )
            ):
                continue
            self.tracked_signals.upsert_open(candidate, origin=origin)

    @staticmethod
    def _close_status(snapshot: Any, price: float) -> str | None:
        role = str(snapshot.signal_role or snapshot.metadata.get("signal_role") or "entry_long")
        is_short = role == "entry_short"
        stop = snapshot.stop_loss
        target = snapshot.take_profit or (snapshot.targets[0] if snapshot.targets else None)
        if stop is not None:
            if is_short and price >= stop:
                return "stop_hit"
            if not is_short and price <= stop:
                return "stop_hit"
        if target is not None:
            if is_short and price <= target:
                return "target_hit"
            if not is_short and price >= target:
                return "target_hit"
        return None

    def _is_due(self, state_key: str, interval_minutes: int) -> bool:
        last = self.runtime_state.get(state_key)
        if not last:
            return True
        try:
            last_dt = datetime.fromisoformat(last)
        except ValueError:
            return True
        return (utc_now() - last_dt).total_seconds() >= max(interval_minutes, 1) * 60

    def _daily_summary_due(self) -> bool:
        last = self.runtime_state.get("workflow:last_daily_summary_at")
        now = utc_now()
        if now.hour < int(self.settings.daily_summary_hour_utc):
            return False
        if not last:
            return True
        try:
            last_dt = datetime.fromisoformat(last)
        except ValueError:
            return True
        return last_dt.astimezone(UTC).date() < now.astimezone(UTC).date()

    def _ledger_cycle_due(self) -> bool:
        if self.ledger_service is None:
            return False
        if not bool(getattr(self.settings, "ledger_enabled", False)):
            return False
        if not bool(getattr(self.settings, "ledger_cycle_enabled", False)):
            return False
        return self._is_due(
            "workflow:last_ledger_cycle_at",
            int(getattr(self.settings, "ledger_cycle_interval_minutes", 15)),
        )

    def _check_open_signals_impl(self, *, notify: bool, force_refresh: bool) -> WorkflowTaskResponse:
        records = self.tracked_signals.list(status="open", limit=500)
        closed_signals = 0
        alerts_sent = 0

        for record in records:
            quote = self.market_data.get_quote(
                record.symbol,
                timeframe=record.timeframe,
                force_refresh=force_refresh,
            )
            price = float(quote.last_execution or quote.ask or quote.bid or record.last_price or 0.0)
            snapshot = record.snapshot.model_copy(
                update={
                    "current_price": price,
                    "current_bid": quote.bid,
                    "current_ask": quote.ask,
                    "rate_timestamp": quote.timestamp,
                    "generated_at": utc_now().isoformat(),
                }
            )
            self.tracked_signals.update_price(record.id, last_price=price, snapshot=snapshot)

            close_status = self._close_status(snapshot, price)
            if close_status is None:
                continue

            closed = self.tracked_signals.close(
                record.id,
                status=close_status,
                last_price=price,
                snapshot=snapshot,
            )
            closed_signals += 1
            message = self.notifier.format_tracked_signal_update(closed, event_type=close_status)
            if notify and self.notifier.send_text(message):
                alerts_sent += 1
            self.alert_history.create(
                category="tracked_signal_update",
                status=close_status,
                message_text=message,
                symbol=closed.symbol,
                strategy_name=closed.strategy_name,
                timeframe=closed.timeframe,
                payload=closed.model_dump(),
            )

        self.runtime_state.set("workflow:last_open_signal_check_at", utc_now().isoformat())
        self.run_logs.log(
            "workflow_open_signal_check_completed",
            {"open_signals": len(records), "closed_signals": closed_signals, "alerts_sent": alerts_sent},
        )
        return WorkflowTaskResponse(
            task="open_signal_check",
            status="ok",
            detail="Open signal check completed.",
            alerts_sent=alerts_sent,
            open_signals=len(records),
            closed_signals=closed_signals,
        )

    def _send_daily_summary_impl(self, *, notify: bool) -> WorkflowTaskResponse:
        open_signals = self.tracked_signals.list(status="open", limit=20)
        recent_alerts = self.alert_history.list(limit=20)
        message = self.notifier.format_daily_summary(
            open_signals=open_signals,
            recent_alerts=recent_alerts,
        )
        alerts_sent = 1 if (notify and self.notifier.send_text(message)) else 0
        self.alert_history.create(
            category="daily_summary",
            status="sent" if alerts_sent else "generated",
            message_text=message,
            payload={
                "open_signals": [item.model_dump() for item in open_signals],
                "recent_alerts": [item.model_dump() if hasattr(item, "model_dump") else item for item in recent_alerts],
            },
        )
        self.runtime_state.set("workflow:last_daily_summary_at", utc_now().isoformat())
        self.run_logs.log(
            "workflow_daily_summary_completed",
            {"open_signals": len(open_signals), "recent_alerts": len(recent_alerts), "alerts_sent": alerts_sent},
        )
        return WorkflowTaskResponse(
            task="daily_summary",
            status="ok",
            detail="Daily summary generated.",
            alerts_sent=alerts_sent,
            open_signals=len(open_signals),
        )

    def _send_scan_alerts(self, *, task: str, response: Any, notify: bool) -> int:
        if not notify:
            return 0
        alert_candidates = [
            item for item in response.candidates
            if bool(
                item.metadata.get(
                    "alert_eligible",
                    (item.direction_label or item.state.value) != "watchlist" and item.state.value != "none",
                )
            )
        ][: max(1, int(self.settings.screener_top_alerts_per_run))]
        if not alert_candidates:
            return 0

        if self.settings.screener_alert_mode == "single":
            sent = 0
            for item in alert_candidates:
                message = self.notifier.format_screener_candidate(item)
                delivered = self.notifier.send_text(message)
                if delivered:
                    sent += 1
                    self._record_ledger_alert(task=task, candidate=item)
                self.alert_history.create(
                    category=task,
                    status="sent" if delivered else "generated",
                    message_text=message,
                    symbol=item.symbol,
                    strategy_name=item.strategy_name,
                    timeframe=item.timeframe,
                    payload=item.model_dump(),
                )
            return sent

        digest = response.model_copy(update={"candidates": alert_candidates})
        try:
            message = self.notifier.format_screener_summary(digest, task_label=task)
        except TypeError:
            message = self.notifier.format_screener_summary(digest)
        alerts_sent = 1 if self.notifier.send_text(message) else 0
        if alerts_sent:
            for item in alert_candidates:
                self._record_ledger_alert(task=task, candidate=item)
        self.alert_history.create(
            category=task,
            status="sent" if alerts_sent else "generated",
            message_text=message,
            payload=digest.model_dump(),
        )
        return alerts_sent

    def _record_ledger_alert(self, *, task: str, candidate: Any) -> None:
        if self.ledger_service is None:
            return
        if not bool(getattr(self.settings, "ledger_enabled", False)):
            return
        if not bool(getattr(self.settings, "ledger_record_alerts_enabled", False)):
            return
        try:
            generated_at = candidate.generated_at or candidate.signal_generated_at or utc_now().isoformat()
            alert_id = (
                f"{task}:{candidate.symbol}:{candidate.strategy_name}:"
                f"{candidate.timeframe}:{generated_at}"
            )
            target = candidate.take_profit
            if target is None and candidate.targets:
                target = candidate.targets[0]
            self.ledger_service.record_alert(
                alert_source=task,
                alert_id=alert_id,
                symbol=candidate.symbol,
                strategy_name=candidate.strategy_name,
                timeframe=candidate.timeframe,
                alert_created_at=generated_at,
                alert_entry_price=candidate.entry_price or candidate.current_price,
                alert_stop=candidate.stop_loss,
                alert_target=target,
                alert_score=candidate.score,
                alert_payload=candidate.model_dump() if hasattr(candidate, "model_dump") else {},
            )
            self.run_logs.log(
                "ledger_alert_recorded",
                {
                    "task": task,
                    "symbol": candidate.symbol,
                    "strategy_name": candidate.strategy_name,
                    "timeframe": candidate.timeframe,
                    "alert_id": alert_id,
                },
            )
        except Exception as exc:  # noqa: BLE001
            self.run_logs.log(
                "ledger_alert_record_error",
                {
                    "task": task,
                    "symbol": getattr(candidate, "symbol", None),
                    "error": str(exc),
                },
            )

    def _run_ledger_cycle_impl(self) -> WorkflowTaskResponse:
        if self.ledger_service is None:
            return WorkflowTaskResponse(
                task="ledger_cycle",
                status="skipped",
                detail="Ledger service is not configured.",
                skipped=True,
            )
        result = self.ledger_service.run_cycle()
        self.runtime_state.set("workflow:last_ledger_cycle_at", utc_now().isoformat())
        self.run_logs.log("workflow_ledger_cycle_completed", result)
        return WorkflowTaskResponse(
            task="ledger_cycle",
            status="ok",
            detail=(
                "Ledger cycle completed. "
                f"Positions seen: {result.get('positions_seen', 0)}, "
                f"matched: {result.get('matched_new', 0)}, "
                f"manual imported: {result.get('manual_imported', 0)}, "
                f"closed: {result.get('closed_new', 0)}, "
                f"expired: {result.get('expired_pending', 0)}."
            ),
            closed_signals=int(result.get("closed_new", 0) or 0),
        )

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

    def _named_scan_due(self, state_key: str, enabled: bool, scheduled_time: str) -> bool:
        if not enabled:
            return False
        now_local = self._local_now()
        due_at = self._combine_local_time(now_local, scheduled_time)
        if now_local < due_at:
            return False
        last = self.runtime_state.get(state_key)
        if not last:
            return True
        try:
            last_dt = datetime.fromisoformat(last)
        except ValueError:
            return True
        return last_dt.astimezone(self._schedule_zone()).date() < now_local.date()

    def _intraday_scan_due(self) -> bool:
        if not self.settings.intraday_repeated_scan_enabled:
            return False
        now_local = self._local_now()
        start = self._combine_local_time(now_local, self.settings.intraday_scan_start_local)
        end = self._combine_local_time(now_local, self.settings.intraday_scan_end_local)
        if now_local < start or now_local > end:
            return False
        return self._is_due("workflow:last_intraday_scan_at", self.settings.intraday_scan_interval_minutes)

    def _intelligent_scan_due(self) -> bool:
        if not self.settings.intelligent_scan_enabled:
            return False
        now_local = self._local_now()
        start = self._combine_local_time(now_local, self.settings.intelligent_scan_start_local)
        end = self._combine_local_time(now_local, self.settings.intelligent_scan_end_local)
        if now_local < start or now_local > end:
            return False
        return self._is_due("workflow:last_intelligent_scan_at", self.settings.intelligent_scan_interval_minutes)

    def _local_now(self) -> datetime:
        return utc_now().astimezone(self._schedule_zone())

    def _schedule_zone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.settings.schedule_timezone)
        except ZoneInfoNotFoundError:
            return ZoneInfo("UTC")

    def _combine_local_time(self, current: datetime, raw_time: str) -> datetime:
        hour, minute = self._parse_time(raw_time)
        return datetime.combine(current.date(), time(hour=hour, minute=minute), tzinfo=current.tzinfo)

    @staticmethod
    def _parse_time(raw_time: str) -> tuple[int, int]:
        try:
            hour_raw, minute_raw = raw_time.strip().split(":", 1)
            return max(0, min(int(hour_raw), 23)), max(0, min(int(minute_raw), 59))
        except Exception:
            return 0, 0
