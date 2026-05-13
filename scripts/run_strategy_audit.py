"""Run the Sprint 3 strategy audit and write ranked reports.

The audit is intentionally outside production code. It evaluates every
registered strategy over the top-100 US universe with the existing
BacktestEngine, default CostModel, and WalkForwardSplitter.
"""

from __future__ import annotations

import json
import math
import os
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backtesting.cost_model import CostModel
from app.backtesting.engine import BacktestEngine, EngineConfig
from app.backtesting.metrics import (
    DAILY_BARS_PER_YEAR,
    compute_max_drawdown,
    compute_sharpe_like,
    deflated_sharpe,
    expectancy_R,
    summarize_trades,
)
from app.backtesting.strategy_selection import strategy_kwargs_for
from app.backtesting.walk_forward import WalkForwardSplitter, aggregate_out_of_sample
from app.indicators import enrich_technical_indicators
from app.models.signal import Signal, SignalAction
from app.runtime_settings import get_settings
from app.strategies import STRATEGY_REGISTRY, STRATEGY_SPECS, get_strategy
from app.universe import resolve_universe


START = "2020-01-01"
END_EXCLUSIVE = "2025-01-01"
WINDOW_LABEL = "2020-01-01 to 2024-12-31"
TIMEFRAME = "1d"
CACHE_DIR = ROOT / ".cache" / "audit"
OUTPUT_DIR = ROOT / "outputs"
MARKDOWN_PATH = OUTPUT_DIR / "strategy_audit_2026_05.md"
JSON_PATH = OUTPUT_DIR / "strategy_audit_2026_05.json"


def main() -> None:
    settings = get_settings().model_copy(
        update={
            "market_universe_symbols": [],
            "market_universe_tier": "broad_top100",
            "market_universe_limit": 100,
        }
    )
    universe = env_list("AUDIT_SYMBOLS") or resolve_universe(settings, limit=100)
    strategy_names = env_list("AUDIT_STRATEGIES") or sorted(STRATEGY_REGISTRY)
    include_trade_detail = env_bool("AUDIT_INCLUDE_TRADE_DETAIL")
    unknown = sorted(set(strategy_names) - set(STRATEGY_REGISTRY))
    if unknown:
        raise ValueError(f"Unknown AUDIT_STRATEGIES entries: {', '.join(unknown)}")
    n_trials = len(strategy_names)
    splitter = WalkForwardSplitter()
    cost_model = CostModel()
    engine_config = EngineConfig(
        initial_cash=float(settings.paper_account_balance_usd),
        risk_per_trade_pct=float(settings.max_risk_per_trade_pct),
        bars_per_year=DAILY_BARS_PER_YEAR,
        cost_model=cost_model,
        allow_extended_hours=False,
    )
    risk_amount = engine_config.initial_cash * (engine_config.risk_per_trade_pct or 0.0) / 100.0

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    data_by_symbol, data_errors = load_universe_data(universe)
    results: list[dict[str, Any]] = []
    strategy_errors: list[dict[str, str]] = []
    trade_details: list[dict[str, Any]] = []

    for strategy_name in strategy_names:
        try:
            result = audit_strategy(
                strategy_name=strategy_name,
                data_by_symbol=data_by_symbol,
                settings=settings,
                splitter=splitter,
                engine_config=engine_config,
                risk_amount=risk_amount,
                n_trials=n_trials,
                include_trade_detail=include_trade_detail,
            )
            trade_details.extend(result.pop("_trade_details", []))
            results.append(result)
            print(
                f"{strategy_name}: deflated_sharpe={result['deflated_sharpe']:.4f} "
                f"sharpe={result['sharpe']:.4f} trades={result['trades']} "
                f"verdict={result['verdict']}",
                flush=True,
            )
        except Exception as exc:
            err = {
                "strategy": strategy_name,
                "error": str(exc),
                "traceback": traceback.format_exc(),
            }
            strategy_errors.append(err)
            print(f"{strategy_name}: ERROR {exc}", flush=True)

    ranked = sorted(results, key=lambda item: item["deflated_sharpe"], reverse=True)
    payload = build_payload(
        ranked=ranked,
        strategy_errors=strategy_errors,
        data_errors=data_errors,
        universe=universe,
        splitter=splitter,
        cost_model=cost_model,
        n_trials=n_trials,
    )
    if include_trade_detail:
        write_trade_detail_files(
            trade_details=trade_details,
            splitter=splitter,
            cost_model=cost_model,
        )
    else:
        MARKDOWN_PATH.write_text(render_markdown(payload), encoding="utf-8")
        JSON_PATH.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(render_stdout_ranking(ranked), flush=True)

    counts = verdict_counts(ranked)
    print(
        "verdict_counts: "
        f"production_candidate={counts['production candidate']} "
        f"needs_more_data={counts['needs more data']} "
        f"no_edge={counts['no edge at this confidence']} "
        f"errors={len(strategy_errors)} data_errors={len(data_errors)}",
        flush=True,
    )


