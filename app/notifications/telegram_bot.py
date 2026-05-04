"""Telegram polling bot for commands and scheduled alerts."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import inspect
import logging
import re
import threading
import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from app.execution.interfaces import SignalApprovalAdapter
from app.models.approval import ApprovalDecisionRequest, ApprovalStatus
from app.runtime_settings import AppSettings
from app.telegram_notify import TelegramNotifier
from app.signals.service import LiveSignalService
from app.universe import DEFAULT_TOP_100_US, resolve_universe
from app.utils.time import utc_now

if TYPE_CHECKING:
    from app.storage.repositories import RunLogRepository, RuntimeStateRepository

logger = logging.getLogger(__name__)


class TelegramBotService:
    """Poll Telegram commands and send scheduled alerts."""

    HELP_TEXT = (
        "Commands:\n"
        "/start or /help - show help\n"
        "/signal SYMBOL - full signal snapshot\n"
        "/price SYMBOL - quick price and watch levels\n"
        "/scan [limit] - ranked screener over a safe live batch\n"
        "/scan top100 tf=1m,5m,10m,15m,1h,1d,1w - deep top-100 scan\n"
        "/scan details [limit] - include lower-priority watch setups\n"
        "/intraday_scan [limit] - ranked intraday screener over a safe live batch\n"
        "/scan [limit] SYMBOL... - ranked screener over specific symbols\n"
        "/scan_status - show whether a screener scan is still running\n"
        "/cancel_scan - request cancellation for the active screener scan\n"
        "/supported_scan [limit] - ranked screener for supported symbols\n"
        "/validated_scan [limit] - ranked screener filtered by validated backtests\n"
        "/propose SYMBOL [amount] - create a pending trade proposal from an actionable signal\n"
        "/propose_top [amount] [universe_limit] - scan the universe and propose the top actionable setup\n"
        "/proposals [status] - list proposals, default pending\n"
        "/approve PROPOSAL_ID [notes] - approve a pending proposal\n"
        "/reject PROPOSAL_ID [notes] - reject a pending proposal\n"
        "/enqueue PROPOSAL_ID - queue an approved proposal\n"
        "/queue - list execution queue\n"
        "/process_queue QUEUE_ID|all - process queued paper/live execution\n"
        "/open_signals - tracked active signals\n"
        "/outcomes - ledger outcome quality summary\n"
        "/strategy_report - ledger-backed strategy audit\n"
        "/performance - paper trading and signal-quality dashboard\n"
        "/health - bot operations health\n"
        "/auto_status - automation, proposal, and execution safety status\n"
        "/schedule_status - scheduler bucket status\n"
        "/pause_auto [reason] - pause scheduled scans and auto proposals\n"
        "/resume_auto [reason] - resume scheduled scans and clear runtime kill switch\n"
        "/kill_switch [reason] - immediately pause automation and block execution\n"
        "/daily_summary - latest workflow summary\n"
        "/notify SYMBOL - force-send the current signal snapshot\n"
    )

    def __init__(
        self,
        *,
        settings: AppSettings,
        notifier: TelegramNotifier,
        live_signals: LiveSignalService,
        market_screener: Any | None = None,
        workflow_service: Any | None = None,
        proposal_service: Any | None = None,
        execution_coordinator: Any | None = None,
        execution_queue_repository: Any | None = None,
        paper_trading_service: Any | None = None,
        automation_service: Any | None = None,
        runtime_state_repository: "RuntimeStateRepository" | Any,
        run_log_repository: "RunLogRepository" | Any,
    ):
        self.settings = settings
        self.notifier = notifier
        self.live_signals = live_signals
        self.market_screener = market_screener
        self.workflow_service = workflow_service
        self.proposal_service = proposal_service
        self.execution_coordinator = execution_coordinator
        self.execution_queue_repository = execution_queue_repository
        self.paper_trading_service = paper_trading_service
        self.automation = automation_service
        self.state = runtime_state_repository
        self.logs = run_log_repository
        self._approval_adapter = SignalApprovalAdapter()
        self._scan_executor = ThreadPoolExecutor(max_workers=1)
        self._scan_lock = threading.Lock()
        self._active_scan_future: Any | None = None
        self._active_scan_cancel_event: threading.Event | None = None
        self._active_scan_started_at: datetime | None = None
        self._active_scan_label: str | None = None
        self._scan_generation = 0

    def run_forever(self) -> None:
        """Run the long-polling command bot and scheduled alert loop."""

        if not self.notifier.enabled:
            raise RuntimeError("Telegram is not enabled or credentials are missing.")

        self.state.set("telegram_bot_started_at", utc_now().isoformat())
        try:
            self.notifier.delete_webhook(drop_pending_updates=False)
        except Exception as exc:
            self._log_loop_error("telegram_bot_delete_webhook_error", exc)
        logger.info("Telegram bot loop started")
        while True:
            try:
                self.poll_once(timeout_seconds=self.settings.telegram_poll_interval_seconds)
            except Exception as exc:
                self._log_loop_error("telegram_bot_poll_error", exc)

            try:
                self.run_scheduled_tasks()
            except Exception as exc:
                self._log_loop_error("telegram_bot_alert_error", exc)

            time.sleep(1)

    def poll_once(self, *, timeout_seconds: int = 0) -> int:
        """Process one batch of Telegram updates."""

        offset = self._next_update_offset()
        updates = self.notifier.get_updates(
            offset=offset,
            timeout=max(timeout_seconds, 0),
            limit=20,
        )
        processed = 0
        for update in updates:
            processed += int(self.handle_update(update))
        self.state.set("telegram_last_poll_at", utc_now().isoformat())
        return processed

    def handle_update(self, update: dict) -> bool:
        """Handle a single Telegram update for polling or webhook delivery."""

        update_id = update.get("update_id")
        if update_id is not None:
            self.state.set("telegram_last_update_id", str(update_id))

        message = update.get("message") or {}
        chat = message.get("chat") or {}
        text = str(message.get("text") or "").strip()
        chat_id = str(chat.get("id") or "")
        if not text or not chat_id:
            return False
        if not self._chat_allowed(chat_id):
            return False

        self.handle_text(chat_id, text)
        return True

    def send_due_alerts(self) -> int:
        """Send hourly alerts when configured and due."""

        if not self.settings.telegram_hourly_alerts_enabled:
            return 0

        sent = 0
        for symbol in self.settings.telegram_alert_symbols:
            state_key = f"telegram_hourly_alert:{symbol}"
            last_sent_raw = self.state.get(state_key)
            if last_sent_raw and not self._is_due(last_sent_raw):
                continue

            response = self._run_with_timeout(
                self.live_signals.send_signal_alert_with_label,
                symbol,
                previous_state="scheduled",
            )
            if response.sent:
                self.state.set(state_key, utc_now().isoformat())
                self.logs.log("telegram_hourly_alert_sent", {"symbol": symbol})
                sent += 1
        return sent

    def run_scheduled_tasks(self) -> int:
        """Run hourly compatibility alerts plus the workflow scheduler if configured."""

        sent = self.send_due_alerts()
        if self.workflow_service is not None:
            result = self.workflow_service.run_scheduled_tasks()
            sent += int(result.get("alerts_sent", 0))
        return sent

    def handle_text(self, chat_id: str, text: str) -> None:
        """Handle one Telegram command message."""

        try:
            self._handle_text_impl(chat_id, text)
        except Exception as exc:
            logger.exception("Telegram command handling failed: %s", exc)
            self.logs.log(
                "telegram_command_error",
                {"chat_id": chat_id, "text": text, "error": str(exc)},
            )
            self.notifier.send_text(
                f"Command failed for `{text}`.\n{exc}",
                chat_id=chat_id,
            )

    def _handle_text_impl(self, chat_id: str, text: str) -> None:
        parts = text.split()
        command = parts[0].split("@", 1)[0].lower()
        args = parts[1:]

        if command in {"/start", "/help"}:
            self.notifier.send_text(self.HELP_TEXT, chat_id=chat_id)
            return

        if command == "/signal":
            if not args:
                self.notifier.send_text("Usage: /signal SYMBOL", chat_id=chat_id)
                return
            symbol = self._parse_symbol_arg(args)
            if symbol is None:
                self.notifier.send_text(
                    "Usage: /signal SYMBOL\n"
                    "Examples: /signal NVDA, /signal AMD\n"
                    "For ranked scans use /scan 5 or /intraday_scan 5.",
                    chat_id=chat_id,
                )
                return
            if self.market_screener is not None and hasattr(self.market_screener, "analyze_symbol"):
                snapshot = self._run_with_timeout(
                    self.market_screener.analyze_symbol,
                    symbol,
                    force_refresh=False,
                )
            else:
                snapshot = self._run_with_timeout(
                    self.live_signals.get_latest_signal,
                    symbol,
                    commit=False,
                    notify=False,
                )
            self.notifier.send_text(
                self.notifier.format_signal_message(snapshot, previous_state="query"),
                chat_id=chat_id,
            )
            return

        if command == "/price":
            if not args:
                self.notifier.send_text("Usage: /price SYMBOL", chat_id=chat_id)
                return
            symbol = self._parse_symbol_arg(args)
            if symbol is None:
                self.notifier.send_text("Usage: /price SYMBOL\nExample: /price NVDA", chat_id=chat_id)
                return
            snapshot = self._run_with_timeout(
                self.live_signals.get_latest_signal,
                symbol,
                commit=False,
                notify=False,
            )
            self.notifier.send_text(
                self.notifier.format_price_message(snapshot),
                chat_id=chat_id,
            )
            return

        if command in {
            "/scan",
            "/scan_top100",
            "/scan_details",
            "/screener",
            "/screener_details",
            "/supported_scan",
            "/intraday_scan",
            "/validated_scan",
        }:
            limit, requested_symbols, include_details, universe_limit, requested_timeframes = self._parse_scan_args(args)
            include_details = include_details or command in {"/scan_details", "/screener_details"}
            if command == "/scan_top100":
                universe_limit = int(getattr(self.settings, "market_universe_limit", 100) or 100)
            supported_only = command == "/supported_scan"
            validated_only = command == "/validated_scan"
            intraday = command == "/intraday_scan"
            self.notifier.send_text(
                self._scan_message(
                    limit=limit,
                    supported_only=supported_only,
                    validated_only=validated_only,
                    intraday=intraday,
                    requested_symbols=requested_symbols,
                    include_details=include_details,
                    universe_limit=universe_limit,
                    requested_timeframes=requested_timeframes,
                    chat_id=chat_id,
                ),
                chat_id=chat_id,
            )
            return

        if command == "/scan_status":
            self.notifier.send_text(self._scan_status_message(), chat_id=chat_id)
            return

        if command == "/cancel_scan":
            self.notifier.send_text(self._cancel_scan_message(), chat_id=chat_id)
            return

        if command == "/notify":
            if not args:
                self.notifier.send_text("Usage: /notify SYMBOL", chat_id=chat_id)
                return
            symbol = self._parse_symbol_arg(args)
            if symbol is None:
                self.notifier.send_text("Usage: /notify SYMBOL\nExample: /notify NVDA", chat_id=chat_id)
                return
            response = self._run_with_timeout(
                self.live_signals.send_signal_alert_with_label,
                symbol,
                previous_state="telegram",
            )
            detail = response.detail if response.sent else f"Failed: {response.detail}"
            self.notifier.send_text(detail, chat_id=chat_id)
            return

        if command == "/propose":
            self.notifier.send_text(self._propose_message(args), chat_id=chat_id)
            return

        if command == "/propose_top":
            self.notifier.send_text(self._propose_top_message(args), chat_id=chat_id)
            return

        if command == "/proposals":
            self.notifier.send_text(self._proposals_message(args), chat_id=chat_id)
            return

        if command == "/approve":
            self.notifier.send_text(self._approve_message(chat_id, args), chat_id=chat_id)
            return

        if command == "/reject":
            self.notifier.send_text(self._reject_message(chat_id, args), chat_id=chat_id)
            return

        if command == "/enqueue":
            self.notifier.send_text(self._enqueue_message(args), chat_id=chat_id)
            return

        if command == "/queue":
            self.notifier.send_text(self._queue_message(), chat_id=chat_id)
            return

        if command == "/process_queue":
            self.notifier.send_text(self._process_queue_message(args), chat_id=chat_id)
            return

        if command == "/open_signals":
            if self.workflow_service is None:
                self.notifier.send_text("Workflow service is not configured.", chat_id=chat_id)
                return
            status = self.workflow_service.status()
            records = self.workflow_service.tracked_signals.list(status="open", limit=10)
            message = self.notifier.format_daily_summary(open_signals=records, recent_alerts=[])
            message = f"{message}\nScheduler enabled: {'yes' if status.scheduler_enabled else 'no'}"
            self.notifier.send_text(message, chat_id=chat_id)
            return

        if command == "/outcomes":
            self.notifier.send_text(self._outcomes_message(), chat_id=chat_id)
            return

        if command in {"/strategy_report", "/strategy_audit"}:
            self.notifier.send_text(self._strategy_report_message(), chat_id=chat_id)
            return

        if command in {"/performance", "/paper_summary"}:
            self.notifier.send_text(self._performance_message(), chat_id=chat_id)
            return

        if command == "/health":
            self.notifier.send_text(self._health_message(), chat_id=chat_id)
            return

        if command == "/auto_status":
            self.notifier.send_text(self._automation_status_message(), chat_id=chat_id)
            return

        if command == "/schedule_status":
            self.notifier.send_text(self._schedule_status_message(), chat_id=chat_id)
            return

        if command == "/pause_auto":
            self.notifier.send_text(self._automation_change_message("pause", args), chat_id=chat_id)
            return

        if command == "/resume_auto":
            self.notifier.send_text(self._automation_change_message("resume", args), chat_id=chat_id)
            return

        if command == "/kill_switch":
            self.notifier.send_text(self._automation_change_message("kill_switch", args), chat_id=chat_id)
            return

        if command == "/daily_summary":
            if self.workflow_service is None:
                self.notifier.send_text("Workflow service is not configured.", chat_id=chat_id)
                return
            result = self._run_with_timeout(self.workflow_service.send_daily_summary, notify=False)
            self.notifier.send_text(result.detail, chat_id=chat_id)
            summary = self.notifier.format_daily_summary(
                open_signals=self.workflow_service.tracked_signals.list(status="open", limit=10),
                recent_alerts=self.workflow_service.alert_history.list(limit=10),
            )
            self.notifier.send_text(summary, chat_id=chat_id)
            return

        self.notifier.send_text(self.HELP_TEXT, chat_id=chat_id)

    def _propose_message(self, args: list[str]) -> str:
        if self.proposal_service is None or self.market_screener is None:
            return "Proposal services are not configured."
        if not args:
            return "Usage: /propose SYMBOL [amount]\nExample: /propose NVDA 20"

        symbol = self._parse_symbol_arg(args[:1])
        if symbol is None:
            return "Usage: /propose SYMBOL [amount]\nExample: /propose NVDA 20"
        amount = self._parse_amount(args[1:], default=float(self.settings.default_trade_amount_usd))
        if amount is None:
            return "Amount must be a positive number. Example: /propose NVDA 20"

        snapshot = self._run_with_timeout(
            self.market_screener.analyze_symbol,
            symbol,
            force_refresh=True,
        )
        if not bool(getattr(snapshot, "execution_ready", False)):
            blockers = self._join_items(
                list((getattr(snapshot, "metadata", {}) or {}).get("execution_blockers") or [])
                or list(getattr(snapshot, "reject_reasons", []) or [])
            )
            return (
                f"No proposal created for {symbol}.\n"
                f"Verdict: {str(getattr(snapshot, 'direction_label', 'no_trade')).upper()} | "
                f"Score: {float(getattr(snapshot, 'score', 0.0) or 0.0):.1f}/100\n"
                f"Reason: {blockers or getattr(snapshot, 'rationale', 'not execution-ready')}\n"
                "The bot will not force a trade without a live setup, stop, target, and risk/reward plan."
            )
        if str(getattr(snapshot, "signal_role", "") or "").lower() == "entry_short":
            return "No proposal created. Short-entry execution is not wired safely yet."
        if not getattr(snapshot, "stop_loss", None):
            return "No proposal created. A stop loss is required before submitting any order."

        request = self._approval_adapter.build_proposal_request(
            snapshot,
            amount_usd=amount,
            notes="Created from Telegram /propose after live screener validation.",
        )
        proposal = self._run_with_timeout(self.proposal_service.create_proposal, request)
        return self._format_proposal(
            proposal,
            header="Proposal created",
            footer=(
                f"Approve: /approve {proposal.id}\n"
                f"Reject: /reject {proposal.id}"
            ),
        )

    def _propose_top_message(self, args: list[str]) -> str:
        if self.proposal_service is None or self.market_screener is None:
            return "Proposal services are not configured."
        amount = self._parse_amount(args[:1], default=float(self.settings.default_trade_amount_usd))
        if amount is None:
            return "Amount must be a positive number. Example: /propose_top 20"
        default_universe_limit = int(
            getattr(
                self.settings,
                "telegram_propose_top_default_universe_limit",
                min(int(getattr(self.settings, "market_universe_limit", 25) or 25), 25),
            )
            or 25
        )
        universe_limit = self._parse_optional_limit(args[1:], default=default_universe_limit)

        timeframes = list(getattr(self.settings, "intelligent_scan_timeframes", []) or self.settings.screener_default_timeframes)
        symbols = resolve_universe(self.settings, limit=universe_limit)
        kwargs = {
            "symbols": symbols,
            "timeframes": timeframes,
            "limit": max(1, int(getattr(self.settings, "screener_top_k", 5) or 5)),
            "validated_only": bool(getattr(self.settings, "require_backtest_validation_for_alerts", True)),
            "notify": False,
            "force_refresh": True,
        }
        if "scan_task" in inspect.signature(self.market_screener.scan_universe).parameters:
            kwargs["scan_task"] = "telegram_propose_top"
        response = self._run_scan_with_timeout("telegram_propose_top", **kwargs)
        eligible = [
            item
            for item in response.candidates
            if bool(getattr(item, "execution_ready", False))
            and bool((getattr(item, "metadata", {}) or {}).get("alert_eligible", False))
            and getattr(item, "stop_loss", None)
        ]
        if not eligible:
            best = response.candidates[0] if response.candidates else None
            if best is None:
                return (
                    "No proposal created.\n"
                    f"Scanned: {response.evaluated_symbols} symbols | "
                    f"Strategy checks: {response.evaluated_strategy_runs}\n"
                    "No execution-ready, backtest-validated candidates passed the current filters."
                )
            blockers = self._join_items(
                list((best.metadata or {}).get("execution_blockers") or [])
                or list(best.reject_reasons or [])
            )
            return (
                "No proposal created.\n"
                f"Best non-actionable setup: {best.symbol} | "
                f"{best.direction_label or best.state.value} | Score {float(best.score or 0.0):.1f}/100\n"
                f"Reason: {blockers or 'not execution-ready'}\n"
                f"Scanned: {response.evaluated_symbols} symbols | "
                f"Strategy checks: {response.evaluated_strategy_runs}"
            )

        snapshot = eligible[0]
        request = self._approval_adapter.build_proposal_request(
            snapshot,
            amount_usd=amount,
            notes=(
                "Created from Telegram /propose_top after top-universe scan, "
                "live setup validation, and backtest gating."
            ),
        )
        proposal = self._run_with_timeout(self.proposal_service.create_proposal, request)
        return self._format_proposal(
            proposal,
            header=(
                "Top opportunity proposal created\n"
                f"Rank: {snapshot.rank or 1} | Score: {float(snapshot.score or 0.0):.1f}/100 | "
                f"Confidence: {snapshot.confidence_label or 'n/a'}"
            ),
            footer=(
                f"Approve: /approve {proposal.id}\n"
                f"Reject: /reject {proposal.id}\n"
                f"Scanned: {response.evaluated_symbols} symbols | "
                f"Strategy checks: {response.evaluated_strategy_runs}"
            ),
        )

    def _proposals_message(self, args: list[str]) -> str:
        if self.proposal_service is None:
            return "Proposal service is not configured."
        status_filter = ApprovalStatus.PENDING
        if args:
            try:
                status_filter = ApprovalStatus(args[0].lower())
            except ValueError:
                return "Usage: /proposals [pending|approved|rejected|executed|expired]"
        proposals = self._run_with_timeout(self.proposal_service.list_proposals, status=status_filter)
        if not proposals:
            return f"No {status_filter.value} proposals."
        lines = [f"{status_filter.value.title()} proposals:"]
        for proposal in proposals[:10]:
            order = proposal.order
            lines.append(
                f"{proposal.id} | {order.symbol} {order.side.value.upper()} "
                f"${float(order.amount_usd):.2f} @ {float(order.proposed_price):.2f} | "
                f"SL {self._fmt_price(order.stop_loss)} | TP {self._fmt_price(order.take_profit)}"
            )
        return "\n".join(lines)

    def _approve_message(self, chat_id: str, args: list[str]) -> str:
        if self.proposal_service is None:
            return "Proposal service is not configured."
        if not args:
            return "Usage: /approve PROPOSAL_ID [notes]"
        proposal_id = args[0]
        notes = " ".join(args[1:]) or "Approved from Telegram."
        proposal = self._run_with_timeout(
            self.proposal_service.approve_proposal,
            proposal_id,
            ApprovalDecisionRequest(reviewer=f"telegram:{chat_id}", notes=notes),
        )
        return self._format_proposal(
            proposal,
            header="Proposal approved",
            footer=self._approval_footer(proposal.id),
        )

    def _reject_message(self, chat_id: str, args: list[str]) -> str:
        if self.proposal_service is None:
            return "Proposal service is not configured."
        if not args:
            return "Usage: /reject PROPOSAL_ID [notes]"
        proposal_id = args[0]
        notes = " ".join(args[1:]) or "Rejected from Telegram."
        proposal = self._run_with_timeout(
            self.proposal_service.reject_proposal,
            proposal_id,
            ApprovalDecisionRequest(reviewer=f"telegram:{chat_id}", notes=notes),
        )
        return self._format_proposal(proposal, header="Proposal rejected")

    def _enqueue_message(self, args: list[str]) -> str:
        if self.execution_coordinator is None:
            return "Execution coordinator is not configured."
        if not args:
            return "Usage: /enqueue PROPOSAL_ID"
        record = self._run_with_timeout(
            self.execution_coordinator.enqueue_approved_proposal,
            args[0],
        )
        return self._format_queue_record(
            record,
            header="Proposal queued",
            footer=f"Process it: /process_queue {record.id}",
        )

    def _queue_message(self) -> str:
        queue_repo = self.execution_queue_repository or getattr(self.execution_coordinator, "queue", None)
        if queue_repo is None:
            return "Execution queue is not configured."
        records = self._run_with_timeout(queue_repo.list, limit=10)
        if not records:
            return "Execution queue is empty."
        lines = ["Execution queue:"]
        for record in records[:10]:
            lines.append(
                f"{record.id} | {record.symbol} | {record.status} | "
                f"mode {record.mode} | reason {record.validation_reason or 'n/a'}"
            )
        return "\n".join(lines)

    def _process_queue_message(self, args: list[str]) -> str:
        if self.execution_coordinator is None:
            return "Execution coordinator is not configured."
        if not args:
            return "Usage: /process_queue QUEUE_ID|all"
        if self.settings.execution_mode == "live" and "CONFIRM_LIVE" not in args:
            return (
                "Live execution requires explicit confirmation.\n"
                "Use: /process_queue QUEUE_ID CONFIRM_LIVE\n"
                "Paper mode does not require this."
            )
        target = args[0]
        if target.lower() == "all":
            records = self._run_with_timeout(self.execution_coordinator.process_ready_queue)
            if not records:
                return "No queued records were processed."
            return "\n\n".join(self._format_queue_record(record, header="Queue processed") for record in records)
        record = self._run_with_timeout(self.execution_coordinator.process_queue_item, target)
        return self._format_queue_record(record, header="Queue processed")

    def _approval_footer(self, proposal_id: str) -> str:
        if not bool(getattr(self.settings, "auto_execute_after_approval", False)):
            return f"Queue it: /enqueue {proposal_id}"
        if self.execution_coordinator is None:
            return "Auto-execute after approval is enabled, but the execution coordinator is not configured."
        try:
            queued = self._run_with_timeout(
                self.execution_coordinator.enqueue_approved_proposal,
                proposal_id,
            )
            processed = self._run_with_timeout(
                self.execution_coordinator.process_queue_item,
                queued.id,
            )
        except Exception as exc:  # noqa: BLE001
            return (
                "Auto-execute after approval is enabled, but execution was not completed.\n"
                f"Reason: {exc}\n"
                f"Manual queue check: /queue"
            )
        return "\n".join(
            [
                "Auto-execute after approval is enabled.",
                f"Queue ID: {processed.id}",
                f"Status: {processed.status}",
                f"Reason: {processed.validation_reason or 'ready'}",
            ]
        )

    def _automation_status_message(self) -> str:
        if self.automation is None:
            return "Automation service is not configured."
        status = self.automation.status()
        lines = [
            "Automation status",
            f"Paused: {'yes' if status.paused else 'no'}",
            f"Kill switch: {'on' if status.kill_switch_enabled else 'off'}",
            f"Auto propose: {'on' if status.auto_propose_enabled else 'off'}",
            f"Auto execute after approval: {'on' if status.auto_execute_after_approval else 'off'}",
            f"Execution mode: {status.execution_mode}",
            f"Real trading enabled: {'yes' if status.enable_real_trading else 'no'}",
            f"Require approval: {'yes' if status.require_approval else 'no'}",
            f"Reason: {status.reason or 'n/a'}",
        ]
        if self.workflow_service is not None and hasattr(self.workflow_service, "schedule_statuses"):
            buckets = list(self.workflow_service.schedule_statuses())
            next_due = self._next_due_bucket(buckets)
            failed = self._latest_failed_bucket(buckets)
            workflow_health = self.workflow_service.health_summary() if hasattr(self.workflow_service, "health_summary") else {}
            lines.append(f"Next due: {next_due.name} at {next_due.next_due_at}" if next_due else "Next due: n/a")
            lines.append(f"Last successful scan: {workflow_health.get('last_successful_screener_run_at') or 'n/a'}")
            lines.append(f"Latest failed bucket: {failed.name} ({failed.last_error or failed.last_status})" if failed else "Latest failed bucket: none")
        return "\n".join(lines)

    def _schedule_status_message(self) -> str:
        if self.workflow_service is None or not hasattr(self.workflow_service, "schedule_statuses"):
            return "Workflow scheduler is not configured."
        buckets = list(self.workflow_service.schedule_statuses())
        lines = ["Schedule status"]
        for bucket in buckets:
            lines.append(
                " | ".join(
                    [
                        bucket.name,
                        "enabled" if bucket.enabled else "disabled",
                        "paused" if bucket.paused else "active",
                        f"last={bucket.last_status or 'n/a'}",
                        f"next={bucket.next_due_at or 'n/a'}",
                    ]
                )
            )
            if bucket.last_error:
                lines.append(f"  error: {bucket.last_error}")
        return "\n".join(lines)

    @staticmethod
    def _next_due_bucket(buckets: list[Any]) -> Any | None:
        due = [item for item in buckets if getattr(item, "enabled", False) and getattr(item, "next_due_at", None)]
        return sorted(due, key=lambda item: str(item.next_due_at))[0] if due else None

    @staticmethod
    def _latest_failed_bucket(buckets: list[Any]) -> Any | None:
        failed = [item for item in buckets if str(getattr(item, "last_status", "")).lower() == "error"]
        return sorted(failed, key=lambda item: str(getattr(item, "last_run_at", "") or ""), reverse=True)[0] if failed else None

    def _automation_change_message(self, action: str, args: list[str]) -> str:
        if self.automation is None:
            return "Automation service is not configured."
        reason = " ".join(args)
        if action == "pause":
            status = self.automation.pause(reason=reason or "Paused from Telegram.")
            header = "Automation paused"
        elif action == "resume":
            status = self.automation.resume(reason=reason or "Resumed from Telegram.")
            header = "Automation resumed"
        elif action == "kill_switch":
            status = self.automation.enable_kill_switch(reason=reason or "Kill switch from Telegram.")
            header = "Kill switch enabled"
        else:
            return "Unknown automation action."
        return "\n".join(
            [
                header,
                f"Paused: {'yes' if status.paused else 'no'}",
                f"Kill switch: {'on' if status.kill_switch_enabled else 'off'}",
                f"Reason: {status.reason or 'n/a'}",
            ]
        )

    def _outcomes_message(self) -> str:
        repository = self._ledger_repository()
        if repository is None:
            return "Outcome ledger is not configured."
        stats = self._run_with_timeout(repository.summary_stats)
        by_status = stats.get("by_status") or {}
        lines = [
            "Outcome ledger",
            f"Total outcomes: {int(stats.get('total_outcomes') or 0)}",
            (
                "Status: "
                + (
                    ", ".join(f"{status}={count}" for status, count in sorted(by_status.items()))
                    if by_status
                    else "none"
                )
            ),
            (
                f"Closed: {int(stats.get('closed_count') or 0)} | "
                f"W/L: {int(stats.get('wins') or 0)}/{int(stats.get('losses') or 0)} | "
                f"Win rate: {self._fmt_pct(stats.get('win_rate'))}"
            ),
            (
                f"PF: {self._fmt_decimal(stats.get('profit_factor'))} | "
                f"Avg R: {self._fmt_r(stats.get('avg_r_multiple'))} | "
                f"Avg hold: {self._fmt_hours(stats.get('avg_hold_hours'))}"
            ),
        ]
        strategies = list(stats.get("by_strategy") or [])
        if strategies:
            lines.append("By strategy:")
            for item in strategies[:5]:
                lines.append(
                    f"{item.get('strategy_name')}: "
                    f"closed {int(item.get('closed') or 0)}, "
                    f"WR {self._fmt_pct(item.get('win_rate'))}, "
                    f"PF {self._fmt_decimal(item.get('profit_factor'))}, "
                    f"avgR {self._fmt_r(item.get('avg_r_multiple'))}"
                )
        return "\n".join(lines)

    def _strategy_report_message(self) -> str:
        repository = self._ledger_repository()
        if repository is None:
            return "Outcome ledger is not configured."
        audit = self._run_with_timeout(repository.strategy_audit, min_closed=20)
        overall = audit.get("overall") or {}
        lines = [
            "Strategy quality audit",
            f"Generated: {audit.get('generated_at') or 'n/a'}",
            f"Decision floor: {int(audit.get('min_closed_for_decision') or 20)} closed outcomes",
            "Overall:",
            self._format_audit_item(overall),
            f"Recommendation: {overall.get('recommendation') or 'n/a'} - {overall.get('recommendation_reason') or 'n/a'}",
        ]
        self._append_audit_section(lines, "By strategy", audit.get("by_strategy") or [], limit=5)
        self._append_audit_section(lines, "By score bucket", audit.get("by_score_bucket") or [], limit=6)
        self._append_audit_section(lines, "By timeframe", audit.get("by_timeframe") or [], limit=5)
        return "\n".join(lines)

    def _append_audit_section(self, lines: list[str], title: str, items: list[dict[str, Any]], *, limit: int) -> None:
        lines.append(title + ":")
        if not items:
            lines.append("no data")
            return
        for item in items[:limit]:
            lines.append(self._format_audit_item(item))

    def _format_audit_item(self, item: dict[str, Any]) -> str:
        return (
            f"{item.get('name') or '-'} | "
            f"alerts {int(item.get('total_alerts') or 0)} | "
            f"matched {int(item.get('matched_count') or 0)} ({self._fmt_pct(item.get('match_rate'))}) | "
            f"closed {int(item.get('closed_count') or 0)} | "
            f"WR {self._fmt_pct(item.get('win_rate'))} | "
            f"PF {self._fmt_decimal(item.get('profit_factor'))} | "
            f"avgR {self._fmt_r(item.get('avg_r_multiple'))} | "
            f"{item.get('recommendation') or 'n/a'}"
        )

    def _health_message(self) -> str:
        if self.workflow_service is None:
            return "Workflow service is not configured."
        health = self.workflow_service.health_summary()
        return "\n".join(
            [
                "Bot health",
                f"Status: {str(health.get('status') or 'unknown').upper()}",
                f"Reason: {health.get('reason') or 'n/a'}",
                f"Last screener: {health.get('last_successful_screener_run_at') or 'never'}",
                f"Last ledger cycle: {health.get('last_successful_ledger_cycle_at') or 'never'}",
                (
                    f"Pending matches: {int(health.get('pending_match_count') or 0)} | "
                    f">24h: {int(health.get('pending_match_older_than_24h_count') or 0)}"
                ),
                f"Model mode: {health.get('model_deployment_mode') or 'shadow'}",
                f"Meta-model: {health.get('active_meta_model_version') or 'not deployed'}",
                f"Regime: {health.get('current_regime_label') or 'not deployed'}",
                (
                    "Last eToro error: "
                    f"{health.get('last_etoro_api_error') or 'none'}"
                    + (
                        f" at {health.get('last_etoro_api_error_at')}"
                        if health.get("last_etoro_api_error_at")
                        else ""
                    )
                ),
            ]
        )

    def _performance_message(self) -> str:
        if self.paper_trading_service is None:
            return "Paper performance service is not configured."
        dashboard = (
            self.paper_trading_service.dashboard()
            if hasattr(self.paper_trading_service, "dashboard")
            else {"paper": self.paper_trading_service.summary()}
        )
        paper = dashboard.get("paper")
        paper_data = paper.model_dump() if hasattr(paper, "model_dump") else dict(paper or {})
        lines = [
            "AlgoBot performance",
            f"Mode: {paper_data.get('mode', 'paper')}",
            (
                f"Paper P&L: realized {self._fmt_decimal(paper_data.get('realized_pnl_usd'))} | "
                f"unrealized {self._fmt_decimal(paper_data.get('unrealized_pnl_usd'))}"
            ),
            (
                f"Trades: {int(paper_data.get('total_trades') or 0)} | "
                f"Open: {int(paper_data.get('open_positions') or 0)} | "
                f"Win rate: {self._fmt_decimal(paper_data.get('win_rate'))}% | "
                f"Expectancy: {self._fmt_decimal(paper_data.get('expectancy_usd'))}"
            ),
        ]
        provider_health = dict(dashboard.get("provider_health") or {})
        if provider_health:
            lines.append(
                "Data: "
                f"history {provider_health.get('history_provider', 'n/a')} | "
                f"quote {provider_health.get('quote_provider', 'n/a')} | "
                f"freshness {provider_health.get('freshness_status', 'n/a')}"
            )
        suggestions = list(dashboard.get("calibration_suggestions") or [])
        if suggestions:
            lines.append("Next calibration:")
            lines.extend(f"- {item}" for item in suggestions[:3])
        return "\n".join(lines)

    def _ledger_repository(self):
        if self.workflow_service is None:
            return None
        ledger_service = getattr(self.workflow_service, "ledger_service", None)
        if ledger_service is None:
            return None
        return getattr(ledger_service, "repository", None)

    def _next_update_offset(self) -> int | None:
        last_update_id = self.state.get("telegram_last_update_id")
        if last_update_id is None:
            return None
        try:
            return int(last_update_id) + 1
        except ValueError:
            return None

    def _chat_allowed(self, chat_id: str) -> bool:
        allowed = self.settings.telegram_allowed_chat_ids or [self.settings.telegram_chat_id]
        return chat_id in [str(item) for item in allowed if str(item)]

    def _log_loop_error(self, event_type: str, exc: Exception) -> None:
        logger.exception("Telegram bot loop error: %s", exc)
        self.logs.log(event_type, {"error": str(exc)})

    def _scan_message(
        self,
        *,
        limit: int,
        supported_only: bool,
        validated_only: bool,
        intraday: bool,
        requested_symbols: list[str] | None = None,
        include_details: bool = False,
        universe_limit: int | None = None,
        requested_timeframes: list[str] | None = None,
        chat_id: str | None = None,
    ) -> str:
        if self.market_screener is None:
            response = self._run_with_timeout(
                self.live_signals.scan_market,
                limit=limit,
                supported_only=supported_only,
                commit=False,
                notify=False,
            )
            return self.notifier.format_scan_message(response)

        symbols = None
        if requested_symbols:
            symbols = requested_symbols
        elif supported_only:
            symbols = list(self.settings.allowed_instruments)
        else:
            scan_universe_limit = int(
                universe_limit
                or getattr(self.settings, "telegram_scan_default_universe_limit", 25)
                or 25
            )
            if universe_limit is not None:
                symbols = list(DEFAULT_TOP_100_US[: max(1, min(scan_universe_limit, len(DEFAULT_TOP_100_US)))])
            else:
                symbols = resolve_universe(
                    self.settings,
                    limit=scan_universe_limit,
                )
        timeframes = list(requested_timeframes or []) or (
            list(self.settings.screener_intraday_timeframes)
            if intraday
            else list(self.settings.screener_default_timeframes)
        )
        kwargs = {
            "symbols": symbols,
            "timeframes": timeframes,
            "limit": limit,
            "validated_only": validated_only,
            "notify": False,
            "force_refresh": False,
        }
        scan_signature = inspect.signature(self.market_screener.scan_universe)
        supports_scan_task = "scan_task" in scan_signature.parameters
        if supports_scan_task:
            kwargs["scan_task"] = (
                "manual_intraday_scan"
                if intraday
                else "manual_validated_scan"
                if validated_only
                else "manual_supported_scan"
                if supported_only
                else "manual_scan"
            )
        scan_task = str(kwargs.get("scan_task") or "manual_scan")
        task_label = (
            "intraday_scan"
            if intraday
            else "validated_scan"
            if validated_only
            else "supported_scan"
            if supported_only
            else "scan"
        )
        scope_line = self._scan_scope_line(
            requested_symbols=requested_symbols,
            include_details=include_details,
            limit=limit,
            universe_limit=universe_limit,
        )
        estimated_checks = len(symbols or []) * len(timeframes or [])
        if estimated_checks > 50 and scan_task == "manual_scan":
            scan_task = "manual_deep_scan"
            if supports_scan_task:
                kwargs["scan_task"] = scan_task
        if chat_id and estimated_checks > 50:
            return self._start_scan_background(
                scan_task,
                chat_id=chat_id,
                task_label=task_label,
                include_details=include_details,
                scope_line=scope_line,
                estimated_symbols=len(symbols or []),
                estimated_timeframes=list(timeframes or []),
                **kwargs,
            )

        response = self._run_scan_with_timeout(scan_task, **kwargs)
        self._track_scan_candidates(response, origin=scan_task)
        try:
            message = self.notifier.format_screener_summary(
                response,
                task_label=task_label,
                include_other_watches=include_details,
            )
        except TypeError:
            message = self.notifier.format_screener_summary(response)
        return f"{message}\n{scope_line}"

    def _scan_scope_line(
        self,
        *,
        requested_symbols: list[str] | None,
        include_details: bool,
        limit: int,
        universe_limit: int | None,
    ) -> str:
        if requested_symbols:
            scanned = ", ".join(requested_symbols[:8])
            extra = "..." if len(requested_symbols) > 8 else ""
            return f"Scope: requested symbols {scanned}{extra}"
        result_scope = f"showing up to {limit} result(s)" if include_details else "best setup shown"
        scan_limit = int(
            universe_limit
            or getattr(self.settings, "telegram_scan_default_universe_limit", 25)
            or 25
        )
        return f"Scope: scanned up to {scan_limit} symbols; {result_scope}."

    def _start_scan_background(
        self,
        label: str,
        *,
        chat_id: str,
        task_label: str,
        include_details: bool,
        scope_line: str,
        estimated_symbols: int,
        estimated_timeframes: list[str],
        **kwargs,
    ) -> str:
        future, _cancel_event, release_scan = self._submit_scan(label, **kwargs)

        def _send_result(completed_future) -> None:
            try:
                response = completed_future.result()
                self._track_scan_candidates(response, origin=label)
                try:
                    message = self.notifier.format_screener_summary(
                        response,
                        task_label=task_label,
                        include_other_watches=include_details,
                    )
                except TypeError:
                    message = self.notifier.format_screener_summary(response)
                message = f"{message}\n{scope_line}"
            except Exception as exc:  # noqa: BLE001
                message = (
                    f"Deep scan failed for {label.replace('_', ' ')}.\n"
                    f"{exc}\n"
                    "Use /scan_status before retrying."
                )
                self.logs.log("telegram_deep_scan_error", {"label": label, "error": str(exc)})
            self.notifier.send_text(message, chat_id=chat_id)

        future.add_done_callback(_send_result)
        future.add_done_callback(release_scan)
        timeframe_text = ", ".join(estimated_timeframes)
        return (
            "Deep scan started.\n"
            f"Universe: {estimated_symbols} symbol(s)\n"
            f"Timeframes: {timeframe_text}\n"
            "I will send the best trade signal here when it finishes.\n"
            "Use /scan_status to check progress or /cancel_scan to stop it."
        )

    @staticmethod
    def _format_proposal(proposal: Any, *, header: str, footer: str | None = None) -> str:
        order = proposal.order
        lines = [
            header,
            f"ID: {proposal.id}",
            f"Status: {proposal.status.value if hasattr(proposal.status, 'value') else proposal.status}",
            f"Order: {order.symbol} {order.side.value.upper()} ${float(order.amount_usd):.2f}",
            f"Entry: {float(order.proposed_price):.2f}",
            f"SL: {TelegramBotService._fmt_price(order.stop_loss)}",
            f"TP: {TelegramBotService._fmt_price(order.take_profit)}",
            f"Strategy: {order.strategy_name or 'n/a'}",
        ]
        if proposal.notes:
            lines.append(f"Notes: {proposal.notes}")
        if footer:
            lines.append(footer)
        return "\n".join(lines)

    @staticmethod
    def _format_queue_record(record: Any, *, header: str, footer: str | None = None) -> str:
        lines = [
            header,
            f"Queue ID: {record.id}",
            f"Proposal: {record.proposal_id}",
            f"Symbol: {record.symbol}",
            f"Status: {record.status}",
            f"Mode: {record.mode}",
            f"Ready: {'yes' if record.ready_for_execution else 'no'}",
            f"Quote: {TelegramBotService._fmt_price(record.latest_quote_price)}",
            f"Reason: {record.validation_reason or 'n/a'}",
        ]
        if footer:
            lines.append(footer)
        return "\n".join(lines)

    @staticmethod
    def _fmt_price(value: Any) -> str:
        if value in (None, ""):
            return "n/a"
        try:
            return f"{float(value):.2f}"
        except (TypeError, ValueError):
            return str(value)

    @staticmethod
    def _fmt_decimal(value: Any) -> str:
        if value in (None, ""):
            return "n/a"
        try:
            return f"{float(value):.2f}"
        except (TypeError, ValueError):
            return str(value)

    @staticmethod
    def _fmt_pct(value: Any) -> str:
        if value in (None, ""):
            return "n/a"
        try:
            return f"{float(value) * 100:.1f}%"
        except (TypeError, ValueError):
            return str(value)

    @staticmethod
    def _fmt_r(value: Any) -> str:
        if value in (None, ""):
            return "n/a"
        try:
            return f"{float(value):+.2f}R"
        except (TypeError, ValueError):
            return str(value)

    @staticmethod
    def _fmt_hours(value: Any) -> str:
        if value in (None, ""):
            return "n/a"
        try:
            hours = float(value)
        except (TypeError, ValueError):
            return str(value)
        if hours < 48:
            return f"{hours:.1f}h"
        return f"{hours / 24.0:.1f}d"

    @staticmethod
    def _join_items(items: list[Any], *, limit: int = 4) -> str:
        return ", ".join(str(item) for item in items[:limit] if str(item))

    def _run_with_timeout(self, func, *args, **kwargs):
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(func, *args, **kwargs)
        try:
            return future.result(timeout=self.settings.telegram_command_timeout_seconds)
        except FutureTimeoutError as exc:
            future.cancel()
            raise RuntimeError(
                f"Operation timed out after {self.settings.telegram_command_timeout_seconds}s"
            ) from exc
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def _track_scan_candidates(self, response: Any, *, origin: str) -> None:
        workflow = self.workflow_service
        if workflow is None or not hasattr(workflow, "_track_candidates"):
            return
        try:
            workflow._track_candidates(response, origin=origin)
        except Exception as exc:  # noqa: BLE001
            self.logs.log(
                "telegram_scan_tracking_error",
                {"origin": origin, "error": str(exc)},
            )

    def _submit_scan(self, label: str, **kwargs):
        if self.market_screener is None:
            raise RuntimeError("Market screener is not configured.")
        self._recover_stale_scan_if_needed()
        if not self._scan_lock.acquire(blocking=False):
            raise RuntimeError(self._scan_in_progress_message())

        cancel_event = threading.Event()
        self._scan_generation += 1
        scan_generation = self._scan_generation
        self._active_scan_cancel_event = cancel_event
        self._active_scan_started_at = utc_now()
        self._active_scan_label = label

        call_kwargs = dict(kwargs)
        if "cancel_event" in inspect.signature(self.market_screener.scan_universe).parameters:
            call_kwargs["cancel_event"] = cancel_event

        future = self._scan_executor.submit(self.market_screener.scan_universe, **call_kwargs)
        self._active_scan_future = future

        def _release_scan(_future) -> None:
            if scan_generation != self._scan_generation:
                return
            self._active_scan_future = None
            self._active_scan_cancel_event = None
            self._active_scan_started_at = None
            self._active_scan_label = None
            try:
                self._scan_lock.release()
            except RuntimeError:
                pass

        return future, cancel_event, _release_scan

    def _run_scan_with_timeout(self, label: str, **kwargs):
        future, cancel_event, release_scan = self._submit_scan(label, **kwargs)
        future.add_done_callback(release_scan)
        try:
            return future.result(timeout=self.settings.telegram_command_timeout_seconds)
        except FutureTimeoutError as exc:
            cancel_event.set()
            raise RuntimeError(
                f"Scan timed out after {self.settings.telegram_command_timeout_seconds}s. "
                "Cancellation requested; it will stop at the next symbol/timeframe boundary. "
                "Use /scan_status before starting another scan."
            ) from exc

    def _recover_stale_scan_if_needed(self) -> bool:
        future = self._active_scan_future
        if future is None:
            if self._scan_lock.locked():
                self._scan_generation += 1
                self._clear_active_scan(release_lock=True)
                self.logs.log("telegram_scan_orphan_lock_recovered", {})
                return True
            return False
        if future.done():
            self._scan_generation += 1
            self._clear_active_scan(release_lock=True)
            return True
        started = self._active_scan_started_at
        if started is None:
            return False
        elapsed = int((utc_now() - started).total_seconds())
        stale_after = int(
            getattr(
                self.settings,
                "telegram_scan_stale_after_seconds",
                max(int(getattr(self.settings, "telegram_command_timeout_seconds", 20) or 20) * 3, 60),
            )
            or 180
        )
        if str(self._active_scan_label or "").startswith("manual_deep_scan") and elapsed < max(stale_after, 3600):
            return False
        if elapsed < max(stale_after, 30):
            return False
        if self._active_scan_cancel_event is not None:
            self._active_scan_cancel_event.set()
        try:
            future.cancel()
        except Exception:
            pass
        self._scan_generation += 1
        self._scan_executor.shutdown(wait=False, cancel_futures=True)
        self._scan_executor = ThreadPoolExecutor(max_workers=1)
        self._clear_active_scan(release_lock=True)
        self.logs.log(
            "telegram_scan_stale_recovered",
            {"elapsed_seconds": elapsed, "stale_after_seconds": stale_after},
        )
        return True

    def _clear_active_scan(self, *, release_lock: bool) -> None:
        self._active_scan_future = None
        self._active_scan_cancel_event = None
        self._active_scan_started_at = None
        self._active_scan_label = None
        if release_lock and self._scan_lock.locked():
            try:
                self._scan_lock.release()
            except RuntimeError:
                pass

    def _scan_status_message(self) -> str:
        recovered = self._recover_stale_scan_if_needed()
        if recovered:
            return "Recovered a stale screener scan. No screener scan is currently running."
        future = self._active_scan_future
        if future is None or future.done():
            return "No screener scan is currently running."
        started = self._active_scan_started_at
        elapsed = int((utc_now() - started).total_seconds()) if started else 0
        cancelling = bool(self._active_scan_cancel_event and self._active_scan_cancel_event.is_set())
        state = "cancelling" if cancelling else "running"
        return (
            f"Screener scan is {state}.\n"
            f"Task: {self._active_scan_label or 'scan'}\n"
            f"Elapsed: {elapsed}s\n"
            "Use /cancel_scan to request stop, or wait for the current scan to finish."
        )

    def _scan_in_progress_message(self) -> str:
        if self._recover_stale_scan_if_needed():
            return "Recovered a stale screener scan. Please retry the scan command."
        future = self._active_scan_future
        if future is None or future.done():
            return "A screener scan is already running. Use /scan_status before starting another scan."
        started = self._active_scan_started_at
        elapsed = int((utc_now() - started).total_seconds()) if started else 0
        label = self._active_scan_label or "scan"
        return (
            "A screener scan is already running.\n"
            f"Task: {label}\n"
            f"Elapsed: {elapsed}s\n"
            "Use /scan_status before starting another scan, or /cancel_scan to request stop."
        )

    def _cancel_scan_message(self) -> str:
        if self._recover_stale_scan_if_needed():
            return "Recovered a stale screener scan. No screener scan is currently running."
        future = self._active_scan_future
        if future is None or future.done() or self._active_scan_cancel_event is None:
            return "No screener scan is currently running."
        self._active_scan_cancel_event.set()
        return (
            "Cancellation requested for the active screener scan. "
            "It will stop at the next symbol/timeframe boundary."
        )

    def _is_due(self, last_sent_raw: str) -> bool:
        try:
            last_sent = datetime.fromisoformat(last_sent_raw)
        except ValueError:
            return True
        return utc_now() - last_sent >= timedelta(minutes=self.settings.telegram_alert_interval_minutes)

    @staticmethod
    def _parse_limit(args: list[str]) -> int:
        if not args:
            return 5
        try:
            return max(1, min(int(args[0]), 20))
        except ValueError:
            return 5

    @staticmethod
    def _parse_scan_args(args: list[str]) -> tuple[int, list[str] | None, bool, int | None, list[str] | None]:
        detail_tokens = {"detail", "details", "verbose"}
        include_details = False
        universe_limit: int | None = None
        requested_timeframes: list[str] = []
        filtered_args: list[str] = []
        for arg in args:
            raw = str(arg or "").strip()
            lowered = raw.lower()
            if not raw:
                continue
            if lowered in detail_tokens:
                include_details = True
                continue
            if lowered in {"all", "full", "top100", "universe", "universe=top100"}:
                universe_limit = 100
                continue
            top_match = re.fullmatch(r"top(\d{1,3})", lowered)
            if top_match:
                universe_limit = max(1, min(int(top_match.group(1)), 100))
                continue
            limit_match = re.fullmatch(r"(?:universe|u)=(\d{1,3})", lowered)
            if limit_match:
                universe_limit = max(1, min(int(limit_match.group(1)), 100))
                continue
            if lowered.startswith(("tf=", "timeframe=", "timeframes=")):
                _, value = raw.split("=", 1)
                for token in re.split(r"[,\s]+", value):
                    timeframe = TelegramBotService._normalize_scan_timeframe(token)
                    if timeframe and timeframe not in requested_timeframes:
                        requested_timeframes.append(timeframe)
                continue
            filtered_args.append(raw)
        args = filtered_args
        if not args:
            return 5, None, include_details, universe_limit, requested_timeframes or None
        limit = 5
        symbol_tokens = list(args)
        try:
            limit = max(1, min(int(args[0]), 20))
            symbol_tokens = args[1:]
        except ValueError:
            symbol_tokens = args

        symbols: list[str] = []
        seen: set[str] = set()
        for token in symbol_tokens:
            for raw_symbol in re.split(r"[,\s]+", str(token or "")):
                timeframe = TelegramBotService._normalize_scan_timeframe(raw_symbol)
                if timeframe:
                    if timeframe not in requested_timeframes:
                        requested_timeframes.append(timeframe)
                    continue
                cleaned = raw_symbol.strip().upper().lstrip("$")
                if not cleaned or cleaned in seen:
                    continue
                if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", cleaned):
                    continue
                symbols.append(cleaned)
                seen.add(cleaned)
        return limit, symbols or None, include_details, universe_limit, requested_timeframes or None

    @staticmethod
    def _normalize_scan_timeframe(value: str) -> str | None:
        normalized = str(value or "").strip().lower()
        mapping = {
            "1": "1m",
            "1m": "1m",
            "1min": "1m",
            "1minute": "1m",
            "5": "5m",
            "5m": "5m",
            "5min": "5m",
            "5minute": "5m",
            "10": "10m",
            "10m": "10m",
            "10min": "10m",
            "10minute": "10m",
            "15": "15m",
            "15m": "15m",
            "15min": "15m",
            "15minute": "15m",
            "60m": "1h",
            "1h": "1h",
            "hour": "1h",
            "1d": "1d",
            "day": "1d",
            "daily": "1d",
            "1w": "1w",
            "1wk": "1w",
            "week": "1w",
            "weekly": "1w",
        }
        return mapping.get(normalized)

    @staticmethod
    def _parse_amount(args: list[str], *, default: float) -> float | None:
        if not args:
            return default
        try:
            amount = float(args[0])
        except ValueError:
            return None
        return amount if amount > 0 else None

    @staticmethod
    def _parse_optional_limit(args: list[str], *, default: int) -> int:
        if not args:
            return max(1, default)
        try:
            return max(1, min(int(args[0]), 100))
        except ValueError:
            return max(1, default)

    @staticmethod
    def _parse_symbol_arg(args: list[str]) -> str | None:
        if len(args) != 1:
            return None
        symbol = args[0].strip().upper()
        if symbol in {"SCAN", "SCREENER", "INTRADAY", "INTRADAY_SCAN", "SUPPORTED_SCAN", "VALIDATED_SCAN"}:
            return None
        if not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", symbol):
            return None
        return symbol
