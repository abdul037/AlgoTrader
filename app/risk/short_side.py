"""Paper short entries (shorts step 3, operator 2026-11-16): the switch and the side helpers.

Phase A: a bracketed ``entry_short`` candidate the scans already produce may open a SHORT on the
Alpaca paper account (practice money), behind ``paper_short_entries_enabled`` (default off), in
paper mode with real trading off, with Alpaca as the paper broker. Every short is tagged
paper-only, and the eToro live mirror refuses any order that opens a short
(``etoro_live_short_not_enabled``) until Phase B (eToro short CFDs) ships. Design:
docs/notes/paper_shorts.md; the admission lives in app/workflow/paper_shorts.py.

This module is a leaf (no app import at module level): the risk gate, the broker adapter, the
ledger and the reconciliation all read these helpers.
"""

from __future__ import annotations

from typing import Any

SHORT_ROLE = "entry_short"  # the scan's signal_role of a bracketed short setup
SHORT_REASON = "short"  # paper_only_reason of a short its own short verdict admits (path a)
OPENS_SHORT_FIELD = "opens_short"  # order metadata: this SELL opens a short (ShortTradePolicy)
POSITION_SIDE_FIELD = "position_side"  # order / ledger metadata: "short" for every paper short
SHORT_SIDE = "short"
BORROW_COST_SOURCE = "cost_model_etb_assumption"


def paper_shorts_enabled(settings: Any) -> bool:
    """Settings alone: whether paper short entries can be admitted on this account."""

    return (
        bool(getattr(settings, "paper_short_entries_enabled", False))  # absent: off
        and str(getattr(settings, "execution_mode", "")) == "paper"
        and not bool(getattr(settings, "enable_real_trading", False))
        and str(getattr(settings, "paper_broker", "") or "") == "alpaca"
        and str(getattr(settings, "broker_for_equities", "") or "") == "alpaca"
    )


def is_short_candidate(candidate: Any) -> bool:
    """A scan candidate whose setup is a short (``signal_role`` "entry_short")."""

    return str(getattr(candidate, "signal_role", "") or "").lower() == SHORT_ROLE


def side_value(side: Any) -> str:
    return str(getattr(side, "value", side) or "").strip().lower()


def metadata_is_short(metadata: Any) -> bool:
    """Order, request or ledger metadata that marks a short (either marker)."""

    return isinstance(metadata, dict) and (
        bool(metadata.get(OPENS_SHORT_FIELD))
        or str(metadata.get(POSITION_SIDE_FIELD) or "").strip().lower() == SHORT_SIDE
    )


def order_opens_short(order: Any) -> bool:
    """True for an entry order that opens a short: a SELL (every order the risk gate and the
    eToro mirror see is an entry, so a SELL is never an exit there) or one marked short."""

    return side_value(getattr(order, "side", "")) == "sell" or metadata_is_short(
        getattr(order, "metadata", None)
    )


def paper_short_order(settings: Any, order: Any) -> bool:
    """A SELL entry the paper switch admits: the switch is effective, the order is tagged
    paper-only and marked ``opens_short``. What the risk gate reads as "shorting switched on"
    for this one order; every other short control still applies."""

    from app.broker import crypto as crypto_symbols
    from app.performance.paper_only import is_paper_only

    metadata = getattr(order, "metadata", None)
    return (
        paper_shorts_enabled(settings)
        and side_value(getattr(order, "side", "")) == "sell"
        and is_paper_only(metadata)
        and isinstance(metadata, dict)
        and bool(metadata.get(OPENS_SHORT_FIELD))
        and routes_to_alpaca_equity(settings, order)
        and not crypto_symbols.is_crypto_symbol(str(getattr(order, "symbol", "") or ""))
    )


def routes_to_alpaca_equity(settings: Any, order: Any) -> bool:
    """The order is an equity the broker router sends to Alpaca: asset class ``equity`` (an
    ``unknown`` one routes as a non-equity, to eToro by default) and Alpaca the equities broker."""

    asset_class = side_value(getattr(order, "asset_class", ""))  # the enum's value, lower-cased
    return asset_class == "equity" and str(getattr(settings, "broker_for_equities", "") or "") == "alpaca"


def closing_side(entry_side: Any) -> str:
    """The order side that closes a position opened by ``entry_side`` (buy covers a short)."""

    return "buy" if side_value(entry_side) == "sell" else "sell"


def signed_pnl(entry_side: Any, entry_price: float, exit_price: float, quantity: float) -> float:
    """Realized P/L of a round trip: a long gains when the exit is higher, a short when lower."""

    move = float(exit_price) - float(entry_price)
    return (-move if side_value(entry_side) == "sell" else move) * float(quantity)


def etb_borrow_cost_annual_pct() -> float:
    """The borrow cost given to an easy-to-borrow name, % a year.

    Alpaca's asset API publishes no borrow rate (``get_asset_capabilities`` reports
    ``borrow_cost_available: False``) and Alpaca opens shorts only in easy-to-borrow names. The
    repo's own Alpaca cost model already prices an ETB short at a general-collateral 0.30 %/year
    (``CostModel.alpaca_equities().short_borrow_fee_daily_pct``, accrued /360), the rate the
    short research charges; this returns that rate, so the policy's cap compares a real number,
    not an assumed 0. A hard-to-borrow name gets no rate at all and is refused.
    """

    from app.backtesting.cost_model import CostModel

    return round(float(CostModel.alpaca_equities().short_borrow_fee_daily_pct) * 360.0 * 100.0, 6)


def short_asset_facts(alpaca: Any, symbol: str) -> dict[str, Any]:
    """The short-sale facts of ``symbol`` from Alpaca's asset and account data, as order metadata
    (what ``ShortTradePolicy`` reads). Raises when either read fails: the caller refuses."""

    from app.utils.time import utc_now

    caps = dict(alpaca.get_asset_capabilities(str(symbol).upper()) or {})
    account = dict(alpaca.get_account_identity() or {})
    etb = bool(caps.get("easy_to_borrow"))
    margin = caps.get("maintenance_margin_requirement")
    try:
        margin_pct = float(margin) if margin not in (None, "") else None
    except (TypeError, ValueError):
        margin_pct = None
    return {
        OPENS_SHORT_FIELD: True,
        POSITION_SIDE_FIELD: SHORT_SIDE,
        "tradable": bool(caps.get("tradable")),
        "shortable": bool(caps.get("shortable")),
        "easy_to_borrow": etb,
        "marginable": bool(caps.get("marginable")),
        "margin_requirement_pct": margin_pct if margin_pct is not None and margin_pct > 0 else None,
        "borrow_cost_annual_pct": etb_borrow_cost_annual_pct() if etb else None,
        "borrow_cost_source": BORROW_COST_SOURCE if etb else None,
        "account_shorting_enabled": bool(account.get("shorting_enabled")),
        "short_facts_read_at": utc_now().isoformat(),
    }


def short_fact_blockers(facts: dict[str, Any]) -> list[str]:
    """Why these facts rule a short out before any proposal (the risk gate checks them again)."""

    blockers: list[str] = []
    if not facts.get("tradable"):
        blockers.append("asset_not_tradable")
    if not facts.get("shortable"):
        blockers.append("not_shortable")
    if not facts.get("easy_to_borrow"):
        blockers.append("hard_to_borrow")
    if facts.get("marginable") is False:
        blockers.append("not_marginable")
    if facts.get("margin_requirement_pct") is None:
        blockers.append("margin_requirement_missing")
    if not facts.get("account_shorting_enabled"):
        blockers.append("account_shorting_disabled")
    return blockers
