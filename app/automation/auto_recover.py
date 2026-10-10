"""Paper-only self-healing after a circuit-breaker trip.

2026-09-10: a false ``missing_bracket_protection`` tripped the breaker
pre-market; the kill switch is persisted runtime state, nothing clears it, and
the unattended bot sat paused for three sessions logging
``workflow_scheduler_paused`` once a minute.

While the breaker is tripped, :func:`try_auto_recover` re-runs reconciliation at
most every ``paper_auto_recover_probe_interval_seconds`` and, once it comes back
clean, clears the breaker and resumes automation (capped by
``paper_auto_recover_max_resumes_per_day``). It never touches an operator
pause, a manual kill switch, ``KILL_SWITCH_ENABLED``, an account mismatch, a
broker trading block, or any real-trading configuration.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.utils.time import utc_now

LAST_PROBE_KEY = "automation:auto_recover:last_probe_at"
RESUME_COUNT_KEY_PREFIX = "automation:auto_recover:resumes:"
_NEVER_RESUME_MARKERS = ("account_mismatch", "alpaca_trading_blocked")


def try_auto_recover(host: Any, blockers: list[str]) -> bool:
    """Return True when the breaker was cleared and automation resumed.

    ``host`` is the scheduler (``SignalWorkflowService``) and provides
    ``settings``, ``automation``, ``reconciliation``, ``runtime_state``,
    ``run_logs`` and ``notifier``.
    """

    settings = host.settings
    automation = host.automation
    reconciliation = getattr(host, "reconciliation", None)
    runtime_state = host.runtime_state
    run_logs = host.run_logs
    notifier = getattr(host, "notifier", None)
    if not bool(getattr(settings, "paper_auto_recover_circuit_breaker", False)):
        return False
    if str(getattr(settings, "execution_mode", "paper")) != "paper":
        return False
    if bool(getattr(settings, "enable_real_trading", False)):
        return False
    if bool(getattr(settings, "kill_switch_enabled", False)):
        return False
    if reconciliation is None or not hasattr(automation, "status"):
        return False
    status = automation.status()
    reason = str(getattr(status, "reason", "") or "")
    breaker_reason = str(getattr(status, "circuit_breaker_reason", "") or "")
    if not breaker_reason or not reason.startswith("circuit breaker:"):
        return False  # operator pause / manual kill switch: never overridden
    if any(marker in breaker_reason for marker in _NEVER_RESUME_MARKERS):
        return False

    now = utc_now()
    interval = max(int(getattr(settings, "paper_auto_recover_probe_interval_seconds", 600) or 0), 0)
    last_probe = str(runtime_state.get(LAST_PROBE_KEY) or "")
    if last_probe:
        try:
            elapsed = (now - datetime.fromisoformat(last_probe)).total_seconds()
        except (TypeError, ValueError):
            elapsed = float("inf")
        if elapsed < interval:
            return False
    count_key = RESUME_COUNT_KEY_PREFIX + now.strftime("%Y-%m-%d")
    resumes_today = int(runtime_state.get(count_key) or 0)
    max_resumes = max(int(getattr(settings, "paper_auto_recover_max_resumes_per_day", 3) or 0), 0)
    if resumes_today >= max_resumes:
        return False

    runtime_state.set(LAST_PROBE_KEY, now.isoformat())
    try:
        probe = reconciliation.reconcile()
    except Exception as exc:  # noqa: BLE001 - a probe failure just leaves automation paused
        run_logs.log(
            "automation_auto_recover_probe",
            {"outcome": "error", "error": str(exc), "breaker_reason": breaker_reason},
        )
        return False
    issues = list((probe or {}).get("issues") or [])
    if str((probe or {}).get("status")) != "ok" or issues:
        run_logs.log(
            "automation_auto_recover_probe",
            {"outcome": "still_blocked", "issues": issues, "breaker_reason": breaker_reason},
        )
        return False

    automation.clear_circuit_breaker()
    automation.resume(reason=f"auto-recovered: reconciliation clean after {breaker_reason}")
    runtime_state.set(count_key, str(resumes_today + 1))
    payload = {
        "breaker_reason": breaker_reason,
        "blockers_cleared": list(blockers),
        "resumes_today": resumes_today + 1,
        "max_resumes_per_day": max_resumes,
    }
    run_logs.log("automation_auto_resumed", payload)
    if notifier is not None and hasattr(notifier, "send_text"):
        try:
            notifier.send_text(
                "Paper automation auto-resumed\n"
                f"Circuit breaker cleared: {breaker_reason}\n"
                f"Reconciliation clean; resume {resumes_today + 1}/{max_resumes} today."
            )
        except Exception:  # noqa: BLE001 - notification failure must not undo the resume
            run_logs.log("automation_auto_resume_notify_failed", payload)
    return True
