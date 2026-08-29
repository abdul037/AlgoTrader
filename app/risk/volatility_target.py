"""Volatility-target sizing, fractional Kelly, portfolio heat, and a drawdown governor.

These are pure functions with no I/O so they are trivially testable and can be
composed by the sizing and guardrail layers. The existing fixed-fraction sizer
(``position_sizing.calculate_position_size``) answers "how big for a fixed % of
account at risk"; the helpers here answer the portfolio-level questions it does
not:

* **Volatility targeting** — size a position so its expected daily dollar move is
  a chosen fraction of equity, using ATR as the per-share daily-vol proxy. This
  keeps risk comparable across a $30 name and a $600 name.
* **Fractional Kelly** — turn a strategy's realized edge (win rate + win/loss
  ratio) into a capital weight, scaled down (half-Kelly by default) and hard
  capped, because full Kelly is famously too aggressive on estimated edges.
* **Portfolio heat** — the sum of open per-trade risk. A system can pass every
  single-trade risk check and still be betting the account; heat caps that.
* **Drawdown governor** — a proportional de-gross ramp between a soft and hard
  drawdown, instead of the binary pause/kill the portfolio evaluator applies.
"""

from __future__ import annotations

from collections.abc import Iterable


def volatility_target_quantity(
    *,
    equity_usd: float,
    target_daily_vol_pct: float,
    atr: float,
    price: float,
    weight: float = 1.0,
    max_position_pct: float = 100.0,
) -> float:
    """Shares sized so the position's daily dollar volatility hits a target.

    A position of ``q`` shares has an expected daily dollar move of ``q * atr``.
    We solve ``q * atr == equity * weight * target_daily_vol_pct/100`` and then
    cap the notional at ``max_position_pct`` of equity. Returns 0 on degenerate
    inputs (non-positive ATR/price/equity) rather than raising, so a missing ATR
    simply yields no vol-target size and the caller can fall back.
    """

    if equity_usd <= 0 or atr <= 0 or price <= 0 or target_daily_vol_pct <= 0:
        return 0.0
    weight = max(weight, 0.0)
    target_daily_dollar_vol = equity_usd * weight * (target_daily_vol_pct / 100.0)
    quantity = target_daily_dollar_vol / atr
    if max_position_pct > 0:
        max_notional = equity_usd * (max_position_pct / 100.0)
        max_shares = max_notional / price
        quantity = min(quantity, max_shares)
    return max(quantity, 0.0)


def fractional_kelly_weight(
    *,
    win_rate: float,
    win_loss_ratio: float,
    fraction: float = 0.5,
    cap: float = 0.25,
) -> float:
    """Capital weight from a strategy's edge via fractional Kelly.

    ``f* = W - (1 - W) / R`` where ``W`` is the win probability and ``R`` the
    average win / average loss. The raw Kelly fraction is scaled by ``fraction``
    (half-Kelly by default) and clamped to ``[0, cap]``. A non-positive edge
    yields 0 — never allocate to a strategy with no demonstrated edge.

    ``win_rate`` accepts either a fraction in [0, 1] or a percentage in (1, 100].
    """

    if win_loss_ratio <= 0:
        return 0.0
    w = win_rate / 100.0 if win_rate > 1.0 else win_rate
    w = min(max(w, 0.0), 1.0)
    kelly = w - (1.0 - w) / win_loss_ratio
    if kelly <= 0:
        return 0.0
    scaled = kelly * max(fraction, 0.0)
    return min(scaled, max(cap, 0.0))


def portfolio_heat_pct(open_risks_usd: Iterable[float], equity_usd: float) -> float:
    """Total open per-trade risk as a percentage of equity."""

    if equity_usd <= 0:
        return 0.0
    total_risk = sum(max(float(r), 0.0) for r in open_risks_usd)
    return (total_risk / equity_usd) * 100.0


def heat_budget_remaining_usd(
    open_risks_usd: Iterable[float],
    *,
    equity_usd: float,
    max_heat_pct: float,
) -> float:
    """Dollar risk budget remaining before the portfolio-heat cap is hit."""

    if equity_usd <= 0 or max_heat_pct <= 0:
        return 0.0
    used = sum(max(float(r), 0.0) for r in open_risks_usd)
    cap = equity_usd * (max_heat_pct / 100.0)
    return max(cap - used, 0.0)


def fits_within_heat(
    new_risk_usd: float,
    open_risks_usd: Iterable[float],
    *,
    equity_usd: float,
    max_heat_pct: float,
) -> bool:
    """True when adding ``new_risk_usd`` keeps total heat at or under the cap."""

    remaining = heat_budget_remaining_usd(
        open_risks_usd, equity_usd=equity_usd, max_heat_pct=max_heat_pct
    )
    return max(new_risk_usd, 0.0) <= remaining + 1e-9


def drawdown_governor_multiplier(
    *,
    drawdown_pct: float,
    soft_pct: float,
    hard_pct: float,
    floor: float = 0.0,
) -> float:
    """Proportional gross-exposure multiplier as drawdown deepens.

    Returns 1.0 at or above ``-soft_pct`` (drawdown shallower than soft), ramps
    linearly down to ``floor`` as drawdown moves from soft to hard, and clamps to
    ``floor`` beyond hard. ``drawdown_pct`` is a positive magnitude (e.g. 6.0 for
    a 6% drawdown). This de-grosses smoothly instead of the binary pause/kill the
    portfolio evaluator applies, so the book breathes rather than slamming shut.
    """

    dd = abs(drawdown_pct)
    soft = abs(soft_pct)
    hard = abs(hard_pct)
    floor = min(max(floor, 0.0), 1.0)
    if dd <= soft:
        return 1.0
    if hard <= soft or dd >= hard:
        return floor
    span = hard - soft
    scaled = 1.0 - (dd - soft) / span * (1.0 - floor)
    return min(max(scaled, floor), 1.0)


def daily_drawdown_pct(*, daily_realized_pnl_usd: float, equity_usd: float) -> float:
    """The day's realized loss as a positive percentage of equity (0 if up).

    A cheap, no-extra-state drawdown proxy for an intraday sizing governor: it
    uses the realized P&L already tracked per session, so new trades shrink as
    the day's losses mount without needing a persisted equity high-water mark.
    """

    if equity_usd <= 0:
        return 0.0
    loss = min(0.0, float(daily_realized_pnl_usd))
    return (-loss / equity_usd) * 100.0


def time_stop_hit(*, bars_held: int, max_bars: int | None) -> bool:
    """True when a position has been held at least ``max_bars`` bars.

    A thesis that has not worked within its expected horizon is dead capital;
    the time stop frees it. ``max_bars`` of None or <= 0 disables the stop.
    """

    if not max_bars or max_bars <= 0:
        return False
    return bars_held >= max_bars
