"""Realistic cost model for backtests.

The brief's non-negotiable: a backtest without costs is marketing, not evidence.
This module implements the eToro retail cost structure from ``CODEX_BRIEF.md``:

* Stock spread 5-15 bps per side during regular hours (20-40 bps pre/post).
* CFD overnight financing at 0.015%/day on long equity positions.
* Weekend financing triple-charged on Friday close for Mon-held CFDs.
* FX spread of 1-3 pips on majors.
* Minimum position size of $50.

All costs are computed in basis points or percentage, then applied to the
notional at fill/hold time. A cost model is cheap to instantiate and safe to
share between backtest runs because it holds no state.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

import pandas as pd

_BPS = 1.0 / 10_000.0


@dataclass(frozen=True)
class CostModel:
    """Round-trip-cost inputs for a backtest.

    Defaults sit at the middle of the ranges in the brief so that reruns of old
    backtests degrade to the realistic middle case rather than to zero.
    """

    spread_bps: float = 10.0
    """Bid/ask spread per round-trip. Applied as half on entry and half on exit."""

    extended_hours_spread_bps: float = 30.0
    """Wider spread used for fills whose timestamp falls outside regular hours."""

    overnight_fee_daily_pct: float = 0.00015
    """Daily CFD financing on notional (0.015%/day). Applied for each calendar day held."""

    weekend_multiplier: float = 3.0
    """Extra multiplier applied when a position is held across the Friday close."""

    fx_spread_bps: float = 0.0
    """FX spread in bps, applied once per round-trip when the position is not quoted in the
    account's base currency. Caller passes ``fx_round_trip=True`` to activate.
    """

    min_position_usd: float = 50.0
    """Minimum notional a fill must clear. Below this, the fill is rejected outright."""

    include_weekend_financing: bool = True
    """When False, overnight financing is applied as ``holding_days * overnight_fee_daily_pct``
    without the Friday triple-charge. Leave True for equity CFDs.
    """

    commission_per_share: float = 0.0
    """Broker commission charged per share, applied on both entry and exit legs."""

    commission_min_usd: float = 0.0
    """Per-leg commission floor. A non-zero commission is raised to this minimum."""

    slippage_bps: float = 0.0
    """Non-spread execution slippage in bps, applied as a drag to each fill leg.

    This captures the gap between the model's reference fill and the price a real
    marketable order actually gets, *beyond* the quoted half-spread — queue
    position, latency, and the fact that our size walks the book. Applied to
    entry notional and exit notional independently.
    """

    sec_fee_per_dollar: float = 0.0
    """SEC Section 31 fee, charged on the *sell* side only, per dollar of notional.
    2024 rate is $27.80 per $1,000,000 sold == 0.0000278."""

    finra_taf_per_share: float = 0.0
    """FINRA Trading Activity Fee, charged on the *sell* side only, per share sold.
    2024 rate is $0.000166/share, capped per trade (see ``finra_taf_max_usd``)."""

    finra_taf_max_usd: float = 0.0
    """Per-trade cap on the FINRA TAF. Zero means uncapped (use a real cap for equities)."""

    impact_coefficient: float = 0.0
    """Square-root market-impact coefficient. Impact fraction == coef * sqrt(participation),
    where participation == order_notional / average_daily_dollar_volume. Zero disables
    ADV-based impact (callers that have no ADV rely on ``slippage_bps`` instead)."""

    @classmethod
    def alpaca_equities(cls) -> "CostModel":
        """Realistic cost defaults for US equities executed via Alpaca paper/live.

        Reference values:
          spread: ~1-3 bps round-trip on top100 names during RTH (much wider in extended hours)
          financing: 0 for cash equity positions (only applies if margin-borrowed; default cash)
          FX: 0 for USD-denominated account holding USD equities
          commission: $0 at Alpaca, but regulatory sell-side fees are real:
            SEC Section 31 fee 0.0000278/$ sold; FINRA TAF $0.000166/share sold, cap $8.30
          slippage: a conservative 1 bps/leg of non-spread execution drag
          impact: square-root model, coef 0.1 (trading 100% of ADV ~= 10% impact)
          min position: Alpaca supports fractional shares from $1; floor at $1
        """

        return cls(
            spread_bps=2.0,
            extended_hours_spread_bps=10.0,
            overnight_fee_daily_pct=0.0,
            weekend_multiplier=1.0,
            fx_spread_bps=0.0,
            min_position_usd=1.0,
            include_weekend_financing=False,
            commission_per_share=0.0,
            commission_min_usd=0.0,
            slippage_bps=1.0,
            sec_fee_per_dollar=0.0000278,
            finra_taf_per_share=0.000166,
            finra_taf_max_usd=8.30,
            impact_coefficient=0.1,
        )

    def half_spread_fraction(self, *, extended_hours: bool = False) -> float:
        """Fractional drag applied to one side of a round-trip."""

        bps = self.extended_hours_spread_bps if extended_hours else self.spread_bps
        return (bps / 2.0) * _BPS

    def entry_fill_price(
        self,
        reference_price: float,
        *,
        side: str,
        extended_hours: bool = False,
    ) -> float:
        """Return the adjusted entry price after the bid/ask half-spread.

        Longs pay the ask (reference + half-spread). Shorts receive the bid
        (reference - half-spread). ``reference_price`` is the model's "fair"
        execution reference (typically next-bar open).
        """

        if reference_price <= 0:
            raise ValueError("reference_price must be positive")
        drag = self.half_spread_fraction(extended_hours=extended_hours)
        if side == "buy":
            return reference_price * (1.0 + drag)
        if side == "sell":
            return reference_price * (1.0 - drag)
        raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")

    def exit_fill_price(
        self,
        reference_price: float,
        *,
        side: str,
        extended_hours: bool = False,
    ) -> float:
        """Return the adjusted exit price after the bid/ask half-spread.

        ``side`` refers to the *original* position direction. A long that is
        exiting sells at the bid (reference - half-spread); a short that is
        closing buys at the ask (reference + half-spread).
        """

        if reference_price <= 0:
            raise ValueError("reference_price must be positive")
        drag = self.half_spread_fraction(extended_hours=extended_hours)
        if side == "buy":
            return reference_price * (1.0 - drag)
        if side == "sell":
            return reference_price * (1.0 + drag)
        raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")

    def holding_cost_usd(
        self,
        *,
        notional_usd: float,
        entry_time: datetime,
        exit_time: datetime,
    ) -> float:
        """Dollar financing cost for a position held from ``entry_time`` to ``exit_time``.

        Both timestamps must be timezone-aware UTC per the brief's timezone rule.
        """

        if notional_usd <= 0:
            return 0.0
        if entry_time.tzinfo is None or exit_time.tzinfo is None:
            raise ValueError("entry_time and exit_time must be timezone-aware")
        if exit_time <= entry_time:
            return 0.0
        entry_utc = entry_time.astimezone(timezone.utc)
        exit_utc = exit_time.astimezone(timezone.utc)
        holding_days = (exit_utc.date() - entry_utc.date()).days
        if holding_days <= 0:
            return 0.0
        weekend_crossings = 0
        if self.include_weekend_financing:
            weekend_crossings = _count_friday_closes_crossed(entry_utc, exit_utc)
        effective_days = holding_days + weekend_crossings * (self.weekend_multiplier - 1.0)
        return notional_usd * self.overnight_fee_daily_pct * effective_days

    def fx_round_trip_cost_usd(self, *, notional_usd: float) -> float:
        """Dollar FX spread cost for a round-trip in a non-base-currency instrument."""

        if notional_usd <= 0 or self.fx_spread_bps <= 0:
            return 0.0
        return notional_usd * self.fx_spread_bps * _BPS

    def commission_usd(self, *, quantity: float) -> float:
        """Per-leg broker commission for ``quantity`` shares."""

        if quantity <= 0 or (self.commission_per_share <= 0 and self.commission_min_usd <= 0):
            return 0.0
        raw = abs(quantity) * self.commission_per_share
        if self.commission_min_usd > 0 and raw < self.commission_min_usd:
            return self.commission_min_usd
        return raw

    def slippage_usd(self, *, notional_usd: float) -> float:
        """Per-leg non-spread execution slippage in dollars."""

        if notional_usd <= 0 or self.slippage_bps <= 0:
            return 0.0
        return abs(notional_usd) * self.slippage_bps * _BPS

    def market_impact_usd(
        self,
        *,
        notional_usd: float,
        avg_daily_dollar_volume: float | None,
    ) -> float:
        """Square-root market-impact cost for a single fill leg.

        ``impact_fraction = impact_coefficient * sqrt(order_notional / ADV$)``.
        Returns 0 when impact is disabled or ADV is unknown/non-positive — callers
        without ADV fall back to the flat ``slippage_bps`` drag instead.
        """

        if (
            notional_usd <= 0
            or self.impact_coefficient <= 0
            or not avg_daily_dollar_volume
            or avg_daily_dollar_volume <= 0
        ):
            return 0.0
        participation = abs(notional_usd) / float(avg_daily_dollar_volume)
        impact_fraction = self.impact_coefficient * math.sqrt(participation)
        return abs(notional_usd) * impact_fraction

    def regulatory_sell_fee_usd(self, *, notional_usd: float, quantity: float) -> float:
        """SEC Section 31 + FINRA TAF fees, charged on the sell leg only.

        US equity regulatory fees apply to sales, not purchases. For a long
        round-trip that is the exit; callers must invoke this only on the sell.
        """

        if notional_usd <= 0:
            return 0.0
        sec = abs(notional_usd) * self.sec_fee_per_dollar if self.sec_fee_per_dollar > 0 else 0.0
        taf = 0.0
        if self.finra_taf_per_share > 0 and quantity > 0:
            taf = abs(quantity) * self.finra_taf_per_share
            if self.finra_taf_max_usd > 0:
                taf = min(taf, self.finra_taf_max_usd)
        return sec + taf

    def accepts_position(self, *, notional_usd: float) -> bool:
        """Reject fills that do not clear the minimum-position threshold."""

        return notional_usd >= self.min_position_usd


def _count_friday_closes_crossed(entry_utc: datetime, exit_utc: datetime) -> int:
    """Count how many Friday-close boundaries are crossed by the holding period."""

    if exit_utc <= entry_utc:
        return 0
    count = 0
    day = entry_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    while day <= exit_utc:
        if day.weekday() == 4 and day > entry_utc:
            count += 1
        day += timedelta(days=1)
    return count


def zero_cost_model() -> CostModel:
    """A no-op cost model used only for diagnostic comparisons.

    Do not ship a model tagged ``shadow`` or later that was trained on
    zero-cost outcomes; the brief forbids that explicitly.
    """

    return CostModel(
        spread_bps=0.0,
        extended_hours_spread_bps=0.0,
        overnight_fee_daily_pct=0.0,
        weekend_multiplier=1.0,
        fx_spread_bps=0.0,
        min_position_usd=0.0,
        include_weekend_financing=False,
    )


def is_extended_hours(timestamp: datetime | str | pd.Timestamp) -> bool:
    """Return True when a timestamp falls outside the 09:30-16:00 US/Eastern session.

    The caller is responsible for converting to America/New_York; we only look
    at the time component here so a naive conversion upstream is a bug.
    """

    if isinstance(timestamp, str):
        ts = pd.Timestamp(timestamp)
    elif isinstance(timestamp, pd.Timestamp):
        ts = timestamp
    else:
        ts = pd.Timestamp(timestamp)
    if ts.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware; naive timestamps are forbidden")
    ts_et = ts.tz_convert("America/New_York")
    hour = ts_et.hour
    minute = ts_et.minute
    if hour < 9 or (hour == 9 and minute < 30):
        return True
    if hour >= 16:
        return True
    return False


def summarize_costs(cost_events: Iterable[dict[str, float]]) -> dict[str, float]:
    """Aggregate a list of per-trade cost dicts into totals for reporting."""

    total_spread = 0.0
    total_financing = 0.0
    total_fx = 0.0
    total_commission = 0.0
    total_slippage = 0.0
    total_regulatory = 0.0
    total_impact = 0.0
    trade_count = 0
    for event in cost_events:
        total_spread += float(event.get("spread_usd", 0.0) or 0.0)
        total_financing += float(event.get("financing_usd", 0.0) or 0.0)
        total_fx += float(event.get("fx_usd", 0.0) or 0.0)
        total_commission += float(event.get("commission_usd", 0.0) or 0.0)
        total_slippage += float(event.get("slippage_usd", 0.0) or 0.0)
        total_regulatory += float(event.get("regulatory_usd", 0.0) or 0.0)
        total_impact += float(event.get("impact_usd", 0.0) or 0.0)
        trade_count += 1
    total = (
        total_spread
        + total_financing
        + total_fx
        + total_commission
        + total_slippage
        + total_regulatory
        + total_impact
    )
    return {
        "total_cost_usd": round(total, 4),
        "spread_cost_usd": round(total_spread, 4),
        "financing_cost_usd": round(total_financing, 4),
        "fx_cost_usd": round(total_fx, 4),
        "commission_cost_usd": round(total_commission, 4),
        "slippage_cost_usd": round(total_slippage, 4),
        "regulatory_cost_usd": round(total_regulatory, 4),
        "impact_cost_usd": round(total_impact, 4),
        "trades_costed": int(trade_count),
    }
