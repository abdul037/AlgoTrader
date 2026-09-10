"""Boot-time end-to-end funnel preflight.

Three weeks of "why didn't it trade?" came down to config mismatches that each
gate hid from the next: a 25-name universe against a 6-name allowlist, then a
6-name instrument catalogue behind that, a $500 per-trade cap that could not
buy one share of a $501 stock, a flat notional that ignored the risk setting.
Every layer had passing unit tests; nothing ever asked, *with the live
configuration, can a candidate for this symbol reach the broker?*

This module asks exactly that, for every symbol in the resolved universe, using
only the pure gate logic and the live settings — no strategy signal, no market
data, no orders. It reports the FIRST blocker per symbol (the same failure the
live funnel would hit) plus the global switches that would stop everything, and
is logged as ``funnel_preflight`` at startup so a misconfiguration shows up in
run_logs before the open instead of after a lost session.

It is diagnostic only: it never changes a gate and never blocks boot.
"""

from __future__ import annotations

import math
from typing import Any

from app.broker.instrument_resolver import InstrumentResolver
from app.risk.proposal_sizing import risk_based_proposal_notional
from app.universe import resolve_universe

# A conservative synthetic setup used purely to exercise the sizing math: a
# 2% stop is typical for the swing/intraday strategies in the library.
_SYNTHETIC_STOP_PCT = 2.0
_SYNTHETIC_TARGET_PCT = 4.0


def run_funnel_preflight(
    settings: Any,
    *,
    automation: Any | None = None,
    alpaca: Any | None = None,
    quote_fn: Any | None = None,
    equity_usd: float | None = None,
) -> dict[str, Any]:
    """Return ``{"global_blockers": [...], "symbols": {SYM: first_blocker|"open"}, ...}``.

    ``quote_fn(symbol) -> price or None`` is optional; without a price the
    sizing stage is evaluated against the per-position cap only.
    """

    global_blockers = _global_blockers(settings, automation)
    universe = resolve_universe(settings)
    resolver = InstrumentResolver(settings)
    equity = float(equity_usd or getattr(settings, "paper_account_balance_usd", 100_000.0) or 100_000.0)

    per_symbol: dict[str, str] = {}
    details: dict[str, dict[str, Any]] = {}
    for symbol in universe:
        blocker, info = _first_symbol_blocker(
            symbol,
            settings=settings,
            resolver=resolver,
            alpaca=alpaca,
            quote_fn=quote_fn,
            equity=equity,
        )
        per_symbol[symbol] = blocker or "open"
        if info:
            details[symbol] = info

    blocked = {s: b for s, b in per_symbol.items() if b != "open"}
    histogram: dict[str, int] = {}
    for blocker in blocked.values():
        histogram[blocker] = histogram.get(blocker, 0) + 1
    return {
        "universe_size": len(universe),
        "open_symbols": len(universe) - len(blocked),
        "blocked_symbols": len(blocked),
        "global_blockers": global_blockers,
        "blocker_histogram": histogram,
        "symbols": per_symbol,
        "details": details,
        "funnel_open": not global_blockers and len(blocked) < len(universe),
    }