def env_list(name: str) -> list[str]:
    raw = os.getenv(name, "")
    return [item.strip().upper() if name == "AUDIT_SYMBOLS" else item.strip().lower() for item in raw.split(",") if item.strip()]


def env_bool(name: str) -> bool:
    raw = str(os.getenv(name, "") or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def load_universe_data(universe: list[str]) -> tuple[dict[str, pd.DataFrame], list[dict[str, str]]]:
    data: dict[str, pd.DataFrame] = {}
    errors: list[dict[str, str]] = []
    for symbol in universe:
        try:
            frame = load_symbol_data(symbol)
            if frame.empty:
                errors.append({"symbol": symbol, "error": "empty data frame"})
                continue
            data[symbol] = frame
            print(f"data {symbol}: {len(frame)} rows", flush=True)
        except Exception as exc:
            errors.append({"symbol": symbol, "error": str(exc), "traceback": traceback.format_exc()})
            print(f"data {symbol}: ERROR {exc}", flush=True)
    return data, errors


def load_symbol_data(symbol: str) -> pd.DataFrame:
    cache_path = CACHE_DIR / f"{symbol.replace('.', '_')}.parquet"
    if cache_path.exists():
        return pd.read_parquet(cache_path)

    yf_symbol = symbol.replace(".", "-")
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            raw = yf.download(
                yf_symbol,
                start=START,
                end=END_EXCLUSIVE,
                interval="1d",
                auto_adjust=False,
                progress=False,
                threads=False,
            )
            frame = normalize_yfinance_frame(raw)
            if frame.empty:
                raise ValueError("yfinance returned no rows")
            frame.to_parquet(cache_path, index=False)
            return frame
        except Exception as exc:
            last_exc = exc
            if attempt == 0:
                time.sleep(5)
    raise RuntimeError(f"failed to fetch {symbol}: {last_exc}") from last_exc


def normalize_yfinance_frame(raw: pd.DataFrame) -> pd.DataFrame:
    if raw.empty:
        return pd.DataFrame()
    frame = raw.copy()
    if isinstance(frame.columns, pd.MultiIndex):
        frame.columns = [str(col[0]).lower().replace(" ", "_") for col in frame.columns]
    else:
        frame.columns = [str(col).lower().replace(" ", "_") for col in frame.columns]
    frame = frame.reset_index()
    frame.columns = [str(col).lower().replace(" ", "_") for col in frame.columns]
    date_column = "date" if "date" in frame.columns else "datetime"
    frame = frame.rename(columns={date_column: "timestamp"})
    keep = ["timestamp", "open", "high", "low", "close", "volume"]
    missing = sorted(set(keep) - set(frame.columns))
    if missing:
        raise ValueError(f"missing yfinance columns: {missing}")
    frame = frame[keep].dropna(subset=["timestamp", "open", "high", "low", "close"])
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    for column in ["open", "high", "low", "close", "volume"]:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=["open", "high", "low", "close"])
    return frame.sort_values("timestamp").reset_index(drop=True)


