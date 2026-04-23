"""Reusable backtesting engine.

Design rules (enforced by tests):

1. **No same-bar fills.** A strategy that produces a signal using bar N's close
   cannot fill on bar N. The fill lands on bar N+1's open, with cost-model
   friction applied. If bar N+1 does not exist the signal is dropped.
2. **Gap-through losses.** Stop fills assume the worse of (stop_price,
   bar.open). Take-profit fills assume the better of (tp_price, bar.open) for
   the trader, i.e. no free intrabar TP overshoot.
3. **Costs are first-class.** Every fill goes through a :class:`CostModel`.
   Spread, financing, and FX costs are subtracted from realized PnL and
   surfaced in the trade record.
4. **Risk-sized by default.** If ``risk_per_trade_pct`` is provided, position
   size is ``(equity * risk_pct / 100) / stop_distance``. If not, the engine
   falls back to the legacy all-in sizing behind an explicit flag so old
   reports can be reproduced.
5. **Bar-frequency aware.** Annualization uses ``bars_per_year`` instead of a
   hardcoded 252. The caller must supply the correct value.

If any of these invariants are relaxed, the brief's live-readiness contract is
broken. Do not work around them silently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import pandas as pd
from pydantic import BaseModel, Field

from app.backtesting.cost_model import CostModel, is_extended_hours
from app.backtesting.metrics import (
    DAILY_BARS_PER_YEAR,
    compute_max_drawdown,
    compute_sharpe_like,
    summarize_trades,
)
from app.storage.repositories import BacktestRepository
from app.utils.ids import generate_id
from app.utils.time import utc_now


@dataclass
class OpenTrade:
    """State for an open backtest position."""

    entry_time: datetime
    entry_timestamp_raw: Any
    entry_price: float
    quantity: float
    side: str
    stop_loss: float | None
    take_profit: float | None
    entry_notional: float
    entry_spread_usd: float = 0.0


@dataclass
class EngineConfig:
    """Tunable knobs for a single backtest run."""

    initial_cash: float = 10_000.0
    risk_per_trade_pct: float | None = 1.0
    """None falls back to all-in sizing (kept only for legacy reproduction)."""

    leverage: int = 1
    bars_per_year: int = DAILY_BARS_PER_YEAR
    cost_model: CostModel = field(default_factory=CostModel)
    allow_extended_hours: bool = False
    """When False, fills outside 09:30-16:00 ET are dropped entirely."""


class BacktestResult(BaseModel):
    """Backtest summary."""

    id: str
    symbol: str
    strategy_name: str
    initial_cash: float
    ending_cash: float
    metrics: dict[str, float]
    trades: list[dict[str, Any]] = Field(default_factory=list)
    cost_breakdown: dict[str, float] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class BacktestEngine:
    """Execute a strategy over historical OHLCV data."""

    def __init__(
        self,
        repository: BacktestRepository | None = None,
        *,
        config: EngineConfig | None = None,
    ):
        self.repository = repository
        self.config = config or EngineConfig()

    def run(
        self,
        *,
        symbol: str,
        strategy: Any,
        data: pd.DataFrame,
        file_path: str,
        initial_cash: float | None = None,
        config: EngineConfig | None = None,
    ) -> BacktestResult:
        run_config = config or self.config
        if initial_cash is not None:
            run_config = _override_cash(run_config, initial_cash)

        started_at = utc_now().isoformat()
        normalized = _normalize_data(data)
        timestamps = normalized["timestamp"].tolist()

        cash = run_config.initial_cash
        open_trade: OpenTrade | None = None
        equity_curve: list[float] = []
        trades: list[dict[str, Any]] = []
        cost_events: list[dict[str, float]] = []
        warnings: list[str] = []

        # Signals generated at bar N fill on bar N+1. The loop therefore stops
        # one bar short of the end for new entries, and hard-closes any open
        # trade at the last bar's close as end-of-data.
        total_bars = len(normalized)
        for index in range(total_bars):
            bar = normalized.iloc[index]
            bar_time = _ensure_utc(bar["timestamp"])
            window = normalized.iloc[: index + 1]

            # Generate the signal from data available THROUGH this bar's close.
            signal = strategy.generate_signal(window, symbol)

            # --- Exit handling for a currently open trade. -------------------
            if open_trade is not None:
                exit_action = _evaluate_exit(
                    open_trade=open_trade,
                    bar=bar,
                    signal=signal,
                    bar_index=index,
                    total_bars=total_bars,
                    next_bar=_maybe_next_bar(normalized, index),
                    cost_model=run_config.cost_model,
                    allow_extended_hours=run_config.allow_extended_hours,
                    bars_per_year=run_config.bars_per_year,
                )
                if exit_action is not None:
                    realized, cost_event, trade_record, warning = _close_trade(
                        open_trade=open_trade,
                        exit_price=exit_action.fill_price,
                        exit_time=exit_action.fill_time,
                        exit_timestamp_raw=exit_action.fill_timestamp_raw,
                        exit_reason=exit_action.reason,
                        cost_model=run_config.cost_model,
                    )
                    cash = realized
                    trades.append(trade_record)
                    cost_events.append(cost_event)
                    if warning:
                        warnings.append(warning)
                    open_trade = None

            # --- Entry handling using next-bar open. -------------------------
            if open_trade is None and signal is not None and getattr(signal.action, "value", None) == "buy":
                next_bar = _maybe_next_bar(normalized, index)
                if next_bar is None:
                    # Cannot fill without a following bar; drop the signal.
                    continue
                entry_attempt = _attempt_entry(
                    signal=signal,
                    next_bar=next_bar,
                    cash=cash,
                    config=run_config,
                )
                if entry_attempt is None:
                    continue
                open_trade = entry_attempt.open_trade
                cash = entry_attempt.cash_after_entry

            equity = _mark_to_market(cash=cash, open_trade=open_trade, bar=bar)
            equity_curve.append(equity)

        # Any trade still open at the end of data force-closes at the final close.
        if open_trade is not None and not normalized.empty:
            final_bar = normalized.iloc[-1]
            final_time = _ensure_utc(final_bar["timestamp"])
            exit_price = run_config.cost_model.exit_fill_price(
                float(final_bar["close"]),
                side=open_trade.side,
                extended_hours=False,
            )
            cash, cost_event, trade_record, warning = _close_trade(
                open_trade=open_trade,
                exit_price=exit_price,
                exit_time=final_time,
                exit_timestamp_raw=final_bar["timestamp"],
                exit_reason="end_of_data",
                cost_model=run_config.cost_model,
            )
            trades.append(trade_record)
            cost_events.append(cost_event)
            if warning:
                warnings.append(warning)
            open_trade = None
            if equity_curve:
                equity_curve[-1] = cash

        ending_cash = equity_curve[-1] if equity_curve else run_config.initial_cash
        metrics = _compile_metrics(
            equity_curve=equity_curve,
            trades=trades,
            initial_cash=run_config.initial_cash,
            ending_cash=ending_cash,
            bars_per_year=run_config.bars_per_year,
        )

        from app.backtesting.cost_model import summarize_costs  # local import to avoid cycle

        cost_breakdown = summarize_costs(cost_events)

        result = BacktestResult(
            id=generate_id("bt"),
            symbol=symbol.upper(),
            strategy_name=strategy.name,
            initial_cash=run_config.initial_cash,
            ending_cash=ending_cash,
            metrics=metrics,
            trades=trades,
            cost_breakdown=cost_breakdown,
            warnings=warnings,
        )

        if self.repository is not None:
            self.repository.create(
                backtest_id=result.id,
                symbol=result.symbol,
                strategy_name=result.strategy_name,
                file_path=file_path,
                started_at=started_at,
                completed_at=utc_now().isoformat(),
                metrics=result.metrics,
                trades=result.trades,
            )
        return result


# ---------------------------------------------------------------------------
# Helpers. Kept at module scope so they are individually testable and the
# engine body stays under the brief's 60-line function ceiling.
# ---------------------------------------------------------------------------


@dataclass
class _ExitAction:
    fill_price: float
    fill_time: datetime
    fill_timestamp_raw: Any
    reason: str


@dataclass
class _EntryAttempt:
    open_trade: OpenTrade
    cash_after_entry: float


def _override_cash(config: EngineConfig, initial_cash: float) -> EngineConfig:
    return EngineConfig(
        initial_cash=initial_cash,
        risk_per_trade_pct=config.risk_per_trade_pct,
        leverage=config.leverage,
        bars_per_year=config.bars_per_year,
        cost_model=config.cost_model,
        allow_extended_hours=config.allow_extended_hours,
    )


def _normalize_data(data: pd.DataFrame) -> pd.DataFrame:
    """Validate the OHLCV frame and coerce the timestamp column to UTC."""

    required = {"timestamp", "open", "high", "low", "close"}
    missing = required - set(data.columns)
    if missing:
        raise ValueError(f"Backtest data missing columns: {sorted(missing)}")
    frame = data.copy()
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
    frame = frame.sort_values("timestamp").reset_index(drop=True)
    return frame


def _ensure_utc(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize(timezone.utc)
    return ts.to_pydatetime()


def _maybe_next_bar(frame: pd.DataFrame, index: int) -> pd.Series | None:
    if index + 1 >= len(frame):
        return None
    return frame.iloc[index + 1]


def _evaluate_exit(
    *,
    open_trade: OpenTrade,
    bar: pd.Series,
    signal: Any,
    bar_index: int,
    total_bars: int,
    next_bar: pd.Series | None,
    cost_model: CostModel,
    allow_extended_hours: bool,
    bars_per_year: int,
) -> _ExitAction | None:
    """Decide whether the trade should close on this bar and at what price."""

    high = float(bar["high"])
    low = float(bar["low"])
    bar_open = float(bar["open"])
    bar_time = _ensure_utc(bar["timestamp"])
    extended = _is_extended(bar_time, allow_extended_hours, bars_per_year=bars_per_year)

    # Stop and take-profit are evaluated intrabar. Stop wins ties to stay
    # conservative. Gap-through is modelled by taking the worse of the level
    # and the bar's open for the trader.
    if open_trade.side == "buy":
        if open_trade.stop_loss is not None and low <= open_trade.stop_loss:
            raw_fill = min(float(open_trade.stop_loss), bar_open)
            fill = cost_model.exit_fill_price(raw_fill, side="buy", extended_hours=extended)
            return _ExitAction(fill, bar_time, bar["timestamp"], "stop_loss")
        if open_trade.take_profit is not None and high >= open_trade.take_profit:
            raw_fill = min(float(open_trade.take_profit), high)
            fill = cost_model.exit_fill_price(raw_fill, side="buy", extended_hours=extended)
            return _ExitAction(fill, bar_time, bar["timestamp"], "take_profit")
    else:  # short
        if open_trade.stop_loss is not None and high >= open_trade.stop_loss:
            raw_fill = max(float(open_trade.stop_loss), bar_open)
            fill = cost_model.exit_fill_price(raw_fill, side="sell", extended_hours=extended)
            return _ExitAction(fill, bar_time, bar["timestamp"], "stop_loss")
        if open_trade.take_profit is not None and low <= open_trade.take_profit:
            raw_fill = max(float(open_trade.take_profit), low)
            fill = cost_model.exit_fill_price(raw_fill, side="sell", extended_hours=extended)
            return _ExitAction(fill, bar_time, bar["timestamp"], "take_profit")

    # Strategy-driven exit: fills on the NEXT bar's open, not this close.
    if (
        signal is not None
        and getattr(signal.action, "value", None) == "sell"
        and next_bar is not None
    ):
        ref_price = float(next_bar["open"])
        next_time = _ensure_utc(next_bar["timestamp"])
        extended_next = _is_extended(next_time, allow_extended_hours, bars_per_year=bars_per_year)
        fill = cost_model.exit_fill_price(ref_price, side=open_trade.side, extended_hours=extended_next)
        return _ExitAction(fill, next_time, next_bar["timestamp"], "strategy_exit")

    return None


def _attempt_entry(
    *,
    signal: Any,
    next_bar: pd.Series,
    cash: float,
    config: EngineConfig,
) -> _EntryAttempt | None:
    next_open = float(next_bar["open"])
    if next_open <= 0:
        return None
    next_time = _ensure_utc(next_bar["timestamp"])
    extended = _is_extended(next_time, config.allow_extended_hours, bars_per_year=config.bars_per_year)
    if extended and not config.allow_extended_hours:
        return None
    raw_price = next_open
    fill_price = config.cost_model.entry_fill_price(raw_price, side="buy", extended_hours=extended)

    quantity = _size_position(
        signal=signal,
        fill_price=fill_price,
        cash=cash,
        config=config,
    )
    if quantity <= 0:
        return None
    notional = quantity * fill_price
    if not config.cost_model.accepts_position(notional_usd=notional):
        return None
    entry_spread = notional - (quantity * raw_price)
    # Cash goes down by full notional (ignoring leverage margin modelling; the
    # engine tracks dollar PnL for the lot rather than margin, which is fine
    # for backtest metrics that feed the screener).
    cash_after = cash - notional
    open_trade = OpenTrade(
        entry_time=next_time,
        entry_timestamp_raw=next_bar["timestamp"],
        entry_price=fill_price,
        quantity=quantity,
        side="buy",
        stop_loss=_coerce_float(getattr(signal, "stop_loss", None)),
        take_profit=_coerce_float(getattr(signal, "take_profit", None)),
        entry_notional=notional,
        entry_spread_usd=max(entry_spread, 0.0),
    )
    return _EntryAttempt(open_trade=open_trade, cash_after_entry=cash_after)


def _size_position(
    *,
    signal: Any,
    fill_price: float,
    cash: float,
    config: EngineConfig,
) -> float:
    stop = _coerce_float(getattr(signal, "stop_loss", None))
    if config.risk_per_trade_pct is None or stop is None or stop <= 0:
        if fill_price <= 0:
            return 0.0
        return cash / fill_price
    stop_distance = abs(fill_price - stop)
    if stop_distance <= 0:
        return 0.0
    risk_budget = cash * (config.risk_per_trade_pct / 100.0)
    notional_cap = cash * max(config.leverage, 1)
    raw_quantity = risk_budget / stop_distance
    notional = raw_quantity * fill_price
    if notional > notional_cap:
        raw_quantity = notional_cap / fill_price
    return raw_quantity


def _close_trade(
    *,
    open_trade: OpenTrade,
    exit_price: float,
    exit_time: datetime,
    exit_timestamp_raw: Any,
    exit_reason: str,
    cost_model: CostModel,
) -> tuple[float, dict[str, float], dict[str, Any], str | None]:
    proceeds = open_trade.quantity * exit_price
    exit_spread_usd = abs(proceeds - open_trade.quantity * _unadjusted_exit_price(exit_price, open_trade, cost_model))
    financing_usd = cost_model.holding_cost_usd(
        notional_usd=open_trade.entry_notional,
        entry_time=open_trade.entry_time,
        exit_time=exit_time,
    )
    fx_usd = cost_model.fx_round_trip_cost_usd(notional_usd=open_trade.entry_notional)
    realized = proceeds - financing_usd - fx_usd
    invested = open_trade.entry_notional
    pnl_usd = realized - invested
    pnl_pct = (pnl_usd / invested) * 100 if invested else 0.0

    warning = None
    if pnl_pct > 75.0 and exit_reason != "take_profit":
        warning = "suspicious-trade-return"

    trade_record = {
        "entry_time": open_trade.entry_time.isoformat(),
        "exit_time": exit_time.isoformat(),
        "entry_price": open_trade.entry_price,
        "exit_price": exit_price,
        "quantity": open_trade.quantity,
        "notional_usd": open_trade.entry_notional,
        "pnl_usd": pnl_usd,
        "pnl_pct": pnl_pct,
        "reason": exit_reason,
        "spread_usd": round(open_trade.entry_spread_usd + exit_spread_usd, 6),
        "financing_usd": round(financing_usd, 6),
        "fx_usd": round(fx_usd, 6),
    }
    cost_event = {
        "spread_usd": open_trade.entry_spread_usd + exit_spread_usd,
        "financing_usd": financing_usd,
        "fx_usd": fx_usd,
    }
    return realized, cost_event, trade_record, warning


def _unadjusted_exit_price(exit_price: float, open_trade: OpenTrade, cost_model: CostModel) -> float:
    """Invert the half-spread so we can quantify the exit-side spread cost."""

    drag = cost_model.half_spread_fraction()
    if open_trade.side == "buy":
        return exit_price / max(1.0 - drag, 1e-9)
    return exit_price / max(1.0 + drag, 1e-9)


def _mark_to_market(
    *,
    cash: float,
    open_trade: OpenTrade | None,
    bar: pd.Series,
) -> float:
    if open_trade is None:
        return cash
    return cash + open_trade.quantity * float(bar["close"])


def _compile_metrics(
    *,
    equity_curve: list[float],
    trades: list[dict[str, Any]],
    initial_cash: float,
    ending_cash: float,
    bars_per_year: int,
) -> dict[str, float]:
    total_return_pct = ((ending_cash - initial_cash) / initial_cash) * 100 if initial_cash else 0.0
    periods = max(len(equity_curve), 1)
    annualized_return_pct = (
        ((ending_cash / initial_cash) ** (bars_per_year / periods) - 1) * 100 if initial_cash > 0 else 0.0
    )
    metrics: dict[str, float] = {
        "total_return_pct": total_return_pct,
        "annualized_return_pct": annualized_return_pct,
        "max_drawdown_pct": compute_max_drawdown(equity_curve),
        "sharpe_like": compute_sharpe_like(equity_curve, bars_per_year=bars_per_year),
        "bars_evaluated": float(periods),
        "bars_per_year": float(bars_per_year),
    }
    metrics.update(summarize_trades(trades))
    return metrics


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_extended(bar_time: datetime, allow_extended_hours: bool, *, bars_per_year: int) -> bool:
    """Helper that tolerates naive timestamps only when extended hours are allowed.

    The brief forbids naive datetimes; if a caller is supplying them we raise
    because the downstream cost model will not trust them anyway.
    """

    if bar_time.tzinfo is None:
        raise ValueError("bar timestamps must be timezone-aware")
    if bars_per_year <= DAILY_BARS_PER_YEAR:
        return False
    try:
        return is_extended_hours(bar_time)
    except ValueError:
        if allow_extended_hours:
            return True
        raise
