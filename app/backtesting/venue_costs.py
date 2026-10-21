"""Alpaca and stress cost profiles for minute-bar PAPER evidence (operator 2026-10-20).

Every walk-forward row is priced with eToro's fee-inclusive model (app/backtesting/batch.py),
and every eToro use of the verdicts keeps reading those numbers. A 1m/5m/10m/15m strategy
trades on Alpaca paper, though, so for its paper verdict the batch also scores it under

* ``alpaca`` -- :meth:`CostModel.alpaca_equities`: 2 bps of spread a round trip, 1 bps of
  slippage a leg, SEC/FINRA sell fees, no commission, about 4.3 bps a round trip;
* ``stress`` -- the same plus ``STRESS_SPREAD_ADD_BPS`` of spread and
  ``STRESS_SLIPPAGE_ADD_BPS_PER_LEG`` of slippage a leg, about 12.3 bps a round trip. That is
  the pessimistic 12 bps case of the 2026-10-04 re-pricing (OPS_LOG): the bot sees IEX quotes
  only (2-3% of US volume), so the spread its market orders cross and the fill a bar-late
  order gets can be well wider than Alpaca's top-of-book figure. At the $10,000 notional cap,
  where most 5m/15m entries size, that matches the eToro model (10 bps plus $2 = 12 bps);
  below the cap eToro's flat $2 weighs more.

The paper gate passes such a strategy only when both profiles' pooled OOS and holdout
expectancy are positive (app/performance/strategy_evidence.py). Both runs replay the eToro
run's signals (``RecordedSignals``), so only fills and costs differ between the three.
Crypto rows get no profiles: this is an equities cost model.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from app.backtesting.cost_model import CostModel

COST_PROFILES_FIELD = "cost_profiles"
STRESS_SPREAD_ADD_BPS = 4.0
STRESS_SLIPPAGE_ADD_BPS_PER_LEG = 2.0


def venue_cost_models() -> dict[str, CostModel]:
    """The profiles every 1m/5m/10m/15m equity row is also scored under, by name."""

    alpaca = CostModel.alpaca_equities()
    stress = replace(
        alpaca,
        spread_bps=alpaca.spread_bps + STRESS_SPREAD_ADD_BPS,
        extended_hours_spread_bps=alpaca.extended_hours_spread_bps + STRESS_SPREAD_ADD_BPS,
        slippage_bps=alpaca.slippage_bps + STRESS_SLIPPAGE_ADD_BPS_PER_LEG,
    )
    return {"alpaca": alpaca, "stress": stress}


VENUE_PROFILE_NAMES = tuple(venue_cost_models())


def profile_metrics(summary: dict[str, Any]) -> dict[str, float | int]:
    """The few numbers of a profile's walk-forward run that the verdict pools."""

    return {
        "number_of_trades": int(summary.get("number_of_trades", 0) or 0),
        "expectancy_usd": round(float(summary.get("expectancy_usd", 0.0) or 0.0), 4),
        "holdout_trades": int(summary.get("holdout_trades", 0) or 0),
        "holdout_expectancy_usd": round(
            float(summary.get("holdout_expectancy_usd", 0.0) or 0.0), 4
        ),
    }


class RecordedSignals:
    """``strategy`` with each bar's signal kept, so a re-run over the same bars (another cost
    profile, a variant) replays it instead of evaluating the strategy again, ~80% of a run's
    time. The engine asks for a signal on every evaluated bar whatever its position, so the
    replay sees exactly the signals the first run saw."""

    def __init__(self, strategy: Any) -> None:
        self._strategy = strategy
        self._signals: dict[tuple[Any, ...], Any] = {}

    def __getattr__(self, name: str) -> Any:  # name, set_hedge_provider, ...
        return getattr(self._strategy, name)

    def generate_signal(self, window: Any, symbol: str) -> Any:
        stamps = window["timestamp"]
        key = (symbol, len(window), stamps.iloc[0], stamps.iloc[-1])
        if key not in self._signals:
            self._signals[key] = self._strategy.generate_signal(window, symbol)
        return self._signals[key]