def audit_strategy(
    *,
    strategy_name: str,
    data_by_symbol: dict[str, pd.DataFrame],
    settings: Any,
    splitter: WalkForwardSplitter,
    engine_config: EngineConfig,
    risk_amount: float,
    n_trials: int,
    include_trade_detail: bool = False,
) -> dict[str, Any]:
    per_fold_trades: list[list[dict[str, Any]]] = []
    per_fold_metrics: list[dict[str, Any]] = []
    fold_returns: list[float] = []
    equity_curve = [engine_config.initial_cash]
    total_test_bars = 0
    symbols_evaluated = 0
    fold_count = 0
    errors: list[dict[str, str]] = []
    trade_details: list[dict[str, Any]] = []

    kwargs = strategy_kwargs(strategy_name, settings)
    worker_args = [
        {
            "symbol": symbol,
            "strategy_name": strategy_name,
            "kwargs": kwargs,
            "initial_cash": engine_config.initial_cash,
            "risk_per_trade_pct": engine_config.risk_per_trade_pct,
            "include_trade_detail": include_trade_detail,
        }
        for symbol in data_by_symbol
    ]
    max_workers = min(4, max(1, os.cpu_count() or 1), len(worker_args) or 1)
    by_symbol: dict[str, dict[str, Any]] = {}
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(audit_symbol_for_strategy, args): args["symbol"] for args in worker_args}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                by_symbol[symbol] = future.result()
            except Exception as exc:
                errors.append({"symbol": symbol, "error": str(exc), "traceback": traceback.format_exc()})

    for symbol in data_by_symbol:
        symbol_result = by_symbol.get(symbol)
        if not symbol_result:
            continue
        errors.extend(symbol_result.get("errors", []))
        total_test_bars += int(symbol_result.get("total_test_bars", 0) or 0)
        if not symbol_result.get("evaluated"):
            continue
        for window_trades in symbol_result["per_fold_trades"]:
            per_fold_trades.append(window_trades)
            per_fold_metrics.append(summarize_trades(window_trades))
            fold_pnl = sum(float(trade.get("pnl_usd", 0.0) or 0.0) for trade in window_trades)
            fold_return = fold_pnl / engine_config.initial_cash if engine_config.initial_cash else 0.0
            fold_returns.append(fold_return)
            equity_curve.append(equity_curve[-1] * (1.0 + fold_return))
            fold_count += 1
        trade_details.extend(symbol_result.get("trade_details", []))
        symbols_evaluated += 1

    aggregated = aggregate_out_of_sample(per_fold_trades, per_fold_metrics)
    trades = aggregated["merged_trades"]
    trade_summary = summarize_trades(trades)
    sharpe = compute_sharpe_like(equity_curve, bars_per_year=DAILY_BARS_PER_YEAR)
    max_dd = compute_max_drawdown(equity_curve)
    dsr = deflated_sharpe(
        sharpe,
        n_trials=n_trials,
        n_observations=total_test_bars,
        skewness=0.0,
        kurtosis=3.0,
    )
    result = {
        "strategy": strategy_name,
        "intraday_limitation": "vwap" in strategy_name.lower(),
        "deflated_sharpe": round(float(dsr), 6),
        "sharpe": round(float(sharpe), 6),
        "max_dd_pct": round(float(max_dd), 4),
        "win_rate": round(float(trade_summary.get("win_rate", 0.0) or 0.0), 4),
        "profit_factor": serializable_float(trade_summary.get("profit_factor", 0.0)),
        "expectancy_R": round(expectancy_R(trades, risk_amount), 6),
        "trades": int(trade_summary.get("number_of_trades", 0) or 0),
        "symbols_evaluated": symbols_evaluated,
        "folds": fold_count,
        "total_test_bars": total_test_bars,
        "verdict": verdict_for(float(dsr)),
        "errors": errors,
    }
    if include_trade_detail:
        result["_trade_details"] = trade_details
    return result


