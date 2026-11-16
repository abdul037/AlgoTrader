"""Paper short orders at execution time (shorts step 3, operator 2026-11-16).

The execution coordinator's Alpaca checks for a SELL entry. A SELL that is not a tagged paper
short the switch admits (``app.risk.short_side.paper_short_order``) is refused exactly as before
(``long_entries_only``). A paper short:

* bracket rules at the fresh quote: the stop ABOVE it (``stop_loss_not_above_entry``), the target
  BELOW it (``take_profit_not_below_entry``), and reward:risk = (quote - target) / (stop - quote)
  at least the execution minimum (``reward_to_risk_below_min_at_quote``);
* fresh borrow and margin facts: Alpaca's asset and account are read again just before the risk
  gate (``refresh_short_facts``), so ``ShortTradePolicy`` judges what Alpaca says now, not at
  proposal time; a failed read writes facts that the policy refuses (fail closed);
* the order is a sell-to-open bracket (``bracket_kwargs``: ``opens_short=True`` for the client).
"""

from __future__ import annotations

from contextlib import suppress
from typing import Any

from app.risk.short_side import paper_short_order, short_asset_facts, side_value


def is_sell(order: Any) -> bool:
    return side_value(getattr(order, "side", "")) == "sell"


def short_bracket_reasons(
    settings: Any, proposal: Any, quote_price: float, min_reward_to_risk: float = 0.0
) -> list[str]:
    """The bracket checks for a SELL entry (see the module docstring)."""

    from app.broker import crypto as crypto_symbols

    order = proposal.order
    if settings is None or not paper_short_order(settings, order):
        return ["long_entries_only"]
    asset_class = str(getattr(order.asset_class, "value", order.asset_class) or "").lower()
    if crypto_symbols.is_crypto_symbol(order.symbol) or asset_class == "crypto":
        return ["short_equities_only"]  # Alpaca's crypto path only buys: never a crypto short
    reasons: list[str] = []
    stop, target, quote = order.stop_loss, order.take_profit, float(quote_price)
    if stop is None:
        reasons.append("stop_loss_missing")
    if target is None:
        reasons.append("take_profit_missing")
    if stop is not None and float(stop) <= quote:
        reasons.append("stop_loss_not_above_entry")
    if target is not None and float(target) >= quote:
        reasons.append("take_profit_not_below_entry")
    if (
        not reasons
        and min_reward_to_risk > 0
        and (quote - float(target)) / (float(stop) - quote) < min_reward_to_risk
    ):
        reasons.append("reward_to_risk_below_min_at_quote")
    return reasons


def refresh_short_facts(settings: Any, broker: Any, order: Any, logs: Any = None) -> None:
    """Re-read a paper short's borrow and margin facts from Alpaca into its order metadata (in
    place, before the risk gate). Any other order is untouched. Never raises."""

    if not paper_short_order(settings, order):
        return
    try:
        facts = short_asset_facts(broker, order.symbol)
    except Exception as exc:  # noqa: BLE001 - unknown facts are refused by the policy
        facts = {
            "shortable": False,
            "easy_to_borrow": False,
            "borrow_cost_annual_pct": None,
            "margin_requirement_pct": None,
            "short_facts_error": str(exc)[:200],
        }
    order.metadata.update(facts)
    if logs is not None:
        with suppress(Exception):
            logs.log(
                "paper_short_facts_refreshed",
                {
                    "symbol": str(order.symbol).upper(),
                    **{
                        key: facts.get(key)
                        for key in (
                            "shortable",
                            "easy_to_borrow",
                            "marginable",
                            "margin_requirement_pct",
                            "borrow_cost_annual_pct",
                            "account_shorting_enabled",
                            "short_facts_error",
                        )
                        if key in facts
                    },
                },
            )


def bracket_kwargs(settings: Any, order: Any) -> dict[str, Any]:
    """Extra ``submit_bracket_order`` arguments: ``opens_short`` for a paper short, else none (a
    broker double without the argument keeps working for longs)."""

    return {"opens_short": True} if paper_short_order(settings, order) else {}
