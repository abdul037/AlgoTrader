"""Performance target readiness endpoints."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hmac import compare_digest
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status

router = APIRouter(prefix="/performance", tags=["performance"])


def _require_control_token(request: Request) -> None:
    expected = str(getattr(request.app.state.settings, "control_api_token", "") or "")
    if not expected:
        return
    supplied = request.headers.get("X-Control-Token", "")
    if not supplied or not compare_digest(supplied, expected):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Control token required")


@router.get("/weekly-target-readiness")
def weekly_target_readiness(request: Request):
    _require_control_token(request)
    settings = request.app.state.settings
    target_usd = float(getattr(settings, "weekly_profit_target_usd", 1000.0) or 1000.0)
    window_weeks = max(int(getattr(settings, "weekly_target_window_weeks", 13) or 13), 1)
    min_trades = max(int(getattr(settings, "weekly_target_min_closed_autonomous_trades", 100) or 100), 1)
    min_sessions = max(int(getattr(settings, "weekly_target_min_clean_sessions", 20) or 20), 1)
    paper_equity = max(float(getattr(settings, "paper_account_balance_usd", 100000.0) or 100000.0), 1.0)
    window_start = datetime.now(timezone.utc) - timedelta(weeks=window_weeks)

    lifecycles = request.app.state.paper_trading_service.lifecycles(limit=1000, autonomous_only=True)
    closed = [item for item in lifecycles if item.flags.entry_filled and item.flags.exit_filled_or_position_flat]
    window_closed = [item for item in closed if _timestamp(item.updated_at or item.created_at) >= window_start]
    clean_window = [item for item in window_closed if _is_clean_lifecycle(item)]
    unclean_window = [item for item in window_closed if not _is_clean_lifecycle(item)]
    pnl_values = [float(item.realized_pnl_usd or 0.0) for item in clean_window]
    stats = _pnl_stats(pnl_values)
    stats["max_drawdown_pct_of_equity"] = round(float(stats["max_drawdown_usd"] or 0.0) / paper_equity * 100.0, 6)
    entry_notional = sum(_entry_notional(item) for item in clean_window)
    r_values = [_r_multiple(item) for item in clean_window]
    r_values = [value for value in r_values if value is not None]
    actual_weekly_avg_pnl = stats["net_pnl_usd"] / window_weeks
    actual_weekly_return_pct = (actual_weekly_avg_pnl / paper_equity) * 100.0
    return_on_deployed_notional_pct = (
        (stats["net_pnl_usd"] / entry_notional) * 100.0
        if entry_notional > 0
        else 0.0
    )
    scenarios = [
        _scenario(
            capital_usd=float(capital),
            target_usd=target_usd,
            actual_weekly_return_pct=actual_weekly_return_pct,
        )
        for capital in list(getattr(settings, "weekly_target_capital_scenarios_usd", []) or [])
        if float(capital) > 0
    ]

    latest_reconciliation = request.app.state.safety_state_repository.latest_reconciliation() or {}
    reconciliation_issues = _json_or_empty(latest_reconciliation.get("issues_json"))
    clean_session_dates = sorted(
        {
            date
            for item in clean_window
            if (date := _date_part(item.updated_at or item.created_at)) is not None
        }
    )
    profit_factor_for_gate = stats["profit_factor"]
    if profit_factor_for_gate is None and stats["gross_profit_usd"] > 0 and stats["gross_loss_usd"] == 0:
        profit_factor_for_gate = 999.0

    blockers: list[str] = []
    if len(clean_window) < min_trades:
        blockers.append("insufficient_clean_autonomous_closed_trades")
    if len(clean_session_dates) < min_sessions:
        blockers.append("insufficient_clean_paper_sessions")
    if stats["expectancy_usd"] <= 0:
        blockers.append("non_positive_expectancy")
    if (profit_factor_for_gate or 0.0) < float(getattr(settings, "production_min_profit_factor", 1.30) or 1.30):
        blockers.append("profit_factor_below_threshold")
    if stats["max_drawdown_pct_of_equity"] > float(getattr(settings, "production_max_portfolio_drawdown_pct", 10.0) or 10.0):
        blockers.append("drawdown_above_threshold")
    if not any(item["target_met"] for item in scenarios):
        blockers.append("weekly_target_not_met")
    if str(latest_reconciliation.get("status") or "") != "ok" or reconciliation_issues:
        blockers.append("reconciliation_not_clean")
    if unclean_window:
        blockers.append("unclean_autonomous_lifecycles_present")

    return {
        "target": {
            "weekly_profit_usd": target_usd,
            "window_weeks": window_weeks,
            "capital_scenarios_usd": [item["capital_usd"] for item in scenarios],
            "minimum_clean_autonomous_closed_trades": min_trades,
            "minimum_clean_sessions": min_sessions,
            "paper_equity_base_usd": paper_equity,
        },
        "ready": not blockers,
        "blockers": sorted(set(blockers)),
        "window": {
            "start_at": window_start.isoformat(),
            "end_at": datetime.now(timezone.utc).isoformat(),
        },
        "evidence": {
            "autonomous_lifecycle_count": len(lifecycles),
            "closed_autonomous_trade_count": len(closed),
            "window_closed_autonomous_trade_count": len(window_closed),
            "clean_window_closed_trade_count": len(clean_window),
            "unclean_window_closed_trade_count": len(unclean_window),
            "clean_session_count": len(clean_session_dates),
            "clean_session_dates": clean_session_dates,
        },
        "actual": {
            "gross_pnl_usd": stats["gross_pnl_usd"],
            "net_pnl_usd": stats["net_pnl_usd"],
            "actual_weekly_avg_pnl_usd": round(actual_weekly_avg_pnl, 4),
            "actual_weekly_return_pct": round(actual_weekly_return_pct, 6),
            "return_on_deployed_notional_pct": round(return_on_deployed_notional_pct, 6),
            "entry_notional_usd": round(entry_notional, 4),
            "profit_factor": stats["profit_factor"],
            "expectancy_usd": stats["expectancy_usd"],
            "average_r_multiple": round(sum(r_values) / len(r_values), 4) if r_values else None,
            "max_drawdown_usd": stats["max_drawdown_usd"],
            "max_drawdown_pct_of_equity": stats["max_drawdown_pct_of_equity"],
        },
        "scenarios": scenarios,
        "reconciliation": {
            "status": latest_reconciliation.get("status") or "never_run",
            "account_number": latest_reconciliation.get("account_number"),
            "issues": reconciliation_issues,
            "created_at": latest_reconciliation.get("created_at"),
        },
    }


def _is_clean_lifecycle(item: Any) -> bool:
    flags = item.flags
    return bool(
        flags.entry_filled
        and flags.bracket_legs_verified
        and flags.exit_filled_or_position_flat
        and flags.reconciled
        and flags.duplicate_order_absent
    )


def _scenario(*, capital_usd: float, target_usd: float, actual_weekly_return_pct: float) -> dict[str, Any]:
    scaled_weekly_profit = capital_usd * (actual_weekly_return_pct / 100.0)
    required_weekly_return_pct = (target_usd / capital_usd) * 100.0
    return {
        "capital_usd": capital_usd,
        "scaled_weekly_profit_usd": round(scaled_weekly_profit, 4),
        "target_weekly_profit_usd": target_usd,
        "required_weekly_return_pct": round(required_weekly_return_pct, 6),
        "required_annualized_return_pct": round(required_weekly_return_pct * 52.0, 4),
        "actual_weekly_return_pct": round(actual_weekly_return_pct, 6),
        "target_met": scaled_weekly_profit >= target_usd,
    }


def _pnl_stats(pnl_values: list[float]) -> dict[str, float | None]:
    if not pnl_values:
        return {
            "gross_pnl_usd": 0.0,
            "net_pnl_usd": 0.0,
            "gross_profit_usd": 0.0,
            "gross_loss_usd": 0.0,
            "profit_factor": None,
            "expectancy_usd": 0.0,
            "max_drawdown_usd": 0.0,
            "max_drawdown_pct_of_equity": 0.0,
        }
    gross_profit = sum(value for value in pnl_values if value > 0.0)
    gross_loss = abs(sum(value for value in pnl_values if value < 0.0))
    total = sum(pnl_values)
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for value in pnl_values:
        cumulative += value
        peak = max(peak, cumulative)
        max_drawdown = max(max_drawdown, peak - cumulative)
    return {
        "gross_pnl_usd": round(total, 4),
        "net_pnl_usd": round(total, 4),
        "gross_profit_usd": round(gross_profit, 4),
        "gross_loss_usd": round(gross_loss, 4),
        "profit_factor": None if gross_loss == 0 else round(gross_profit / gross_loss, 4),
        "expectancy_usd": round(total / len(pnl_values), 4),
        "max_drawdown_usd": round(max_drawdown, 4),
        "max_drawdown_pct_of_equity": 0.0,
    }


def _entry_notional(item: Any) -> float:
    entry = float(item.entry_fill_price or 0.0)
    qty = float(item.execution.quantity or item.execution.filled_qty or 0.0)
    return max(entry * qty, 0.0)


def _r_multiple(item: Any) -> float | None:
    entry = float(item.entry_fill_price or 0.0)
    qty = float(item.execution.quantity or item.execution.filled_qty or 0.0)
    stop_prices = [
        float(leg.stop_price)
        for leg in item.execution.legs
        if leg.stop_price is not None and str(leg.side or "").lower() == "sell"
    ]
    if entry <= 0.0 or qty <= 0.0 or not stop_prices:
        return None
    risk_per_share = max(entry - min(stop_prices), 0.0)
    risk_usd = risk_per_share * qty
    if risk_usd <= 0.0:
        return None
    return round(float(item.realized_pnl_usd or 0.0) / risk_usd, 4)


def _timestamp(value: str | None) -> datetime:
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _date_part(timestamp: str | None) -> str | None:
    if not timestamp:
        return None
    return str(timestamp)[:10]


def _json_or_empty(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    try:
        import json

        value = json.loads(str(raw))
    except Exception:
        return []
    return value if isinstance(value, list) else []
