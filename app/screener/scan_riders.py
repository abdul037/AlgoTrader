"""Paper-only riders: a focused swing run's paper-only specs, on the bars its own scan evaluated.

Operator option B (2026-10-21): a spec the main paper gate admits on its pooled OOS evidence
alone is paper-only. In a focused swing run such specs "ride" on the run's own scan without
being part of it (app/workflow/paper_only_riders.py runs their step):

* ``KeptBars`` is handed to the run's scan. While the scan runs it is a pure pass-through: every
  market-data call reaches the engine with the same arguments and its result is returned, never
  served from here (a read that timed out and lands late changes nothing). The scan's own thread
  calls ``keep`` once every spec of the run has run on a symbol's bars for a timeframe, so only
  what the run evaluated is kept. Once the run's scan has returned, the riders' step sets
  ``replay``: from then on the kept bars and quote are served, and nothing else is fetched (a
  rider's candidate checks, a one-symbol scan of its one spec, read them the same way).
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
    """The run's market data: passed through while its scan runs, the bars and quote of each
    evaluated (symbol, timeframe) kept (``keep``), and only those served once ``replay`` is set."""

    def __init__(self, market_data: Any):
        self.market_data = market_data
        self.started = time.monotonic()  # the run's scan starts now; its batch deadline too
        self.intelligence_timeout: float | None = None  # a rider check's own bound (scan_universe)
        self.replay = False  # set by the riders' step once the run's scan has returned
        self.bars: dict[tuple[str, str], tuple[Any, Any]] = {}  # (symbol, tf) -> (history, quote)

    def keep(self, symbol: str, timeframe: str, history: Any, quote: Any) -> None:
        """From the run's scan: every spec of the run ran on these for ``symbol``/``timeframe``."""

        self.bars[(str(symbol).upper(), str(timeframe).lower())] = (history, quote)

    def get_history(self, symbol: str, **kwargs: Any) -> Any:
        if not self.replay:
            return self.market_data.get_history(symbol, **kwargs)
        return self._kept(symbol, kwargs)[0]

    def get_quote(self, symbol: str, **kwargs: Any) -> Any:
        if not self.replay:
            return self.market_data.get_quote(symbol, **kwargs)
        return self._kept(symbol, kwargs)[1]

    def _kept(self, symbol: str, kwargs: dict[str, Any]) -> tuple[Any, Any]:
        """Replay: the kept bars and quote; a pair that was not kept raises (never fetched)."""

        return self.bars[(str(symbol).upper(), str(kwargs.get("timeframe") or "1d").lower())]


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
    """Every rider spec of ``keys`` on every kept (symbol, timeframe) of its timeframe that has a
    quote, in the scan's order, until ``stop_at``; never raises."""

    from app.screener.scan_support import _build_for_run, _spec_key, _strategy_specs_for_timeframe

    out = Riders()
    wanted = {str(key).strip().lower() for key in keys}
    kept_bars = [(key, bars) for key, bars in list(kept.bars.items()) if bars[1] is not None]
    strategies: dict[str, list[tuple[str, Any]]] = {}
    for timeframe in dict.fromkeys(timeframe for (_symbol, timeframe), _bars in kept_bars):
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
    for (symbol, timeframe), (history, _quote) in kept_bars:
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