def audit_symbol_for_strategy(args: dict[str, Any]) -> dict[str, Any]:
    symbol = args["symbol"]
    cache_path = CACHE_DIR / f"{symbol.replace('.', '_')}.parquet"
    history = pd.read_parquet(cache_path)
    install_cached_indicator_patch()
    history = maybe_precompute_indicators(
        strategy_name=args["strategy_name"],
        kwargs=args["kwargs"],
        history=history,
    )
    splitter = WalkForwardSplitter()
    errors: list[dict[str, str]] = []
    try:
        windows = list(splitter.split(history))
    except Exception as exc:
        return {
            "symbol": symbol,
            "evaluated": False,
            "total_test_bars": 0,
            "per_fold_trades": [],
            "errors": [{"symbol": symbol, "error": f"split failed: {exc}"}],
        }
    if not windows:
        return {
            "symbol": symbol,
            "evaluated": False,
            "total_test_bars": 0,
            "per_fold_trades": [],
            "errors": [],
        }
    intervals = [(window.test_start, window.test_end) for window in windows]
    total_test_bars = sum(len(window.test_df) for window in windows)
    engine = BacktestEngine(
        config=EngineConfig(
            initial_cash=float(args["initial_cash"]),
            risk_per_trade_pct=args["risk_per_trade_pct"],
            bars_per_year=DAILY_BARS_PER_YEAR,
            cost_model=CostModel(),
            allow_extended_hours=False,
        )
    )
    try:
        strategy = _TestWindowGate(
            get_strategy(args["strategy_name"], **args["kwargs"]),
            intervals=intervals,
        )
        result = engine.run(
            symbol=symbol,
            strategy=strategy,
            data=history,
            file_path=f"yfinance:{TIMEFRAME}:{symbol}:walk_forward_signal_gate",
        )
    except Exception as exc:
        return {
            "symbol": symbol,
            "evaluated": False,
            "total_test_bars": total_test_bars,
            "per_fold_trades": [],
            "errors": [{"symbol": symbol, "error": str(exc)}],
        }
    trade_details = (
        build_trade_details(
            strategy_name=args["strategy_name"],
            symbol=symbol,
            trades=result.trades,
            history=history,
        )
        if bool(args.get("include_trade_detail"))
        else []
    )
    return {
        "symbol": symbol,
        "evaluated": True,
        "total_test_bars": total_test_bars,
        "per_fold_trades": group_trades_by_window(result.trades, intervals),
        "trade_details": trade_details,
        "errors": errors,
    }


def build_trade_details(
    *,
    strategy_name: str,
    symbol: str,
    trades: list[dict[str, Any]],
    history: pd.DataFrame,
) -> list[dict[str, Any]]:
    bars = history.copy()
    bars["timestamp"] = pd.to_datetime(bars["timestamp"], utc=True)
    bars = bars.sort_values("timestamp").reset_index(drop=True)
    timestamps = list(bars["timestamp"])
    details: list[dict[str, Any]] = []
    for trade in trades:
        entry_ts = pd.Timestamp(trade["entry_time"]).tz_convert("UTC")
        entry_index = bars["timestamp"].searchsorted(entry_ts)
        if entry_index >= len(bars) or pd.Timestamp(bars.iloc[entry_index]["timestamp"]) != entry_ts:
            entry_index = next(
                (
                    idx
                    for idx, ts in enumerate(timestamps)
                    if pd.Timestamp(ts) == entry_ts
                ),
                -1,
            )
        entry_bar = bars.iloc[entry_index] if entry_index >= 0 else None
        signal_bar = bars.iloc[entry_index - 1] if entry_index > 0 else None
        details.append(
            {
                "symbol": symbol,
                "strategy": strategy_name,
                "entry_time": trade.get("entry_time"),
                "entry_price": trade.get("entry_price"),
                "exit_time": trade.get("exit_time"),
                "exit_price": trade.get("exit_price"),
                "side": "long",
                "quantity": trade.get("quantity"),
                "entry_notional": trade.get("notional_usd"),
                "spread_cost_usd": trade.get("spread_usd"),
                "financing_cost_usd": trade.get("financing_usd"),
                "fx_cost_usd": trade.get("fx_usd"),
                "pnl_usd": trade.get("pnl_usd"),
                "signal_bar_timestamp": (
                    pd.Timestamp(signal_bar["timestamp"]).isoformat()
                    if signal_bar is not None
                    else None
                ),
                "signal_bar_close": (
                    float(signal_bar["close"])
                    if signal_bar is not None
                    else None
                ),
                "entry_bar_open": (
                    float(entry_bar["open"])
                    if entry_bar is not None
                    else None
                ),
            }
        )
    return details


