"""Judge a research candidate exactly as production judges a live minute-bar key (RESEARCH ONLY).

Per symbol, ``run_symbol`` calls the production walk-forward itself,
``BatchBacktestService._run_strategy``, with the engine config ``BatchBacktestService.run``
builds for a 5m/10m/15m key: $10,000 account, 1% risk, session flatten, fills one bar late,
eToro's fee-inclusive cost model, and the Alpaca and stress venue profiles replayed on the
same signals (``RecordedSignals``). ``pooled_verdict`` then pools the symbols' rows through
the production verdict code, ``compute_verdicts``, over an in-memory table, so the
40-trade / positive-expectancy / holdout bars are the gate's own.

Nothing is persisted: the service has no repository or run log, the rows live in a private
SQLite memory database, and ``AppSettings`` is never instantiated (it would read ``.env``);
``production_defaults`` reads the class's declared defaults instead.

For statistics the per-trade records are captured too: while ``run_symbol`` runs, the
``BacktestEngine`` name inside ``app.backtesting.batch`` is swapped for a subclass that keeps
each run's trades, tagged by cost profile and by fold or holdout. Only this process's module
global is touched, and it is restored on exit.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest import mock

import numpy as np
import pandas as pd

from app.backtesting import batch as batch_module
from app.backtesting.cost_model import CostModel, with_etoro_trade_fees, zero_cost_model
from app.backtesting.engine import BacktestEngine, EngineConfig
from app.backtesting.intraday_plan import is_intraday, is_minute_bar
from app.backtesting.metrics import bars_per_year_for
from app.backtesting.venue_costs import RecordedSignals, venue_cost_models
from app.research.intraday.session import (
    NEW_YORK,
    RTH_CLOSE_MINUTE,
    RTH_OPEN_MINUTE,
    bar_minutes_for,
)
from app.research.intraday.strategies import build_candidate

ETORO_PROFILE = "etoro"
GROSS_PROFILE = "gross"  # diagnostic only: zero costs, never part of a verdict
PRODUCTION_SETTING_NAMES = (
    "max_risk_per_trade_pct",
    "intraday_flatten_minutes_before_close",
    "min_stop_session_atr_multiple",
    "walk_forward_intraday_lookback_days",
    "walk_forward_intraday_train_days",
    "walk_forward_intraday_test_days",
    "walk_forward_intraday_step_days",
    "walk_forward_intraday_embargo_days",
    "walk_forward_intraday_holdout_days",
    "walk_forward_intraday_entry_delay_bars",
    "strategy_evidence_lookback_days",
    "strategy_evidence_min_trades",
    "strategy_evidence_min_holdout_trades",
    "strategy_evidence_min_expectancy_r",
    "paper_oos_bar_enabled",
    "primary_market_data_provider",
)


def production_defaults() -> SimpleNamespace:
    """The deployed defaults of every setting the walk-forward and the verdict read."""

    from app.runtime_settings import AppSettings

    fields = AppSettings.model_fields
    return SimpleNamespace(
        **{name: fields[name].get_default(call_default_factory=True) for name in PRODUCTION_SETTING_NAMES}
    )


@dataclass
class SymbolRun:
    symbol: str
    variant: str
    timeframe: str
    direction: str
    summary: dict[str, Any]
    # profile -> {"oos": [trade, ...], "holdout": [trade, ...]}
    trades: dict[str, dict[str, list[dict[str, Any]]]] = field(default_factory=dict)


def engine_config_for(timeframe: str, settings: Any, *, short: bool = False) -> EngineConfig:
    """``BatchBacktestService.run``'s config for an equity key on ``timeframe``."""

    minute_bars = is_minute_bar(timeframe)
    config = EngineConfig(
        initial_cash=10_000.0,
        risk_per_trade_pct=float(settings.max_risk_per_trade_pct),
        bars_per_year=bars_per_year_for(timeframe),
        flatten_at_session_end=is_intraday(timeframe)
        and float(settings.intraday_flatten_minutes_before_close or 0) > 0,
        min_stop_session_atr_multiple=float(settings.min_stop_session_atr_multiple or 0.0),
        entry_delay_bars=max(int(settings.walk_forward_intraday_entry_delay_bars or 0), 0)
        if minute_bars
        else 0,
        short_entries=short,
    )
    return replace(config, cost_model=with_etoro_trade_fees(config.cost_model, crypto=False))


