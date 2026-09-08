"""A non-secret snapshot of the effective auto-execution policy.

Logged once at startup as the ``execution_policy_effective`` run-log event so the
deployed execution policy is always observable from the run_logs (e.g. Supabase)
*without* the app URL or raw environment access — both of which can be
unreachable (an egress-blocked domain, a redacting OAuth connection, a control
token nobody can read back). Whether the first autonomous paper trade can fire
comes down to these flags and thresholds, so recording them at boot turns "why
didn't it trade?" into a single query.

Hard rule: this snapshot contains ONLY policy flags/thresholds — never a
credential, key, token, secret, URL, or account number. ``test_policy_snapshot``
enforces that no field name or value looks secret-bearing.
"""

from __future__ import annotations

from typing import Any


def effective_execution_policy(settings: Any) -> dict[str, Any]:
    """Return the non-secret auto-execution policy as a flat, loggable dict."""

    def _get(name: str, default: Any) -> Any:
        return getattr(settings, name, default)

    return {
        # Hard paper-only guardrails.
        "execution_mode": str(_get("execution_mode", "")),
        "enable_real_trading": bool(_get("enable_real_trading", False)),
        # The auto-execution enablement chain: every one of these must be set the
        # right way for an unattended paper trade to fire.
        "paper_auto_operation_mode": str(_get("paper_auto_operation_mode", "shadow")),
        "paper_auto_approve_proposals": bool(_get("paper_auto_approve_proposals", False)),
        "auto_execution_worker_enabled": bool(_get("auto_execution_worker_enabled", False)),
        "auto_propose_enabled": bool(_get("auto_propose_enabled", False)),
        "auto_execute_after_approval": bool(_get("auto_execute_after_approval", False)),
        # Approval-tier policy + the clean-lifecycle bootstrap.
        "paper_auto_approval_tier": str(_get("paper_auto_approval_tier", "")),
        "paper_auto_min_clean_supervised_lifecycles": int(
            _get("paper_auto_min_clean_supervised_lifecycles", 0) or 0
        ),
        # Exploration / near-miss auto-exec path.
        "paper_scanner_exploration_enabled": bool(_get("paper_scanner_exploration_enabled", False)),
        "paper_scanner_bypass_production_approval": bool(
            _get("paper_scanner_bypass_production_approval", False)
        ),
        "paper_near_miss_promotion_enabled": bool(_get("paper_near_miss_promotion_enabled", False)),
        "paper_unattended_near_miss_auto_exec_enabled": bool(
            _get("paper_unattended_near_miss_auto_exec_enabled", False)
        ),
        # Score floors.
        "auto_execution_min_score": float(_get("auto_execution_min_score", 0.0) or 0.0),
        "paper_exploration_auto_execution_min_score": float(
            _get("paper_exploration_auto_execution_min_score", 0.0) or 0.0
        ),
        # Scan/backtest health knobs (the two 240s-timeout fixes live here).
        "backtest_scheduler_enabled": bool(_get("backtest_scheduler_enabled", False)),
        "backtest_scheduler_deadline_seconds": float(
            _get("backtest_scheduler_deadline_seconds", 0.0) or 0.0
        ),
        "screener_batch_deadline_seconds": float(_get("screener_batch_deadline_seconds", 0.0) or 0.0),
        "market_universe_limit": int(_get("market_universe_limit", 0) or 0),
        "alpaca_data_feed": str(_get("alpaca_data_feed", "")),
        # Position sizing caps (non-secret; the broker step sizes as
        # floor(min(default_trade_amount, max_trade_amount) / price), so both
        # decide whether a >$500/share name can trade at all).
        "default_trade_amount_usd": float(_get("default_trade_amount_usd", 0.0) or 0.0),
        "max_trade_amount_usd": float(_get("max_trade_amount_usd", 0.0) or 0.0),
        "max_open_positions": int(_get("max_open_positions", 0) or 0),
        "max_trades_per_day": int(_get("max_trades_per_day", 0) or 0),
        # Risk profile (operator-approved aggressive paper profile, 2026-09-08).
        "max_risk_per_trade_pct": float(_get("max_risk_per_trade_pct", 0.0) or 0.0),
        "auto_propose_risk_based_sizing": bool(_get("auto_propose_risk_based_sizing", False)),
        "max_daily_loss_usd": float(_get("max_daily_loss_usd", 0.0) or 0.0),
        "max_weekly_loss_usd": float(_get("max_weekly_loss_usd", 0.0) or 0.0),
        "max_consecutive_losses_before_cooldown": int(
            _get("max_consecutive_losses_before_cooldown", 0) or 0
        ),
        "drawdown_governor_enabled": bool(_get("drawdown_governor_enabled", False)),
        "drawdown_governor_soft_pct": float(_get("drawdown_governor_soft_pct", 0.0) or 0.0),
        "drawdown_governor_hard_pct": float(_get("drawdown_governor_hard_pct", 0.0) or 0.0),
        "drawdown_governor_floor": float(_get("drawdown_governor_floor", 0.0) or 0.0),
        # Tradeable-instrument allowlist vs the scanned universe. On 2026-09-08 the
        # two disagreed and 17 of 19 promoted candidates died at the proposal step
        # ("not in the allowed instrument list") — invisible until someone read the
        # run_logs by hand. Counts + an agreement flag only; the lists themselves
        # are not secret but are long, and the flag is what matters.
        "allowed_instruments_count": len(list(_get("allowed_instruments", []) or [])),
        "market_universe_symbols_count": len(list(_get("market_universe_symbols", []) or [])),
        "universe_not_in_allowlist": sorted(
            {str(s).upper() for s in (_get("market_universe_symbols", []) or [])}
            - {str(s).upper() for s in (_get("allowed_instruments", []) or [])}
        )[:50],
        # A boolean only — never the account number itself.
        "alpaca_expected_account_configured": bool(
            str(_get("alpaca_expected_account_number", "") or "").strip()
        ),
    }
