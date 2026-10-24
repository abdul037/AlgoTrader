"""Paper-only riders: a focused swing run's paper-only specs, on the bars its own scan fetched.

Operator option B (2026-10-21): a spec the main paper gate admits on its pooled OOS evidence
alone is paper-only. In a focused swing run such specs "ride" on the run's own scan without
being part of it (app/workflow/paper_only_riders.py runs their step):

* ``KeptBars`` stands in for the market data of the run's scan: it passes every request through
  unchanged and keeps what came back (about 0.6 us a read), so the scan runs exactly as without
  riders, and the riders later read the same bars and quotes without a fetch. A rider's candidate
  checks (a one-symbol scan of its one spec) replay them the same way.
* ``rider_signals`` runs each rider's ``generate_signal`` alone on the kept bars: no candidate
  checks, no database write, no scan decision row. Each build and call is guarded (an error is
  counted, never raised), the whole is bounded by a stop, and a strategy the screener hands a data
  provider (``set_hedge_provider``: pairs fetch their hedge leg) never rides.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

FETCHING_HOOKS = ("set_hedge_provider",)  # a strategy with one fetches data in generate_signal


class KeptBars:
    """The scan's market data, kept: the same calls with the same arguments reach
    ``market_data``, and each history frame (by symbol and timeframe) and quote (by symbol) that
    comes back is kept and served again instead of being fetched again."""

    def __init__(self, market_data: Any):
        self.market_data = market_data
        self.started = time.monotonic()  # the run's scan starts now; its batch deadline too
        self.intelligence_timeout: float | None = None  # a rider check's own bound (scan_universe)
        self.history: dict[tuple[str, str], Any] = {}
        self.quotes: dict[str, Any] = {}

    def get_history(self, symbol: str, *, timeframe: str = "1d", **kwargs: Any) -> Any:
        key = (str(symbol).upper(), str(timeframe).lower())
        if key not in self.history:
            self.history[key] = self.market_data.get_history(symbol, timeframe=timeframe, **kwargs)
        return self.history[key]

    def get_quote(self, symbol: str, **kwargs: Any) -> Any:
        key = str(symbol).upper()
        if key not in self.quotes:
            self.quotes[key] = self.market_data.get_quote(symbol, **kwargs)
        return self.quotes[key]


@dataclass
class Fired:
    """A rider's signal on the kept bars of ``symbol`` and ``timeframe``."""

    key: str
    symbol: str
    timeframe: str
    signal: Any


@dataclass
class Riders:
    fired: list[Fired] = field(default_factory=list)
    excluded: int = 0  # rider specs left out: their strategy would fetch data
    errors: int = 0  # builds and generate_signal calls that raised
    stopped: bool = False  # the stop came before every rider had run


def rider_signals(screener: Any, kept: KeptBars, keys: list[str], *, stop_at: float) -> Riders:
    """Every rider spec of ``keys`` on every kept (symbol, timeframe) of its timeframe, in the
    scan's order, until ``stop_at``; never raises."""

    from app.screener.scan_support import _build_for_run, _spec_key, _strategy_specs_for_timeframe

    out = Riders()
    wanted = {str(key).strip().lower() for key in keys}
    strategies: dict[str, list[tuple[str, Any]]] = {}
    for timeframe in dict.fromkeys(timeframe for _symbol, timeframe in kept.history):
        built = []
        try:
            specs = _strategy_specs_for_timeframe(screener, timeframe, wanted)
        except Exception:  # noqa: BLE001 - a rider never fails the run
            out.errors += 1
            specs = []
        for spec in specs:
            try:
                strategy, _why = _build_for_run(screener, spec)
            except Exception:  # noqa: BLE001
                strategy = None
            if strategy is None:
                out.errors += 1
            elif any(hasattr(strategy, hook) for hook in FETCHING_HOOKS):
                out.excluded += 1
            else:
                built.append((_spec_key(spec), strategy))
        strategies[timeframe] = built
    for (symbol, timeframe), history in list(kept.history.items()):
        for key, strategy in strategies.get(timeframe, []):
            if time.monotonic() >= stop_at:
                out.stopped = True
                return out
            try:
                signal = strategy.generate_signal(history.copy(), symbol)
            except Exception:  # noqa: BLE001
                out.errors += 1
                continue
            if signal is not None:
                out.fired.append(Fired(key, symbol, timeframe, signal))
    return out