def profile_configs(config: EngineConfig) -> dict[str, EngineConfig]:
    """The venue profiles the batch scores a minute-bar row under, plus the zero-cost diagnostic.

    1h rows get them too here (production gives 1h no venue profiles): research judges every
    timeframe at least as strictly as a minute-bar key.
    """

    models: dict[str, CostModel] = {**venue_cost_models(), GROSS_PROFILE: zero_cost_model()}
    return {name: replace(config, cost_model=model) for name, model in models.items()}


def run_symbol(
    frame: pd.DataFrame,
    *,
    symbol: str,
    variant: str,
    timeframe: str,
    direction: str = "long",
    settings: Any | None = None,
    strategy: Any | None = None,
) -> SymbolRun:
    """One symbol's production walk-forward (folds, holdout, all cost profiles).

    ``strategy`` overrides the candidate ``variant`` names (a live registry strategy, say, for
    calibrating this data against production's own evidence); ``variant`` then only labels it.
    """

    settings = settings or production_defaults()
    config = engine_config_for(timeframe, settings, short=direction == "short")
    profiles = profile_configs(config)
    if strategy is None:
        strategy = build_candidate(
            variant, timeframe=timeframe, direction=direction, entry_delay_bars=config.entry_delay_bars
        )
    service = batch_module.BatchBacktestService(
        settings=settings, market_data_engine=None, backtest_repository=None, run_log_repository=None
    )
    labels = {id(config.cost_model): ETORO_PROFILE}
    labels.update({id(profile.cost_model): name for name, profile in profiles.items()})
    trades: dict[str, dict[str, list[dict[str, Any]]]] = {}
    with _recording_engines(labels, trades):
        summary = service._run_strategy(
            engine=BacktestEngine(config=config),
            symbol=symbol,
            strategy=RecordedSignals(strategy),
            history=frame.copy(),
            timeframe=timeframe,
            provider="research",
            walk_forward=True,
            persist=False,
            cost_profiles=profiles,
        )
    return SymbolRun(symbol.upper(), variant, timeframe, direction, summary, trades)


@contextmanager
def _recording_engines(
    labels: dict[int, str], sink: dict[str, dict[str, list[dict[str, Any]]]]
) -> Iterator[None]:
    class RecordingEngine(BacktestEngine):
        def run(self, **kwargs: Any) -> Any:
            result = super().run(**kwargs)
            config = kwargs.get("config") or self.config
            profile = labels.get(id(config.cost_model), "unknown")
            kind = "holdout" if str(kwargs.get("file_path", "")).endswith(":holdout") else "oos"
            sink.setdefault(profile, {"oos": [], "holdout": []})[kind].extend(result.trades)
            return result

    with mock.patch.object(batch_module, "BacktestEngine", RecordingEngine):
        yield


class _MemoryBacktests:
    """The one table ``compute_verdicts`` reads, in a private SQLite memory database."""

    def __init__(self) -> None:
        self._connection = sqlite3.connect(":memory:")
        self._connection.row_factory = sqlite3.Row
        self._connection.execute(
            "CREATE TABLE backtests (strategy_name TEXT, file_path TEXT, metrics_json TEXT, completed_at TEXT)"
        )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        yield self._connection

    def add(self, run: SymbolRun, completed_at: str) -> None:
        metrics = {key: value for key, value in run.summary.items() if _jsonable(value)}
        self._connection.execute(
            "INSERT INTO backtests VALUES (?, ?, ?, ?)",
            (
                run.variant,
                f"research:{run.timeframe}:{run.symbol}:walk_forward_oos",
                json.dumps(metrics),
                completed_at,
            ),
        )


def _jsonable(value: Any) -> bool:
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return False
    return True