def write_trade_detail_files(
    *,
    trade_details: list[dict[str, Any]],
    splitter: WalkForwardSplitter,
    cost_model: CostModel,
) -> None:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for trade in trade_details:
        key = (str(trade["strategy"]), str(trade["symbol"]))
        grouped.setdefault(key, []).append(trade)
    for (strategy_name, symbol), trades in sorted(grouped.items()):
        path = trade_detail_path(strategy_name=strategy_name, symbol=symbol)
        payload = {
            "generated_at": datetime.now(UTC).isoformat(),
            "window": WINDOW_LABEL,
            "timeframe": TIMEFRAME,
            "strategy": strategy_name,
            "symbol": symbol,
            "walk_forward": {
                "train_days": splitter.train_days,
                "test_days": splitter.test_days,
                "step_days": splitter.step_days,
                "embargo_days": splitter.embargo_days,
                "holdout_days": splitter.holdout_days,
            },
            "cost_model": {
                "spread_bps": cost_model.spread_bps,
                "half_spread_fraction": cost_model.half_spread_fraction(),
                "overnight_fee_daily_pct": cost_model.overnight_fee_daily_pct,
                "weekend_multiplier": cost_model.weekend_multiplier,
                "fx_spread_bps": cost_model.fx_spread_bps,
            },
            "trades": trades,
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        print(f"trade_detail {strategy_name} {symbol}: {len(trades)} trades -> {path}", flush=True)


def trade_detail_path(*, strategy_name: str, symbol: str) -> Path:
    safe_strategy = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in strategy_name)
    safe_symbol = "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in symbol)
    return OUTPUT_DIR / f"strategy_audit_2026_05_{safe_strategy}_{safe_symbol}_TRADES.json"


def maybe_precompute_indicators(
    *,
    strategy_name: str,
    kwargs: dict[str, object],
    history: pd.DataFrame,
) -> pd.DataFrame:
    indicator_heavy = {
        "ema_trend_stack",
        "intraday_vwap_trend",
        "rsi_reversal",
        "rsi_trend_continuation",
        "rsi_vwap_ema_confluence",
        "vwap_reclaim",
    }
    if strategy_name not in indicator_heavy:
        return history
    timeframe = str(kwargs.get("timeframe") or TIMEFRAME)
    return enrich_technical_indicators(history, timeframe=timeframe)


def install_cached_indicator_patch() -> None:
    def _cached_enrich(data: pd.DataFrame, *, timeframe: str) -> pd.DataFrame:
        if {"ema_9", "ema_20", "vwap", "atr_14", "relative_volume", "macd_hist"}.issubset(data.columns):
            return data.copy().reset_index(drop=True)
        return enrich_technical_indicators(data, timeframe=timeframe)

    import app.strategies.ema_trend_stack as ema_trend_stack
    import app.strategies.intraday_vwap_trend as intraday_vwap_trend
    import app.strategies.rsi_reversal as rsi_reversal
    import app.strategies.rsi_trend_continuation as rsi_trend_continuation
    import app.strategies.rsi_vwap_ema_confluence as rsi_vwap_ema_confluence
    import app.strategies.vwap_reclaim as vwap_reclaim

    ema_trend_stack.enrich_technical_indicators = _cached_enrich
    intraday_vwap_trend.enrich_technical_indicators = _cached_enrich
    rsi_reversal.enrich_technical_indicators = _cached_enrich
    rsi_trend_continuation.enrich_technical_indicators = _cached_enrich
    rsi_vwap_ema_confluence.enrich_technical_indicators = _cached_enrich
    vwap_reclaim.enrich_technical_indicators = _cached_enrich


