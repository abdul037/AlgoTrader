"""Scheduled scan workflow, tracked signal monitoring, and summaries."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from app.execution.interfaces import SignalApprovalAdapter
from app.models.approval import ApprovalStatus
from app.models.workflow import WorkflowBucketStatus, WorkflowStatusResponse, WorkflowTaskResponse
from app.universe import resolve_universe
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
    is_market_day,
    last_successful_screener_run_at,
    ledger_cycle_due,
    local_now,
    named_scan_due,
    next_market_day,
    parse_time,
    schedule_zone,
)


class LedgerRecordingError(RuntimeError):
    """Raised when we cannot reliably record an alert in the ledger."""


class SignalWorkflowService:
    """Coordinate scheduled scans, tracked open signals, and daily summaries."""

    LedgerRecordingError = LedgerRecordingError
    SCAN_BUCKETS = ("premarket_scan", "market_open_scan", "intraday_rotation", "swing_hourly", "end_of_day_scan")
    SCHEDULER_BUCKETS = (*SCAN_BUCKETS, "maintenance")

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
        proposal_service: Any | None = None,
        automation_service: Any | None = None,
        reconciliation_service: Any | None = None,
        etoro_reconciliation_service: Any | None = None,
        auto_trading_service: Any | None = None,
        learning_service: Any | None = None,
        rl_policy_service: Any | None = None,
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
        self.proposal_service = proposal_service
        self.automation = automation_service
        self.reconciliation = reconciliation_service
        self.etoro_reconciliation = etoro_reconciliation_service
        self.auto_trading = auto_trading_service
        self.learning = learning_service
        self.rl_policy = rl_policy_service
        self._approval_adapter = SignalApprovalAdapter()

    def run_scheduled_tasks(self) -> dict[str, int]:
        summary = {"alerts_sent": 0, "closed_signals": 0, "ledger_cycles": 0, "buckets_run": 0}
        if self.automation is not None:
            blockers = self.automation.scan_blockers()
            if blockers:
                self.run_logs.log("workflow_scheduler_paused", {"blockers": blockers})
                for bucket_name in self.SCAN_BUCKETS:
                    self._record_bucket_state(bucket_name, status="paused", error=",".join(blockers))
                return summary

        if self._bucket_due("maintenance"):
            result = self.run_maintenance(notify=True)
            if result.status == "ok":
                summary["alerts_sent"] += result.alerts_sent
                summary["closed_signals"] += result.closed_signals
                summary["ledger_cycles"] += int((result.detail or "").count("ledger_cycle"))
                summary["buckets_run"] += 1

        if not self.settings.screener_scheduler_enabled:
            return summary

        for bucket_name in self.SCAN_BUCKETS:
            if not self._bucket_due(bucket_name):
                continue
            result = self.run_bucket(bucket_name, notify=True, force_refresh=bucket_name in {"premarket_scan", "market_open_scan", "end_of_day_scan"})
            summary["alerts_sent"] += result.alerts_sent
            if result.status == "ok":
                summary["buckets_run"] += 1
        return summary

    def run_premarket_scan(self, *, notify: bool = True, force_refresh: bool = False) -> WorkflowTaskResponse:
        return self._execute_guarded(
            "premarket_scan",
            lambda: self._run_scan_task(
                task="premarket_scan",
                state_key="workflow:last_premarket_scan_at",
                origin="premarket_scan",
                timeframes=["15m", "1h", "1d"],
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
                timeframes=["1m", "5m", "10m", "15m"],
                notify=notify,
                force_refresh=force_refresh,
            ),
        )

    def run_swing_scan(self, *, notify: bool = True, force_refresh: bool = False) -> WorkflowTaskResponse:
        symbols = resolve_universe(
            self.settings,
            limit=int(getattr(self.settings, "market_universe_limit", 100) or 100),
        )
        timeframes = self._normalized_timeframes(getattr(self.settings, "swing_scan_timeframes", ["1d", "1w"]))
        if str(getattr(self.settings, "screener_spec_coverage_mode", "default")) == "scheduled_all":
            timeframes = ["1h", "1d"]
        return self._execute_guarded(
            "swing_scan",
            lambda: self._run_scan_task(
                task="swing_scan",
                state_key="workflow:last_swing_scan_at",
                origin="swing_scan",
                timeframes=timeframes,
                notify=notify,
                force_refresh=force_refresh,
                symbols=symbols,
            ),
            bucket_name="swing_hourly",
        )

    def run_intelligent_scan(self, *, notify: bool = True, force_refresh: bool = False) -> WorkflowTaskResponse:
        timeframes = self._normalized_timeframes(self.settings.intelligent_scan_timeframes)
        if str(getattr(self.settings, "screener_spec_coverage_mode", "default")) == "scheduled_all":
            timeframes = self._rotating_intelligent_timeframes()
        return self._execute_guarded(
            "intelligent_scan",
            lambda: self._run_scan_task(
                task="intelligent_scan",
                state_key="workflow:last_intelligent_scan_at",
                origin="intelligent_scan",
                timeframes=timeframes,
                notify=notify,
                force_refresh=force_refresh,
            ),
        )

    def run_intraday_scan(self, *, notify: bool = True, force_refresh: bool = False) -> WorkflowTaskResponse:
        symbols = self._intraday_scan_symbols()
        return self._execute_guarded(
            "intraday_scan",
            lambda: self._run_scan_task(
                task="intraday_scan",
                state_key="workflow:last_intraday_scan_at",
                origin="intraday_scan",
                timeframes=self._normalized_timeframes(self.settings.screener_intraday_timeframes),
                notify=notify,
                force_refresh=force_refresh,
                symbols=symbols,
            ),
            bucket_name="intraday_rotation",
        )

    def run_end_of_day_scan(self, *, notify: bool = True, force_refresh: bool = False) -> WorkflowTaskResponse:
        timeframes = ["15m", "1h", "1d", "1w"]
        if str(getattr(self.settings, "screener_spec_coverage_mode", "default")) == "scheduled_all":
            timeframes = ["1w"]
        return self._execute_guarded(
            "end_of_day_scan",
            lambda: self._run_scan_task(
                task="end_of_day_scan",
                state_key="workflow:last_end_of_day_scan_at",
                origin="end_of_day_scan",
                timeframes=timeframes,
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
            bucket_name="maintenance",
        )

    def run_maintenance(self, *, notify: bool = True) -> WorkflowTaskResponse:
        def runner() -> WorkflowTaskResponse:
            alerts_sent = 0
            closed_signals = 0
            completed: list[str] = []
            errors: list[str] = []

            if self._ledger_cycle_due():
                result = self.run_ledger_cycle()
                completed.append("ledger_cycle")
                errors.extend(result.errors)
            if self.reconciliation is not None and self._is_due(
                "reconciliation:last_run_at",
                max(int(getattr(self.settings, "alpaca_reconciliation_interval_seconds", 60)) // 60, 1),
            ):
                reconciliation = self.reconciliation.reconcile()
                completed.append("alpaca_reconciliation")
                if reconciliation.get("status") == "error":
                    errors.extend(list(reconciliation.get("issues") or []))
            if self.etoro_reconciliation is not None and self._is_due(
                "etoro_demo_reconciliation:last_run_at",
                max(int(getattr(self.settings, "alpaca_reconciliation_interval_seconds", 60)) // 60, 1),
            ):
                reconciliation = self.etoro_reconciliation.reconcile()
                completed.append("etoro_demo_reconciliation")
                if reconciliation.get("status") == "error":
                    errors.extend(list(reconciliation.get("issues") or []))
            if self.auto_trading is not None:
                health = self.auto_trading.refresh_strategy_health()
                if health:
                    completed.append("strategy_health")
                processed = self.auto_trading.process_ready_queue()
                if processed:
                    completed.append("auto_execution_queue")
            if self.learning is not None:
                scheduled = self.learning.schedule_due_jobs()
                if scheduled:
                    completed.append("learning_jobs_scheduled")
                processed_learning = self.learning.process_jobs(limit=10)
                if processed_learning:
                    completed.append("learning_jobs_processed")
            if self.rl_policy is not None:
                if bool(getattr(self.settings, "rl_policy_training_enabled", False)) and self._rl_policy_training_due():
                    try:
                        policy = self.rl_policy.train()
                        completed.append("rl_policy_training")
                        self.runtime_state.set("rl_policy:last_train_at", utc_now().isoformat())
                        if getattr(policy, "blockers", None):
                            self.run_logs.log("rl_policy_training_blocked", {"blockers": policy.blockers})
                    except Exception as exc:  # noqa: BLE001 - maintenance should continue after RL failures
                        errors.append(f"rl_policy_training:{exc}")
                        self.run_logs.log("rl_policy_training_error", {"error": str(exc)})
                if bool(getattr(self.settings, "rl_policy_paper_proposals_enabled", False)):
                    try:
                        proposal = self.rl_policy.propose()
                        completed.append("rl_policy_proposal_check")
                        if getattr(proposal, "status", "") == "queued":
                            completed.append("rl_policy_proposal_queued")
                    except Exception as exc:  # noqa: BLE001 - RL can never block reconciliation or safety work
                        errors.append(f"rl_policy_proposal:{exc}")
                        self.run_logs.log("rl_policy_proposal_error", {"error": str(exc)})
            if self._is_due("workflow:last_open_signal_check_at", self.settings.open_signal_check_interval_minutes):
                result = self.check_open_signals(notify=notify)
                completed.append("open_signal_check")
                alerts_sent += result.alerts_sent
                closed_signals += result.closed_signals
                errors.extend(result.errors)
            if self._daily_summary_due():
                result = self.send_daily_summary(notify=notify)
                completed.append("daily_summary")
                alerts_sent += result.alerts_sent
                errors.extend(result.errors)

            detail = "maintenance completed: " + ", ".join(completed) if completed else "maintenance skipped: nothing due"
            return WorkflowTaskResponse(
                task="maintenance",
                status="ok",
                detail=detail,
                skipped=not completed,
                alerts_sent=alerts_sent,
                closed_signals=closed_signals,
                open_signals=len(self.tracked_signals.list(status="open", limit=500)),
                errors=errors,
            )

        return self._execute_guarded("maintenance", runner, bucket_name="maintenance")

    def run_bucket(self, bucket_name: str, *, notify: bool = True, force_refresh: bool = True) -> WorkflowTaskResponse:
        normalized = bucket_name.strip().lower().replace("-", "_")
        runners = {
            "premarket_scan": lambda: self.run_premarket_scan(notify=notify, force_refresh=force_refresh),
            "market_open_scan": lambda: self.run_market_open_scan(notify=notify, force_refresh=force_refresh),
            "intraday_rotation": lambda: self.run_intraday_scan(notify=notify, force_refresh=force_refresh),
            "swing_hourly": lambda: self.run_swing_scan(notify=notify, force_refresh=force_refresh),
            "end_of_day_scan": lambda: self.run_end_of_day_scan(notify=notify, force_refresh=force_refresh),
            "maintenance": lambda: self.run_maintenance(notify=notify),
        }
        runner = runners.get(normalized)
        if runner is None:
            raise KeyError(f"Unknown workflow bucket: {bucket_name}")
        return runner()

    def schedule_statuses(self) -> list[WorkflowBucketStatus]:
        return [self._bucket_status(name) for name in self.SCHEDULER_BUCKETS]

    def status(self) -> WorkflowStatusResponse:
        state_keys = [
            "workflow:last_premarket_scan_at",
            "workflow:last_market_open_scan_at",
            "workflow:last_intelligent_scan_at",
            "workflow:last_swing_scan_at",
            "workflow:last_intraday_scan_at",
            "workflow:last_end_of_day_scan_at",
            "workflow:last_open_signal_check_at",
            "workflow:last_ledger_cycle_at",
            "workflow:last_daily_summary_at",
        ]
        state = self._runtime_state_get_many(state_keys)
        return WorkflowStatusResponse(
            scheduler_enabled=bool(self.settings.screener_scheduler_enabled),
            schedule_timezone=self.settings.schedule_timezone,
            last_premarket_scan_at=state.get("workflow:last_premarket_scan_at"),
            last_market_open_scan_at=state.get("workflow:last_market_open_scan_at"),
            last_intelligent_scan_at=state.get("workflow:last_intelligent_scan_at"),
            last_swing_scan_at=state.get("workflow:last_swing_scan_at"),
            last_intraday_scan_at=state.get("workflow:last_intraday_scan_at"),
            last_end_of_day_scan_at=state.get("workflow:last_end_of_day_scan_at"),
            last_open_signal_check_at=state.get("workflow:last_open_signal_check_at"),
            last_ledger_cycle_at=state.get("workflow:last_ledger_cycle_at"),
            last_daily_summary_at=state.get("workflow:last_daily_summary_at"),
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
        learning = self.learning.status() if self.learning is not None else {}
        if int(learning.get("failed_jobs") or 0) > 0:
            status = "warning"
            reasons.append("learning_jobs_failed")
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
            "active_meta_model_version": learning.get("active_model_version"),
            "model_deployment_mode": learning.get("model_deployment_mode", "shadow"),
            "learning": learning,
        }

    def lightweight_health(self) -> dict[str, Any]:
        """Return scheduler freshness without triggering scan due calculations."""

        blockers: list[str] = []
        buckets: list[dict[str, Any]] = []
        now = utc_now()
        now_local = self._local_now()
        state_keys = []
        for bucket_name in self.SCHEDULER_BUCKETS:
            prefix = self._bucket_state_prefix(bucket_name)
            state_keys.extend(
                [
                    f"{prefix}:last_success_at",
                    f"{prefix}:last_run_at",
                    f"{prefix}:last_status",
                    f"{prefix}:last_error",
                ]
            )
        state = self._runtime_state_get_many(state_keys)
        for bucket_name in self.SCHEDULER_BUCKETS:
            prefix = self._bucket_state_prefix(bucket_name)
            enabled = self._bucket_enabled(bucket_name)
            paused = self._bucket_paused(bucket_name)
            last_success_at = state.get(f"{prefix}:last_success_at")
            last_run_at = state.get(f"{prefix}:last_run_at")
            last_status = state.get(f"{prefix}:last_status")
            last_error = state.get(f"{prefix}:last_error") or None
            threshold_seconds = self._bucket_freshness_threshold_seconds(bucket_name)
            expected = self._bucket_expected_for_freshness(bucket_name, now_local=now_local)
            age_seconds = self._age_seconds(last_success_at, now=now)
            stale = bool(
                enabled
                and not paused
                and expected
                and (age_seconds is None or age_seconds > threshold_seconds)
            )
            if stale:
                blockers.append(f"scheduler_bucket_stale:{bucket_name}")
            if enabled and last_status == "error":
                blockers.append(f"scheduler_bucket_error:{bucket_name}")
            buckets.append(
                {
                    "name": bucket_name,
                    "enabled": enabled,
                    "paused": paused,
                    "expected": expected,
                    "last_run_at": last_run_at,
                    "last_success_at": last_success_at,
                    "age_seconds": age_seconds,
                    "freshness_threshold_seconds": threshold_seconds,
                    "stale": stale,
                    "last_status": last_status,
                    "last_error": last_error,
                }
            )
        return {
            "status": "ok" if not blockers else "warning",
            "scheduler_enabled": bool(self.settings.screener_scheduler_enabled),
            "generated_at": now.isoformat(),
            "blockers": sorted(set(blockers)),
            "buckets": buckets,
            "scan_coverage": self._scan_coverage_summary(),
        }

    def _run_scan_task(self, **kwargs: Any) -> WorkflowTaskResponse:
        return run_scan_task(self, **kwargs)

    def _auto_propose_candidates(self, response: Any, *, origin: str, notify: bool) -> int:
        if not (
            bool(getattr(self.settings, "auto_propose_enabled", False))
            or bool(getattr(self.settings, "paper_auto_approve_proposals", False))
        ):
            return 0
        if self.proposal_service is None:
            return 0
        if self.automation is not None and self.automation.scan_blockers():
            return 0
        existing_symbols = {
            proposal.order.symbol.upper()
            for status in (ApprovalStatus.PENDING, ApprovalStatus.APPROVED)
            for proposal in self.proposal_service.list_proposals(status=status)
        }
        created = 0
        for candidate in list(getattr(response, "candidates", []) or []):
            symbol = str(getattr(candidate, "symbol", "") or "").upper()
            if not symbol or symbol in existing_symbols:
                continue
            if self.auto_trading is not None:
                proposal_blockers = self.auto_trading.candidate_proposal_blockers(candidate)
                if proposal_blockers:
                    self.run_logs.log(
                        "auto_proposal_safety_blocked",
                        {"origin": origin, "symbol": symbol, "blockers": proposal_blockers},
                    )
                    continue
            if not bool(getattr(candidate, "execution_ready", False)):
                continue
            if not bool((getattr(candidate, "metadata", {}) or {}).get("alert_eligible", False)):
                continue
            if str(getattr(candidate, "signal_role", "") or "").lower() == "entry_short":
                continue
            if getattr(candidate, "stop_loss", None) is None:
                continue
            try:
                request = self._approval_adapter.build_proposal_request(
                    candidate,
                    amount_usd=float(getattr(self.settings, "default_trade_amount_usd", 1000.0)),
                    notes=f"Auto-created from {origin}; Telegram approval is required before execution.",
                )
                proposal = self.proposal_service.create_proposal(request)
            except Exception as exc:  # noqa: BLE001
                self.run_logs.log(
                    "auto_proposal_failed",
                    {"origin": origin, "symbol": symbol, "error": str(exc)},
                )
                continue
            existing_symbols.add(symbol)
            created += 1
            self.run_logs.log(
                "auto_proposal_created",
                {"origin": origin, "proposal_id": proposal.id, "symbol": symbol},
            )
            if self.auto_trading is not None:
                self.auto_trading.approve_enqueue_execute(proposal, candidate)
            if notify:
                self.notifier.send_text(
                    "\n".join(
                        [
                            "Auto proposal created",
                            f"ID: {proposal.id}",
                            f"Symbol: {proposal.order.symbol}",
                            f"Entry: {proposal.order.proposed_price:.2f}",
                            f"Stop: {proposal.order.stop_loss or 'n/a'}",
                            f"Target: {proposal.order.take_profit or 'n/a'}",
                            f"Approve: /approve {proposal.id}",
                            f"Reject: /reject {proposal.id}",
                        ]
                    )
                )
        return created

    def _intraday_scan_symbols(self) -> list[str]:
        universe = resolve_universe(
            self.settings,
            limit=int(
                max(
                    int(getattr(self.settings, "market_universe_limit", 100) or 100),
                    int(getattr(self.settings, "intraday_active_mover_scan_limit", 80) or 80),
                )
            ),
        )
        if not universe:
            return []
        batch_size = max(1, int(getattr(self.settings, "scalp_scan_batch_size", 20) or 20))
        shortlist_limit = max(0, int(getattr(self.settings, "intraday_active_shortlist_size", 20) or 20))
        offset_key = "workflow:intraday_scan_offset"
        try:
            offset = int(self.runtime_state.get(offset_key) or "0")
        except ValueError:
            offset = 0
        offset = offset % len(universe)
        rotated = (universe + universe)[offset : offset + min(batch_size, len(universe))]
        next_offset = (offset + min(batch_size, len(universe))) % len(universe)
        self.runtime_state.set(offset_key, str(next_offset))
        active: list[str] = []
        if shortlist_limit:
            for record in self.tracked_signals.list(status="open", limit=shortlist_limit):
                symbol = str(getattr(record, "symbol", "") or "").upper()
                if symbol:
                    active.append(symbol)
        movers = self._active_mover_shortlist(universe)
        combined: list[str] = []
        for symbol in [*active, *movers, *rotated]:
            if symbol not in combined:
                combined.append(symbol)
        max_symbols = max(0, int(getattr(self.settings, "intraday_scan_max_symbols", 0) or 0))
        if max_symbols:
            return combined[:max_symbols]
        return combined

    def _active_mover_shortlist(self, universe: list[str]) -> list[str]:
        if not bool(getattr(self.settings, "intraday_active_mover_shortlist_enabled", False)):
            return []
        refresh_minutes = max(int(getattr(self.settings, "intraday_active_mover_refresh_minutes", 15) or 15), 1)
        cached = self._load_active_mover_state()
        cached_at = cached.get("generated_at")
        cached_age_seconds = self._age_seconds(cached_at, now=utc_now()) if cached_at else None
        if cached_age_seconds is not None and float(cached_age_seconds) <= refresh_minutes * 60:
            return list(cached.get("symbols") or [])

        scan_limit = max(1, int(getattr(self.settings, "intraday_active_mover_scan_limit", 80) or 80))
        shortlist_size = max(0, int(getattr(self.settings, "intraday_active_mover_shortlist_size", 30) or 30))
        if shortlist_size <= 0:
            return []
        candidates: list[dict[str, Any]] = []
        errors: list[str] = []
        for symbol in universe[:scan_limit]:
            if self._symbol_blocked_for_shortlist(symbol):
                continue
            try:
                history = self.market_data.get_history(symbol, timeframe="5m", bars=40, force_refresh=True)
                quote = self.market_data.get_quote(symbol, timeframe="5m", force_refresh=True)
                ranked = self._rank_active_mover(symbol, history=history, quote=quote)
            except Exception as exc:  # noqa: BLE001 - a bad symbol must not block the shortlist
                errors.append(f"{symbol}:{exc}")
                continue
            if ranked is not None:
                candidates.append(ranked)
        ranked_symbols = [
            item["symbol"]
            for item in sorted(candidates, key=lambda item: float(item["score"]), reverse=True)[:shortlist_size]
        ]
        self._store_active_mover_state(
            {
                "generated_at": utc_now().isoformat(),
                "symbols": ranked_symbols,
                "evaluated": len(candidates),
                "scan_limit": scan_limit,
                "scores": sorted(candidates, key=lambda item: float(item["score"]), reverse=True)[:shortlist_size],
                "errors": errors[:10],
            }
        )
        return ranked_symbols

    def _rank_active_mover(self, symbol: str, *, history: Any, quote: Any) -> dict[str, Any] | None:
        if history is None or len(history) < 20:
            return None
        quote_age = getattr(quote, "data_age_seconds", None)
        max_age = float(getattr(self.settings, "max_market_data_age_seconds", 120) or 120)
        if quote_age is not None and float(quote_age) > max_age:
            return None
        bid = getattr(quote, "bid", None)
        ask = getattr(quote, "ask", None)
        if bid is None or ask is None or float(bid) <= 0 or float(ask) <= 0 or float(ask) < float(bid):
            return None
        mid = (float(bid) + float(ask)) / 2.0
        spread_bps = ((float(ask) - float(bid)) / max(mid, 0.01)) * 10_000.0
        if spread_bps > float(getattr(self.settings, "screener_max_spread_bps", 50.0)):
            return None
        frame = history.tail(40).copy()
        last = frame.iloc[-1]
        close = float(last["close"])
        volume = float(last["volume"])
        avg_volume = float(frame["volume"].tail(20).mean() or 0.0)
        relative_volume = volume / avg_volume if avg_volume > 0 else 0.0
        short_window_close = float(frame["close"].iloc[-6]) if len(frame) >= 6 else float(frame["close"].iloc[0])
        short_move_pct = abs((close - short_window_close) / max(short_window_close, 0.01)) * 100.0
        session_open = float(frame["open"].iloc[0])
        intraday_move_pct = abs((close - session_open) / max(session_open, 0.01)) * 100.0
        dollar_volume = close * volume
        score = (
            min(relative_volume / 2.0, 2.0) * 35.0
            + min(short_move_pct / 2.5, 2.0) * 25.0
            + min(intraday_move_pct / 4.0, 2.0) * 20.0
            + min(dollar_volume / 50_000_000.0, 2.0) * 10.0
            + max(0.0, 1.0 - min(spread_bps / max(float(self.settings.screener_max_spread_bps), 1.0), 1.0)) * 10.0
        )
        return {
            "symbol": symbol.upper(),
            "score": round(score, 4),
            "relative_volume": round(relative_volume, 4),
            "short_move_pct": round(short_move_pct, 4),
            "intraday_move_pct": round(intraday_move_pct, 4),
            "dollar_volume": round(dollar_volume, 2),
            "spread_bps": round(spread_bps, 4),
        }

    def _symbol_blocked_for_shortlist(self, symbol: str) -> bool:
        normalized = symbol.upper().strip()
        if normalized in {item.upper() for item in getattr(self.settings, "blocked_instruments", []) or []}:
            return True
        safety = getattr(getattr(self, "auto_trading", None), "safety", None)
        if safety is not None and hasattr(safety, "is_blacklisted"):
            try:
                if safety.is_blacklisted(normalized):
                    return True
            except Exception:  # noqa: BLE001
                return True
        alpaca = getattr(getattr(self, "auto_trading", None), "alpaca", None)
        if alpaca is not None and hasattr(alpaca, "is_supported_equity"):
            try:
                return not bool(alpaca.is_supported_equity(normalized))
            except Exception:  # noqa: BLE001
                return True
        return False

    def _load_active_mover_state(self) -> dict[str, Any]:
        raw = self.runtime_state.get("workflow:intraday_active_movers")
        if not raw:
            return {}
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def _store_active_mover_state(self, payload: dict[str, Any]) -> None:
        try:
            self.runtime_state.set("workflow:intraday_active_movers", json.dumps(payload, default=str))
        except Exception:  # noqa: BLE001 - shortlist telemetry must not block scans
            return

    def _scan_coverage_summary(self) -> dict[str, Any]:
        tasks = ("premarket_scan", "market_open_scan", "intraday_scan", "swing_scan", "end_of_day_scan")
        state_keys = ["workflow:intraday_active_movers"]
        state_keys.extend(f"workflow:{task}:last_scan_coverage" for task in tasks)
        state = self._runtime_state_get_many(state_keys)
        coverage: dict[str, Any] = {
            "active_mover_shortlist": self._decode_json_object(
                state.get("workflow:intraday_active_movers"),
                error_key="invalid_active_mover_state",
            ),
            "latest_by_task": {},
        }
        for task in tasks:
            raw = state.get(f"workflow:{task}:last_scan_coverage")
            if not raw:
                continue
            coverage["latest_by_task"][task] = self._decode_json_object(
                raw,
                error_key="invalid_scan_coverage_state",
            )
        return coverage

    def _runtime_state_get_many(self, keys: list[str] | tuple[str, ...]) -> dict[str, str | None]:
        getter = getattr(self.runtime_state, "get_many", None)
        if callable(getter):
            return getter(keys)
        return {key: self.runtime_state.get(key) for key in keys}

    @staticmethod
    def _decode_json_object(raw: str | None, *, error_key: str) -> dict[str, Any]:
        if not raw:
            return {}
        try:
            loaded = json.loads(raw)
        except json.JSONDecodeError:
            return {"error": error_key}
        return loaded if isinstance(loaded, dict) else {"error": error_key}

    @staticmethod
    def _normalized_timeframes(timeframes: Any) -> list[str]:
        return [str(item).strip().lower() for item in list(timeframes or []) if str(item).strip()]

    def _rotating_intelligent_timeframes(self) -> list[str]:
        groups = [["1m", "5m"], ["10m", "15m"], ["1h"], ["1d"], ["1w"]]
        key = "workflow:intelligent_scan_timeframe_group"
        try:
            index = int(self.runtime_state.get(key) or "0")
        except ValueError:
            index = 0
        selected = groups[index % len(groups)]
        self.runtime_state.set(key, str((index + 1) % len(groups)))
        return selected

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

    def _rl_policy_training_due(self) -> bool:
        return self._is_due("rl_policy:last_train_at", 24 * 60)

    def _bucket_due(self, bucket_name: str) -> bool:
        if bucket_name == "maintenance":
            return self._maintenance_due()
        if not self._bucket_enabled(bucket_name):
            return False
        if bucket_name == "premarket_scan":
            return self._named_scan_due("workflow:last_premarket_scan_at", True, self.settings.premarket_scan_time_local)
        if bucket_name == "market_open_scan":
            return self._named_scan_due("workflow:last_market_open_scan_at", True, self.settings.market_open_scan_time_local)
        if bucket_name == "intraday_rotation":
            return self._intraday_scan_due()
        if bucket_name == "swing_hourly":
            return self._swing_scan_due()
        if bucket_name == "end_of_day_scan":
            return self._named_scan_due("workflow:last_end_of_day_scan_at", True, self.settings.end_of_day_scan_time_local)
        return False

    def _maintenance_due(self) -> bool:
        return (
            self._ledger_cycle_due()
            or (
                self.reconciliation is not None
                and self._is_due(
                    "reconciliation:last_run_at",
                    max(int(getattr(self.settings, "alpaca_reconciliation_interval_seconds", 60)) // 60, 1),
                )
            )
            or self._is_due("workflow:last_open_signal_check_at", self.settings.open_signal_check_interval_minutes)
            or self._daily_summary_due()
        )

    def _swing_scan_due(self) -> bool:
        now_local = self._local_now()
        if not is_market_day(now_local):
            return False
        start = self._combine_local_time(now_local, self.settings.market_open_scan_time_local)
        end = self._combine_local_time(now_local, self.settings.end_of_day_scan_time_local)
        if now_local < start or now_local > end:
            return False
        return self._is_due("workflow:last_swing_scan_at", int(getattr(self.settings, "swing_scan_interval_minutes", 60)))

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

    def _execute_guarded(self, task: str, runner, *, bucket_name: str | None = None) -> WorkflowTaskResponse:
        bucket = bucket_name or task
        if not self._acquire_lock(task):
            result = WorkflowTaskResponse(
                task=task,
                status="skipped",
                detail=f"{task.replace('_', ' ').title()} skipped because a prior run is still active.",
                skipped=True,
            )
            self._record_bucket_state(bucket, status=result.status, error=result.detail)
            return result
        started_at = utc_now().isoformat()
        self._record_bucket_run(bucket, started_at=started_at)
        self.run_logs.log(f"workflow_{task}_started", {"started_at": started_at})
        try:
            result = runner()
            self._record_bucket_state(
                bucket,
                status=result.status,
                success_at=utc_now().isoformat() if result.status == "ok" else None,
                error=",".join(result.errors) if result.errors else "",
            )
            return result
        except Exception as exc:
            self.run_logs.log(f"workflow_{task}_error", {"error": str(exc)})
            result = WorkflowTaskResponse(
                task=task,
                status="error",
                detail=f"{task.replace('_', ' ').title()} failed: {exc}",
                errors=[str(exc)],
            )
            self._record_bucket_state(bucket, status=result.status, error=str(exc))
            return result
        finally:
            self.runtime_state.set(self._lock_key(task), "")

    def _bucket_status(self, bucket_name: str) -> WorkflowBucketStatus:
        prefix = self._bucket_state_prefix(bucket_name)
        return WorkflowBucketStatus(
            name=bucket_name,
            enabled=self._bucket_enabled(bucket_name),
            paused=self._bucket_paused(bucket_name),
            last_run_at=self.runtime_state.get(f"{prefix}:last_run_at"),
            last_success_at=self.runtime_state.get(f"{prefix}:last_success_at"),
            next_due_at=self._bucket_next_due_at(bucket_name),
            last_status=self.runtime_state.get(f"{prefix}:last_status"),
            last_error=self.runtime_state.get(f"{prefix}:last_error") or None,
        )

    def _bucket_enabled(self, bucket_name: str) -> bool:
        if bucket_name == "maintenance":
            return True
        if not bool(getattr(self.settings, "screener_scheduler_enabled", False)):
            return False
        if bucket_name == "premarket_scan":
            return bool(getattr(self.settings, "premarket_scan_enabled", False))
        if bucket_name == "market_open_scan":
            return bool(getattr(self.settings, "market_open_scan_enabled", False))
        if bucket_name == "intraday_rotation":
            return bool(getattr(self.settings, "intraday_repeated_scan_enabled", False))
        if bucket_name == "swing_hourly":
            return int(getattr(self.settings, "swing_scan_interval_minutes", 0) or 0) > 0
        if bucket_name == "end_of_day_scan":
            return bool(getattr(self.settings, "end_of_day_scan_enabled", False))
        return False

    def _bucket_paused(self, bucket_name: str) -> bool:
        if bucket_name == "maintenance" or self.automation is None:
            return False
        return bool(self.automation.scan_blockers())

    def _record_bucket_run(self, bucket_name: str, *, started_at: str) -> None:
        prefix = self._bucket_state_prefix(bucket_name)
        self.runtime_state.set(f"{prefix}:last_run_at", started_at)
        self.runtime_state.set(f"{prefix}:last_status", "running")
        self.runtime_state.set(f"{prefix}:last_error", "")

    def _record_bucket_state(self, bucket_name: str, *, status: str, error: str = "", success_at: str | None = None) -> None:
        prefix = self._bucket_state_prefix(bucket_name)
        self.runtime_state.set(f"{prefix}:last_status", status)
        self.runtime_state.set(f"{prefix}:last_error", error)
        if success_at:
            self.runtime_state.set(f"{prefix}:last_success_at", success_at)

    @staticmethod
    def _bucket_state_prefix(bucket_name: str) -> str:
        return f"workflow:bucket:{bucket_name}"

    def _bucket_freshness_threshold_seconds(self, bucket_name: str) -> int:
        if bucket_name == "maintenance":
            return max(int(getattr(self.settings, "alpaca_reconciliation_interval_seconds", 60) or 60) * 5, 900)
        if bucket_name == "intraday_rotation":
            return max(int(getattr(self.settings, "intraday_scan_interval_minutes", 15) or 15) * 60 * 3, 3600)
        if bucket_name == "swing_hourly":
            return max(int(getattr(self.settings, "swing_scan_interval_minutes", 60) or 60) * 60 * 2, 7200)
        return 36 * 60 * 60

    def _bucket_expected_for_freshness(self, bucket_name: str, *, now_local: datetime) -> bool:
        if bucket_name == "maintenance":
            return True
        if not is_market_day(now_local):
            return False
        if bucket_name == "premarket_scan":
            return now_local >= self._combine_local_time(now_local, self.settings.premarket_scan_time_local)
        if bucket_name == "market_open_scan":
            return now_local >= self._combine_local_time(now_local, self.settings.market_open_scan_time_local)
        if bucket_name == "end_of_day_scan":
            return now_local >= self._combine_local_time(now_local, self.settings.end_of_day_scan_time_local)
        if bucket_name == "intraday_rotation":
            start = self._combine_local_time(now_local, self.settings.intraday_scan_start_local)
            end = self._combine_local_time(now_local, self.settings.intraday_scan_end_local)
            return start <= now_local <= end + timedelta(minutes=max(int(getattr(self.settings, "intraday_scan_interval_minutes", 15)), 1))
        if bucket_name == "swing_hourly":
            start = self._combine_local_time(now_local, self.settings.market_open_scan_time_local)
            end = self._combine_local_time(now_local, self.settings.end_of_day_scan_time_local)
            return start <= now_local <= end + timedelta(minutes=max(int(getattr(self.settings, "swing_scan_interval_minutes", 60)), 1))
        return False

    @staticmethod
    def _age_seconds(timestamp: str | None, *, now: datetime) -> int | None:
        if not timestamp:
            return None
        try:
            parsed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return max(0, int((now - parsed.astimezone(UTC)).total_seconds()))

    def _bucket_next_due_at(self, bucket_name: str) -> str | None:
        if not self._bucket_enabled(bucket_name):
            return None
        if self._bucket_due(bucket_name):
            return utc_now().isoformat()
        if bucket_name == "maintenance":
            return None

        now_local = self._local_now()
        if bucket_name == "premarket_scan":
            return self._next_named_due_at(self.settings.premarket_scan_time_local, now_local)
        if bucket_name == "market_open_scan":
            return self._next_named_due_at(self.settings.market_open_scan_time_local, now_local)
        if bucket_name == "end_of_day_scan":
            return self._next_named_due_at(self.settings.end_of_day_scan_time_local, now_local)
        if bucket_name == "intraday_rotation":
            return self._next_interval_due_at(
                "workflow:last_intraday_scan_at",
                int(self.settings.intraday_scan_interval_minutes),
                self.settings.intraday_scan_start_local,
                self.settings.intraday_scan_end_local,
                now_local,
            )
        if bucket_name == "swing_hourly":
            return self._next_interval_due_at(
                "workflow:last_swing_scan_at",
                int(getattr(self.settings, "swing_scan_interval_minutes", 60)),
                self.settings.market_open_scan_time_local,
                self.settings.end_of_day_scan_time_local,
                now_local,
            )
        return None

    def _next_named_due_at(self, raw_time: str, now_local: datetime) -> str:
        candidate_day = now_local
        candidate = self._combine_local_time(candidate_day, raw_time)
        if now_local >= candidate or not is_market_day(candidate_day):
            candidate_day = next_market_day(now_local + timedelta(days=1))
            candidate = self._combine_local_time(candidate_day, raw_time)
        return candidate.isoformat()

    def _next_interval_due_at(
        self,
        state_key: str,
        interval_minutes: int,
        start_time: str,
        end_time: str,
        now_local: datetime,
    ) -> str:
        start = self._combine_local_time(now_local, start_time)
        end = self._combine_local_time(now_local, end_time)
        if not is_market_day(now_local) or now_local > end:
            next_day = next_market_day(now_local + timedelta(days=1))
            return self._combine_local_time(next_day, start_time).isoformat()
        if now_local < start:
            return start.isoformat()
        last = self.runtime_state.get(state_key)
        if not last:
            return now_local.isoformat()
        try:
            next_due = datetime.fromisoformat(last).astimezone(self._schedule_zone()) + timedelta(minutes=max(interval_minutes, 1))
        except ValueError:
            return now_local.isoformat()
        if next_due > end:
            next_day = next_market_day(now_local + timedelta(days=1))
            return self._combine_local_time(next_day, start_time).isoformat()
        return max(next_due, now_local).isoformat()

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