def pooled_verdict(runs: Iterable[SymbolRun], settings: Any | None = None) -> dict[str, Any]:
    """The production verdict for one variant and timeframe pooled over ``runs``' symbols.

    Adds ``paper_gate_full`` (``passed`` and the venue profiles: the pre-option-B paper gate)
    and ``paper_gate_oos_bar`` (option B's OOS-only admission while ``paper_oos_bar_enabled``).
    """

    from app.performance.strategy_evidence import _oos_bar, _paper_full, compute_verdicts

    settings = settings or production_defaults()
    runs = list(runs)
    if not runs:
        return {}
    keys = {(run.variant, run.timeframe) for run in runs}
    if len(keys) != 1:
        raise ValueError(f"pool one variant and timeframe at a time, got {sorted(keys)}")
    variant, timeframe = keys.pop()
    now = datetime.now(UTC)
    table = _MemoryBacktests()
    for run in runs:
        table.add(run, now.isoformat())
    verdict = compute_verdicts(table, settings, now=now).get(f"{variant}:{timeframe}", {})
    verdict["paper_gate_full"] = _paper_full(verdict, timeframe)
    verdict["paper_gate_oos_bar"] = _oos_bar(settings, verdict)
    return verdict


def trade_statistics(trades: list[dict[str, Any]], *, risk_usd: float = 100.0) -> dict[str, Any]:
    """Per-trade R statistics plus a by-day clustered t-stat (trades on one day are not
    independent: the symbols move together), and the exit-reason mix."""

    if not trades:
        return {"trades": 0}
    r = np.array([float(t["pnl_usd"]) / risk_usd for t in trades])
    days = pd.to_datetime([t["entry_time"] for t in trades], utc=True).tz_convert(NEW_YORK).date
    daily = pd.Series(r).groupby(np.asarray(days)).sum()
    out: dict[str, Any] = {
        "trades": int(len(r)),
        "win_rate": round(float((r > 0).mean() * 100.0), 2),
        "expectancy_r": round(float(r.mean()), 4),
        "t_stat": _t(r),
        "days": int(len(daily)),
        "daily_t_stat": _t(daily.to_numpy()),
        "avg_return_bps": round(float(np.mean([t["pnl_pct"] for t in trades]) * 100.0), 2),
    }
    reasons = pd.Series([t.get("reason", "") for t in trades]).value_counts()
    out["exit_reasons"] = {str(key): int(value) for key, value in reasons.items()}
    return out


def _t(values: np.ndarray) -> float | None:
    if len(values) < 3 or float(np.std(values, ddof=1)) == 0.0:
        return None
    return round(float(np.mean(values) / (np.std(values, ddof=1) / np.sqrt(len(values)))), 3)


def resample_session_bars(frame: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Regular-hours bars of ``timeframe`` built from finer ones, anchored at 09:30 ET.

    A bar is stamped with its start; bins are whole sessions' slots, so a 10m bar is
    09:30-09:40, ..., 15:50-16:00 and a 1h bar ends with the 15:30-16:00 half hour, as Alpaca
    and yfinance stamp them. Bars outside regular hours are dropped.
    """

    minutes = bar_minutes_for(timeframe)
    stamps = pd.DatetimeIndex(pd.to_datetime(frame["timestamp"], utc=True)).tz_convert(NEW_YORK)
    minute_of_day = stamps.hour * 60 + stamps.minute
    regular = (minute_of_day >= RTH_OPEN_MINUTE) & (minute_of_day < RTH_CLOSE_MINUTE)
    slot = (minute_of_day - RTH_OPEN_MINUTE) // minutes
    bin_start = stamps.normalize() + pd.to_timedelta(RTH_OPEN_MINUTE + slot * minutes, unit="m")
    work = frame.loc[regular].assign(_bin=np.asarray(bin_start[regular]))
    grouped = work.groupby("_bin", sort=True).agg(
        open=("open", "first"),
        high=("high", "max"),
        low=("low", "min"),
        close=("close", "last"),
        volume=("volume", "sum"),
    )
    grouped.index = pd.DatetimeIndex(grouped.index).tz_convert("UTC")
    return grouped.rename_axis("timestamp").reset_index()