class _TestWindowGate:
    """Allow entries only in walk-forward test windows, with full-history warmup."""

    def __init__(self, strategy: Any, *, intervals: list[tuple[pd.Timestamp, pd.Timestamp]]):
        self.strategy = strategy
        self.intervals = [(pd.Timestamp(start), pd.Timestamp(end)) for start, end in intervals]
        self.name = strategy.name

    def generate_signal(self, data: pd.DataFrame, symbol: str):
        if data.empty:
            return None
        current_ts = pd.Timestamp(data.iloc[-1]["timestamp"])
        if not in_any_interval(current_ts, self.intervals):
            return Signal(
                symbol=symbol,
                strategy_name=self.name,
                action=SignalAction.SELL,
                rationale="walk_forward_window_exit",
                price=float(data.iloc[-1]["close"]),
            )
        return self.strategy.generate_signal(data, symbol)


def group_trades_by_window(
    trades: list[dict[str, Any]],
    intervals: list[tuple[pd.Timestamp, pd.Timestamp]],
) -> list[list[dict[str, Any]]]:
    grouped: list[list[dict[str, Any]]] = [[] for _ in intervals]
    for trade in trades:
        entry_ts = pd.Timestamp(trade["entry_time"])
        for index, interval in enumerate(intervals):
            if in_any_interval(entry_ts, [interval]):
                grouped[index].append(trade)
                break
    return grouped


def in_any_interval(ts: pd.Timestamp, intervals: list[tuple[pd.Timestamp, pd.Timestamp]]) -> bool:
    return any(start <= ts <= end for start, end in intervals)


def strategy_kwargs(strategy_name: str, settings: Any) -> dict[str, object]:
    one_day = [spec for spec in STRATEGY_SPECS if spec.name == strategy_name and spec.timeframe == TIMEFRAME]
    candidates = one_day or [spec for spec in STRATEGY_SPECS if spec.name == strategy_name]
    if not candidates:
        return {}
    return strategy_kwargs_for(settings, candidates[0])


def serializable_float(value: Any) -> float | str:
    numeric = float(value or 0.0)
    if math.isinf(numeric):
        return "inf"
    if math.isnan(numeric):
        return 0.0
    return round(numeric, 6)


def verdict_for(confidence: float) -> str:
    if confidence >= 0.95:
        return "production candidate"
    if confidence >= 0.80:
        return "needs more data"
    return "no edge at this confidence"


def verdict_counts(results: list[dict[str, Any]]) -> dict[str, int]:
    counts = {
        "production candidate": 0,
        "needs more data": 0,
        "no edge at this confidence": 0,
    }
    for item in results:
        counts[item["verdict"]] += 1
    return counts


