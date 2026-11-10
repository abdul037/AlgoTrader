"""The pre-registered grid: 5 strategies x V0-V4, plus V5 (weekly) for four of them.

A variant is constructor kwargs (where the class has the knob) plus, where it has none, a
signal transform: scale the target distance (``target_scale``), drop the target
(``no_target``) and/or set ``metadata['max_hold_bars']`` (``max_hold``). ``VariantStrategy``
applies the transform to the real class for the engine; ``plan`` applies it to the vectorised
signals.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

STRATEGIES = (
    "inside_bar_narrow_range_breakout",
    "relative_strength_momentum",
    "connors_rsi2_reversion",
    "overnight_drift",
    "mean_reversion",
)
SHORT = {
    "inside_bar_narrow_range_breakout": "inside_bar",
    "relative_strength_momentum": "rs_momentum",
    "connors_rsi2_reversion": "connors_rsi2",
    "overnight_drift": "overnight_drift",
    "mean_reversion": "mean_reversion",
}
# The production 1d spec kwargs (get_strategy_specs(timeframe="1d")).
SPEC_KWARGS: dict[str, dict[str, Any]] = {
    "inside_bar_narrow_range_breakout": {"timeframe": "1d"},
    "relative_strength_momentum": {"timeframe": "1d"},
    "connors_rsi2_reversion": {"timeframe": "1d"},
    "overnight_drift": {"timeframe": "1d"},
    "mean_reversion": {"lookback": 20, "zscore_threshold": 1.8},
}
# Constructor defaults the variants change (read from the classes, checked in parity.py).
RISK_MULTIPLE = {"inside_bar_narrow_range_breakout": 2.1, "relative_strength_momentum": 2.4}


@dataclass(frozen=True)
class Variant:
    strategy: str
    name: str  # V0..V5
    label: str
    timeframe: str = "1d"
    class_kwargs: dict[str, Any] = field(default_factory=dict)  # on top of SPEC_KWARGS
    target_scale: float | None = None  # code change: target distance x this
    no_target: bool = False  # code change: no take-profit
    max_hold: int | None = None  # metadata['max_hold_bars'] (code change unless overnight_drift)
    change: str = ""  # the exact change, for the report

    @property
    def key(self) -> str:
        return f"{self.strategy}:{self.name}"

    def signal_kwargs(self) -> dict[str, Any]:
        """Kwargs for ``signals.compute`` (the timeframe kwarg only picks intraday VWAP)."""

        merged = {**SPEC_KWARGS[self.strategy], **self.class_kwargs}
        merged.pop("timeframe", None)
        merged.pop("max_hold_bars", None)
        return merged

    def constructor_kwargs(self) -> dict[str, Any]:
        merged = {**SPEC_KWARGS[self.strategy], **self.class_kwargs}
        if self.timeframe == "1w" and "timeframe" in merged:
            merged["timeframe"] = "1w"
        return merged


def _grid() -> list[Variant]:
    out: list[Variant] = []
    for strategy in STRATEGIES:
        if strategy in RISK_MULTIPLE:
            base = RISK_MULTIPLE[strategy]
            out += [
                Variant(strategy, "V0", "as is", change=f"none (risk_multiple={base:g})"),
                Variant(strategy, "V1", "target x2", class_kwargs={"risk_multiple": round(base * 2, 4)},
                        change=f"risk_multiple={base * 2:g}"),
                Variant(strategy, "V2", "target x3", class_kwargs={"risk_multiple": round(base * 3, 4)},
                        change=f"risk_multiple={base * 3:g}"),
            ]
        elif strategy == "overnight_drift":
            out += [
                Variant(strategy, "V0", "as is", max_hold=1, change="none (target_atr_mult=1.0, max_hold_bars=1)"),
                Variant(strategy, "V1", "target x2", class_kwargs={"target_atr_mult": 2.0}, max_hold=1,
                        change="target_atr_mult=2.0"),
                Variant(strategy, "V2", "target x3", class_kwargs={"target_atr_mult": 3.0}, max_hold=1,
                        change="target_atr_mult=3.0"),
            ]
        else:
            out += [
                Variant(strategy, "V0", "as is", change="none"),
                Variant(strategy, "V1", "target x2", target_scale=2.0,
                        change="target distance x2 (no kwarg: code change)"),
                Variant(strategy, "V2", "target x3", target_scale=3.0,
                        change="target distance x3 (no kwarg: code change)"),
            ]
        for name, bars in (("V3", 10), ("V4", 20)):
            if strategy == "overnight_drift":
                out.append(Variant(strategy, name, f"no target, out after {bars} bars",
                                   class_kwargs={"max_hold_bars": bars}, no_target=True, max_hold=bars,
                                   change=f"max_hold_bars={bars}, take-profit removed (code change)"))
            else:
                out.append(Variant(strategy, name, f"no target, out after {bars} bars", no_target=True,
                                   max_hold=bars, change=f"take-profit removed, max_hold_bars={bars} (code change)"))
        if strategy != "overnight_drift":
            out.append(Variant(strategy, "V5", "weekly bars, defaults", timeframe="1w",
                               change="new StrategySpec(timeframe='1w') with the defaults"))
    return out


VARIANTS: list[Variant] = _grid()
SKIPPED = {"overnight_drift:V5": "overnight hold by design (max_hold_bars=1, close-to-open); no weekly meaning"}


def plan(variant: Variant, signals: dict[str, np.ndarray]) -> tuple[np.ndarray, int | None]:
    """The engine's take-profit per bar for this variant (NaN: none) and its time stop."""

    target = signals["target"].copy()
    if variant.no_target:
        target[:] = np.nan
    elif variant.target_scale is not None:
        price = signals["price"]
        target = price + variant.target_scale * (target - price)
    return target, variant.max_hold


class VariantStrategy:
    """The real class with this variant's transform, for the real engine (parity, calibration)."""

    def __init__(self, base: Any, variant: Variant):
        self.base = base
        self.variant = variant
        self.name = base.name
        self.required_bars = base.required_bars

    def generate_signal(self, data: Any, symbol: str) -> Any:
        signal = self.base.generate_signal(data, symbol)
        if signal is None or getattr(signal.action, "value", None) != "buy":
            return signal
        update: dict[str, Any] = {}
        if self.variant.no_target:
            update["take_profit"] = None
        elif self.variant.target_scale is not None:
            price = float(signal.price)
            update["take_profit"] = price + self.variant.target_scale * (float(signal.take_profit) - price)
        if self.variant.max_hold is not None:
            update["metadata"] = {**signal.metadata, "max_hold_bars": self.variant.max_hold}
        return signal.model_copy(update=update) if update else signal


def make_strategy(variant: Variant) -> Any:
    from app.strategies import get_strategy

    base = get_strategy(variant.strategy, **variant.constructor_kwargs())
    if variant.no_target or variant.target_scale is not None or (
        variant.max_hold is not None and variant.strategy != "overnight_drift"
    ):
        return VariantStrategy(base, variant)
    return base
