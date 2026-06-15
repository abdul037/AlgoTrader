"""Runtime automation controls backed by runtime_state."""

from __future__ import annotations

from typing import Any

from app.models.automation import AutomationStatus
from app.utils.time import utc_now


class AutomationService:
    """Control scheduled scans, automatic proposals, and execution kill switch."""

    PAUSED_KEY = "automation:paused"
    KILL_SWITCH_KEY = "automation:kill_switch"
    REASON_KEY = "automation:reason"
    UPDATED_AT_KEY = "automation:updated_at"
    CIRCUIT_REASON_KEY = "automation:circuit_breaker_reason"
    ACCOUNT_VERIFIED_KEY = "automation:account_verified"

    def __init__(self, *, settings: Any, runtime_state: Any, run_logs: Any, broker_router: Any | None = None):
        self.settings = settings
        self.state = runtime_state
        self.logs = run_logs
        self.broker_router = broker_router

    def status(self) -> AutomationStatus:
        return AutomationStatus(
            paused=self.is_paused(),
            kill_switch_enabled=self.kill_switch_enabled(),
            auto_propose_enabled=bool(getattr(self.settings, "auto_propose_enabled", False)),
            auto_execute_after_approval=bool(getattr(self.settings, "auto_execute_after_approval", False)),
            paper_auto_approve_proposals=bool(
                getattr(self.settings, "paper_auto_approve_proposals", False)
            ),
            auto_execution_worker_enabled=bool(
                getattr(self.settings, "auto_execution_worker_enabled", False)
            ),
            execution_mode=str(getattr(self.settings, "execution_mode", "paper")),
            require_approval=bool(getattr(self.settings, "require_approval", True)),
            enable_real_trading=bool(getattr(self.settings, "enable_real_trading", False)),
            reason=self.state.get(self.REASON_KEY) or "",
            updated_at=self.state.get(self.UPDATED_AT_KEY),
            circuit_breaker_reason=self.state.get(self.CIRCUIT_REASON_KEY) or "",
            account_verified=self._optional_bool(self.state.get(self.ACCOUNT_VERIFIED_KEY)),
        )

    def is_paused(self) -> bool:
        raw = self.state.get(self.PAUSED_KEY)
        if raw is None:
            return bool(getattr(self.settings, "automation_paused_default", False))
        return self._to_bool(raw)

    def kill_switch_enabled(self) -> bool:
        return bool(getattr(self.settings, "kill_switch_enabled", False)) or self._to_bool(
            self.state.get(self.KILL_SWITCH_KEY)
        )

    def pause(self, *, reason: str = "") -> AutomationStatus:
        return self._set_state(paused=True, kill_switch=None, reason=reason or "paused manually")

    def resume(self, *, reason: str = "") -> AutomationStatus:
        return self._set_state(paused=False, kill_switch=False, reason=reason or "resumed manually")

    def enable_kill_switch(self, *, reason: str = "", emergency_stop: bool = True) -> AutomationStatus:
        effective_reason = reason or "kill switch enabled"
        status = self._set_state(paused=True, kill_switch=True, reason=effective_reason)
        if emergency_stop:
            self._emergency_stop(reason=effective_reason)
        return status

    def scan_blockers(self) -> list[str]:
        blockers: list[str] = []
        if self.kill_switch_enabled():
            blockers.append("automation_kill_switch_enabled")
        if self.is_paused():
            blockers.append("automation_paused")
        return blockers

    def execution_blockers(self) -> list[str]:
        blockers = self.scan_blockers()
        circuit_reason = self.state.get(self.CIRCUIT_REASON_KEY)
        if circuit_reason:
            blockers.append("circuit_breaker:" + circuit_reason)
        expected_account = getattr(
            self.settings,
            "alpaca_effective_expected_account_number",
            getattr(self.settings, "alpaca_expected_account_number", ""),
        )
        if expected_account and not self._to_bool(self.state.get(self.ACCOUNT_VERIFIED_KEY)):
            blockers.append("alpaca_account_not_verified")
        if getattr(self.settings, "execution_mode", "paper") == "live":
            if not bool(getattr(self.settings, "require_approval", True)):
                blockers.append("approval_required_must_remain_enabled")
            if not bool(getattr(self.settings, "enable_real_trading", False)):
                blockers.append("enable_real_trading_false")
            if bool(getattr(self.settings, "paper_trading_enabled", True)):
                blockers.append("paper_trading_enabled_in_live_mode")
        return blockers

    def set_account_verified(self, verified: bool) -> None:
        self.state.set(self.ACCOUNT_VERIFIED_KEY, "true" if verified else "false")

    def trip_circuit_breaker(self, *, reason: str, emergency_stop: bool = True) -> AutomationStatus:
        self.state.set(self.CIRCUIT_REASON_KEY, reason)
        return self.enable_kill_switch(
            reason=f"circuit breaker: {reason}",
            emergency_stop=emergency_stop,
        )

    def clear_circuit_breaker(self) -> AutomationStatus:
        self.state.set(self.CIRCUIT_REASON_KEY, "")
        return self.status()

    def _set_state(self, *, paused: bool, kill_switch: bool | None, reason: str) -> AutomationStatus:
        now = utc_now().isoformat()
        self.state.set(self.PAUSED_KEY, "true" if paused else "false")
        if kill_switch is not None:
            self.state.set(self.KILL_SWITCH_KEY, "true" if kill_switch else "false")
        self.state.set(self.REASON_KEY, reason)
        self.state.set(self.UPDATED_AT_KEY, now)
        self.logs.log(
            "automation_state_changed",
            {"paused": paused, "kill_switch": kill_switch, "reason": reason, "updated_at": now},
        )
        return self.status()

    def _emergency_stop(self, *, reason: str) -> None:
        if self.broker_router is None:
            return
        close_allowed = (
            str(getattr(self.settings, "execution_mode", "paper")) != "live"
            or bool(getattr(self.settings, "kill_switch_auto_close_positions", False))
        )
        results: list[dict[str, Any]] = []
        total_cancelled = 0
        total_closed = 0
        for client in self.broker_router.all_clients():
            item: dict[str, Any] = {
                "client": client.__class__.__name__,
                "orders_cancelled": 0,
                "positions_closed": 0,
                "errors": [],
            }
            if hasattr(client, "cancel_all_orders"):
                try:
                    cancelled = int(client.cancel_all_orders() or 0)
                    item["orders_cancelled"] = cancelled
                    total_cancelled += cancelled
                except Exception as exc:
                    item["errors"].append(f"cancel_all_orders:{exc}")
            if close_allowed and hasattr(client, "close_all_positions"):
                try:
                    closed = int(client.close_all_positions() or 0)
                    item["positions_closed"] = closed
                    total_closed += closed
                except Exception as exc:
                    item["errors"].append(f"close_all_positions:{exc}")
            elif not close_allowed:
                item["close_all_positions_skipped"] = "live_mode_requires_kill_switch_auto_close_positions"
            results.append(item)
        self.logs.log(
            "kill_switch_emergency_stop",
            {
                "reason": reason,
                "orders_cancelled": total_cancelled,
                "positions_closed": total_closed,
                "close_positions_allowed": close_allowed,
                "clients": results,
            },
        )

    @staticmethod
    def _to_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        return str(value or "").strip().lower() in {"1", "true", "yes", "on"}

    @classmethod
    def _optional_bool(cls, value: Any) -> bool | None:
        if value is None:
            return None
        return cls._to_bool(value)