def build_payload(
    *,
    ranked: list[dict[str, Any]],
    strategy_errors: list[dict[str, str]],
    data_errors: list[dict[str, str]],
    universe: list[str],
    splitter: WalkForwardSplitter,
    cost_model: CostModel,
    n_trials: int,
) -> dict[str, Any]:
    return {
        "audit_date": datetime.now(UTC).isoformat(),
        "window": WINDOW_LABEL,
        "universe": "top100_us",
        "symbols_requested": len(universe),
        "symbols": universe,
        "strategies_requested_by_prompt": n_trials,
        "strategies_evaluated": len(ranked),
        "n_trials": n_trials,
        "timeframe": TIMEFRAME,
        "walk_forward": {
            "train_days": splitter.train_days,
            "test_days": splitter.test_days,
            "step_days": splitter.step_days,
            "embargo_days": splitter.embargo_days,
            "holdout_days": splitter.holdout_days,
        },
        "cost_model": {
            "spread_bps": cost_model.spread_bps,
            "extended_hours_spread_bps": cost_model.extended_hours_spread_bps,
            "overnight_fee_daily_pct": cost_model.overnight_fee_daily_pct,
            "weekend_multiplier": cost_model.weekend_multiplier,
            "fx_spread_bps": cost_model.fx_spread_bps,
            "min_position_usd": cost_model.min_position_usd,
            "include_weekend_financing": cost_model.include_weekend_financing,
        },
        "verdict_counts": verdict_counts(ranked),
        "results": ranked,
        "strategy_errors": strategy_errors,
        "data_errors": data_errors,
    }


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Strategy Audit 2026-05",
        "",
        f"- Audit date: {payload['audit_date']}",
        f"- Window: {payload['window']}",
        f"- Universe: {payload['universe']} ({payload['symbols_requested']} requested)",
        f"- Timeframe: {payload['timeframe']}",
        f"- n_trials: {payload['n_trials']}",
        (
            "- Walk-forward: "
            f"{payload['walk_forward']['train_days']}/"
            f"{payload['walk_forward']['test_days']}/"
            f"{payload['walk_forward']['step_days']}/"
            f"{payload['walk_forward']['embargo_days']}/"
            f"{payload['walk_forward']['holdout_days']} days "
            "(train/test/step/embargo/holdout)"
        ),
        (
            "- Cost model defaults: "
            f"spread_bps={payload['cost_model']['spread_bps']}, "
            f"extended_hours_spread_bps={payload['cost_model']['extended_hours_spread_bps']}, "
            f"overnight_fee_daily_pct={payload['cost_model']['overnight_fee_daily_pct']}, "
            f"weekend_multiplier={payload['cost_model']['weekend_multiplier']}, "
            f"fx_spread_bps={payload['cost_model']['fx_spread_bps']}, "
            f"min_position_usd={payload['cost_model']['min_position_usd']}"
        ),
        (
            f"- Registered strategies evaluated: {payload['strategies_evaluated']} "
            f"(prompt n_trials remains {payload['n_trials']})"
        ),
        "- Execution method: full-history warmup with entries gated to walk-forward test windows; trades are grouped by test-window entry time.",
        "",
        "Note: strategies with `vwap` in the name are intraday by nature; 1d-bar results likely understate their real performance and are flagged below.",
        "",
        "## Ranked Results",
        "",
        "| rank | strategy | intraday limit | deflated_sharpe | sharpe | max_dd_pct | win_rate | profit_factor | expectancy_R | trades |",
        "|---:|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for rank, item in enumerate(payload["results"], start=1):
        lines.append(
            "| "
            f"{rank} | {item['strategy']} | {'yes' if item['intraday_limitation'] else 'no'} | "
            f"{item['deflated_sharpe']:.6f} | {item['sharpe']:.6f} | "
            f"{item['max_dd_pct']:.4f} | {item['win_rate']:.4f} | "
            f"{item['profit_factor']} | {item['expectancy_R']:.6f} | {item['trades']} |"
        )
    counts = payload["verdict_counts"]
    lines.extend(
        [
            "",
            "## Verdict",
            "",
            "- deflated_sharpe >= 0.95: production candidate",
            "- 0.80 <= deflated_sharpe < 0.95: needs more data",
            "- deflated_sharpe < 0.80: no edge at this confidence",
            "",
            f"- production candidate: {counts['production candidate']}",
            f"- needs more data: {counts['needs more data']}",
            f"- no edge at this confidence: {counts['no edge at this confidence']}",
            "",
        ]
    )
    if payload["strategy_errors"]:
        lines.extend(["## Strategies That Errored", ""])
        for item in payload["strategy_errors"]:
            lines.append(f"- {item['strategy']}: {item['error']}")
        lines.append("")
    if payload["data_errors"]:
        lines.extend(["## Data Fetch Errors", ""])
        for item in payload["data_errors"]:
            lines.append(f"- {item['symbol']}: {item['error']}")
        lines.append("")
    for item in payload["results"]:
        if item["errors"]:
            lines.extend([f"## Run Errors: {item['strategy']}", ""])
            for error in item["errors"][:20]:
                detail = error.get("window")
                prefix = f"{error['symbol']} {detail}" if detail else error["symbol"]
                lines.append(f"- {prefix}: {error['error']}")
            if len(item["errors"]) > 20:
                lines.append(f"- ... {len(item['errors']) - 20} more")
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_stdout_ranking(ranked: list[dict[str, Any]]) -> str:
    lines = ["FINAL RANKING"]
    for rank, item in enumerate(ranked, start=1):
        lines.append(
            f"{rank}. {item['strategy']} "
            f"deflated_sharpe={item['deflated_sharpe']:.6f} "
            f"sharpe={item['sharpe']:.6f} "
            f"trades={item['trades']} "
            f"verdict={item['verdict']}"
        )
    return "\n".join(lines)


if __name__ == "__main__":
    main()
