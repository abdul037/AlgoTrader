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

    def __init__(self, *, settings: Any, runtime_state: Any, run_logs: Any):
        self.settings = settings
        self.state = runtime_state
        self.logs = run_logs

    def status(self) -> AutomationStatus:
        return AutomationStatus(
            paused=self.is_paused(),
            kill_switch_enabled=self.kill_switch_enabled(),
            auto_propose_enabled=bool(getattr(self.settings, "auto_propose_enabled", False)),
            auto_execute_after_approval=bool(getattr(self.settings, "auto_execute_after_approval", False)),
            execution_mode=str(getattr(self.settings, "execution_mode", "paper")),
            require_approval=bool(getattr(self.settings, "require_approval", True)),
            enable_real_trading=bool(getattr(self.settings, "enable_real_trading", False)),
            reason=self.state.get(self.REASON_KEY) or "",
            updated_at=self.state.get(self.UPDATED_AT_KEY),
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

    def enable_kill_switch(self, *, reason: str = "") -> AutomationStatus:
        return self._set_state(paused=True, kill_switch=True, reason=reason or "kill switch enabled")

    def scan_blockers(self) -> list[str]:
        blockers: list[str] = []
        if self.kill_switch_enabled():
            blockers.append("automation_kill_switch_enabled")
        if self.is_paused():
            blockers.append("automation_paused")
        return blockers

    def execution_blockers(self) -> list[str]:
        blockers = self.scan_blockers()
        if getattr(self.settings, "execution_mode", "paper") == "live":
            if not bool(getattr(self.settings, "require_approval", True)):
                blockers.append("approval_required_must_remain_enabled")
            if not bool(getattr(self.settings, "enable_real_trading", False)):
                blockers.append("enable_real_trading_false")
            if bool(getattr(self.settings, "paper_trading_enabled", True)):
                blockers.append("paper_trading_enabled_in_live_mode")
        return blockers

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

    @staticmethod
    def _to_bool(value: Any) -> bool:
        if isinstance(value, bool):
            return value
        return str(value or "").strip().lower() in {"1", "true", "yes", "on"}