def _global_blockers(settings: Any, automation: Any | None) -> list[str]:
    blockers: list[str] = []
    if str(getattr(settings, "execution_mode", "paper")) != "paper":
        blockers.append("execution_mode_not_paper")
    if bool(getattr(settings, "enable_real_trading", False)):
        blockers.append("enable_real_trading_is_true")
    if not bool(getattr(settings, "auto_propose_enabled", False)) and not bool(
        getattr(settings, "paper_auto_approve_proposals", False)
    ):
        blockers.append("auto_propose_disabled")
    if not bool(getattr(settings, "auto_execution_worker_enabled", False)):
        blockers.append("auto_execution_worker_disabled")
    if not bool(getattr(settings, "auto_execute_after_approval", False)):
        blockers.append("auto_execute_after_approval_disabled")
    if str(getattr(settings, "paper_auto_operation_mode", "shadow")) != "unattended":
        blockers.append("paper_auto_operation_mode_not_unattended")
    if not bool(getattr(settings, "paper_auto_approve_proposals", False)):
        blockers.append("paper_auto_approve_proposals_disabled")
    if not bool(getattr(settings, "alpaca_enabled", False)):
        blockers.append("alpaca_disabled")
    if str(getattr(settings, "paper_broker", "")) != "alpaca":
        blockers.append("paper_broker_not_alpaca")
    if bool(getattr(settings, "kill_switch_enabled", False)):
        blockers.append("kill_switch_enabled")
    if int(getattr(settings, "max_open_positions", 0) or 0) <= 0:
        blockers.append("max_open_positions_zero")
    if int(getattr(settings, "max_trades_per_day", 0) or 0) <= 0:
        blockers.append("max_trades_per_day_zero")
    if automation is not None and hasattr(automation, "scan_blockers"):
        try:
            blockers.extend(f"automation:{item}" for item in automation.scan_blockers())
        except Exception as exc:  # noqa: BLE001 - diagnostics must not raise
            blockers.append(f"automation_status_unavailable:{exc}")
    return blockers


def _first_symbol_blocker(
    symbol: str,
    *,
    settings: Any,
    resolver: InstrumentResolver,
    alpaca: Any | None,
    quote_fn: Any | None,
    equity: float,
) -> tuple[str | None, dict[str, Any]]:
    info: dict[str, Any] = {}
    # 1. Allowlist / blocklist / catalogue — the exact check the proposal step runs.
    try:
        resolver.resolve(symbol)
    except ValueError as exc:
        return f"instrument:{exc}", info
    # 2. Broker asset support (best effort; skipped when no client is wired).
    if alpaca is not None and hasattr(alpaca, "is_supported_equity"):
        try:
            if not alpaca.is_supported_equity(symbol):
                return "symbol_not_supported_by_alpaca", info
        except Exception as exc:  # noqa: BLE001 - a broker hiccup is not a config blocker
            info["alpaca_asset_check_error"] = str(exc)
    # 3. Sizing: can the risk-based notional buy at least one whole share?
    price = None
    if quote_fn is not None:
        try:
            price = quote_fn(symbol)
        except Exception as exc:  # noqa: BLE001
            info["quote_error"] = str(exc)
    if price is not None and float(price) > 0:
        price = float(price)
        stop = round(price * (1 - _SYNTHETIC_STOP_PCT / 100.0), 4)
        amount, sizing = risk_based_proposal_notional(
            settings, entry_price=price, stop_price=stop, equity_usd=equity
        )
        cap = min(
            float(getattr(settings, "default_trade_amount_usd", amount) or amount),
            float(getattr(settings, "max_trade_amount_usd", amount) or amount),
        )
        capped_amount = min(float(amount), cap)
        shares = math.floor(capped_amount / price)
        info.update({"price": price, "notional_usd": capped_amount, "shares": shares, "sizing_mode": sizing.get("sizing_mode")})
        if shares < 1:
            return "one_share_exceeds_max_trade_amount", info
    else:
        cap = min(
            float(getattr(settings, "default_trade_amount_usd", 0.0) or 0.0),
            float(getattr(settings, "max_trade_amount_usd", 0.0) or 0.0),
        )
        info["notional_cap_usd"] = cap
        if cap <= 0:
            return "per_trade_notional_cap_zero", info
    # 4. Bracket geometry the broker step enforces (synthetic long setup).
    if bool(getattr(settings, "alpaca_require_bracket_orders", True)) and price:
        stop = price * (1 - _SYNTHETIC_STOP_PCT / 100.0)
        target = price * (1 + _SYNTHETIC_TARGET_PCT / 100.0)
        if not (stop < price < target):
            return "bracket_geometry_invalid", info
    return None, info
