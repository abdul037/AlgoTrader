"""Signal evaluation helpers for the live signal service."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pandas as pd

from app.live_signal_schema import LiveSignalSnapshot, MarketQuote, SignalState
from app.models.signal import Signal, SignalAction
from app.utils.time import utc_now


def atr_from_candles(candles: pd.DataFrame, period: int = 14) -> float:
    """Return the latest Average True Range from OHLC candles (0.0 if unavailable)."""

    if len(candles) < period + 1:
        return 0.0
    high = candles["high"].astype("float64")
    low = candles["low"].astype("float64")
    prev_close = candles["close"].astype("float64").shift(1)
    true_range = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    atr = float(true_range.rolling(period).mean().iloc[-1])
    return atr if atr == atr and atr > 0 else 0.0  # NaN/degenerate guard


def _risk_floor(service: Any, candles: pd.DataFrame, entry_price: float, pct_floor: float) -> float:
    """Minimum stop distance: ATR-based when enabled/available, else percentage."""

    settings = service.settings
    if getattr(settings, "live_signal_atr_stop_enabled", True):
        atr = atr_from_candles(candles, period=int(getattr(settings, "live_signal_atr_period", 14)))
        if atr > 0:
            return float(getattr(settings, "live_signal_atr_stop_mult", 1.5)) * atr
    return entry_price * pct_floor


def evaluate_symbol(service: Any, symbol: str) -> LiveSignalSnapshot:
    candles = service.market_data.get_daily_candles(
        symbol,
        candles_count=service.settings.live_signal_candles_count,
        interval=service.settings.live_signal_interval,
    )
    quote = service.market_data.get_rates([symbol]).get(symbol.upper())
    if quote is None:
        raise RuntimeError(f"No quote returned for {symbol.upper()}")

    if symbol.upper() == "GOLD":
        snapshot = evaluate_gold(service, symbol.upper(), candles, quote)
    else:
        # Single canonical route for equities: evaluate the full strategy catalog
        # and pick the best long setup — the same strategy library the automated
        # screener path uses. evaluate_equity_catalog falls back to the legacy
        # single-strategy snapshot internally when no strategy fires, so the
        # familiar "watch / no signal" narrative is preserved.
        snapshot = evaluate_equity_catalog(service, symbol.upper(), candles, quote)
    return service._attach_backtest_context(snapshot)


def resolve_live_strategy_names(service: Any) -> list[str]:
    """Resolve the strategy names the live path should evaluate.

    Uses ``live_signal_strategy_names`` when set, otherwise falls back to
    ``screener_active_strategy_names``; the alias ``"all"`` expands to the core
    strategy pack. The gold-only strategy is never run on equities.
    """

    from app.strategies import CORE_STRATEGY_NAMES, STRATEGY_REGISTRY

    configured = list(getattr(service.settings, "live_signal_strategy_names", []) or [])
    if not configured:
        configured = list(getattr(service.settings, "screener_active_strategy_names", []) or [])

    names: list[str] = []
    for raw in configured:
        name = str(raw).strip().lower()
        if name == "all":
            names.extend(sorted(CORE_STRATEGY_NAMES))
        elif name in STRATEGY_REGISTRY:
            names.append(name)

    resolved: list[str] = []
    seen: set[str] = set()
    for name in names:
        if name in seen or name == "gold_momentum":
            continue
        seen.add(name)
        resolved.append(name)
    return resolved or ["pullback_trend"]


def score_with_screener_ranker(
    service: Any,
    symbol: str,
    candles: pd.DataFrame,
    quote: MarketQuote,
    signal: Signal,
) -> float:
    """Score one candidate with the screener's 21-component ranker.

    Builds the same measured market context the screener uses (no network), folds
    those measurements into the signal metadata so the ranker sees real
    confluence/execution values, pulls the latest backtest summary, and returns
    the blended 0-100 ``final_score``. Lets the live path rank candidates by the
    full ranker instead of a strategy's self-reported confidence.
    """

    from app.screener.filters import build_market_context
    from app.screener.scoring import build_backtest_snapshot, rank_live_signal

    context = build_market_context(candles, quote=quote, signal=signal)
    ranked_signal = signal.model_copy(
        update={"metadata": {**(signal.metadata or {}), **context.measurements}}
    )
    summary = None
    backtests = getattr(service, "backtests", None)
    if backtests is not None:
        try:
            summary = backtests.get_latest_summary(symbol, signal.strategy_name)
        except Exception:  # noqa: BLE001 - a missing summary must not break ranking
            summary = None
    backtest_snapshot = build_backtest_snapshot(
        summary,
        validated=bool(summary),
        validation_reason="live_ranker",
    )
    ranked = rank_live_signal(
        settings=service.settings,
        signal=ranked_signal,
        context=context,
        backtest_snapshot=backtest_snapshot,
        intelligence=None,
        watchlist_only=False,
        freshness="fresh",
    )
    return float(ranked.get("final_score") or 0.0)


def evaluate_equity_catalog(
    service: Any,
    symbol: str,
    candles: pd.DataFrame,
    quote: MarketQuote,
    *,
    strategy_factory: Callable[[str], Any] | None = None,
    ranker: Callable[[Any, str, pd.DataFrame, MarketQuote, Signal], float] | None = None,
) -> LiveSignalSnapshot:
    """Run the configured strategy catalog and pick the best long setup.

    This unifies the live path with the strategy library: instead of a single
    hardcoded strategy, every configured strategy is evaluated on the symbol's
    candles and the highest-conviction qualifying BUY (ranked by confidence then
    reward-to-risk) is selected. When no strategy produces a long setup, the
    legacy single-strategy snapshot is returned so operators keep the familiar
    "watch / no signal" narrative.
    """

    if strategy_factory is None:
        from app.strategies import get_strategy

        strategy_factory = get_strategy

    use_ranker = bool(getattr(service.settings, "live_signal_use_screener_ranker", False))
    if use_ranker and ranker is None:
        ranker = score_with_screener_ranker

    names = resolve_live_strategy_names(service)
    evaluated: list[dict[str, Any]] = []
    best: tuple[tuple[float, ...], str, Signal, float | None] | None = None

    for name in names:
        try:
            strategy = strategy_factory(name)
            signal = strategy.generate_signal(candles.copy(), symbol)
        except Exception as exc:  # noqa: BLE001 - a strategy must not break the scan
            evaluated.append({"strategy": name, "status": f"error:{type(exc).__name__}"})
            continue
        if signal is None:
            evaluated.append({"strategy": name, "status": "no_signal"})
            continue
        action = signal.action.value if isinstance(signal.action, SignalAction) else str(signal.action)
        if action != SignalAction.BUY.value:
            # The live path is long-only; a SELL is not an actionable entry.
            evaluated.append({"strategy": name, "status": f"non_buy:{action}"})
            continue
        confidence = float(signal.confidence or 0.0)
        reward_to_risk = float((signal.metadata or {}).get("risk_reward_ratio") or 0.0)
        record: dict[str, Any] = {
            "strategy": name,
            "status": "buy",
            "confidence": confidence,
            "risk_reward_ratio": reward_to_risk,
        }
        # Rank by the screener's full 21-component score when enabled, else by
        # the strategy's own confidence then reward-to-risk.
        final_score: float | None = None
        if ranker is not None:
            try:
                final_score = float(ranker(service, symbol, candles, quote, signal))
            except Exception as exc:  # noqa: BLE001 - ranking failure keeps the candidate
                record["ranker_error"] = f"{type(exc).__name__}: {exc}"
                final_score = 0.0
            record["screener_final_score"] = final_score
            key: tuple[float, ...] = (final_score,)
        else:
            key = (confidence, reward_to_risk)
        evaluated.append(record)
        if best is None or key > best[0]:
            best = (key, name, signal, final_score)

    if best is None:
        snapshot = evaluate_equity(service, symbol, candles, quote)
        metadata = dict(snapshot.metadata or {})
        metadata["evaluated_strategies"] = evaluated
        metadata["strategy_selection"] = "catalog_no_buy_fallback"
        return snapshot.model_copy(update={"metadata": metadata})

    _, winner, signal, winner_score = best
    return _snapshot_from_signal(
        service, symbol, candles, quote, winner, signal, evaluated, score_override=winner_score
    )


def _snapshot_from_signal(
    service: Any,
    symbol: str,
    candles: pd.DataFrame,
    quote: MarketQuote,
    strategy_name: str,
    signal: Signal,
    evaluated: list[dict[str, Any]],
    *,
    score_override: float | None = None,
) -> LiveSignalSnapshot:
    last = candles.iloc[-1]
    current_price = quote.last_execution or quote.ask or quote.bid or float(last["close"])
    entry_price = float(signal.price or quote.ask or current_price)
    stop_loss = float(signal.stop_loss) if signal.stop_loss else None
    take_profit = float(signal.take_profit) if signal.take_profit else None

    reward_to_risk = (signal.metadata or {}).get("risk_reward_ratio")
    if reward_to_risk is None and stop_loss and take_profit and entry_price:
        risk = entry_price - stop_loss
        reward_to_risk = round((take_profit - entry_price) / risk, 2) if risk > 0 else None

    confidence = float(signal.confidence or 0.0)
    trade_supported, support_note = trade_support(service, symbol)
    candle_ts = last["timestamp"]
    candle_ts = candle_ts.isoformat() if hasattr(candle_ts, "isoformat") else str(candle_ts)

    indicators = {
        key: value
        for key, value in (signal.metadata or {}).items()
        if key not in {"risk_reward_ratio"}
    }
    return LiveSignalSnapshot(
        symbol=symbol,
        strategy_name=strategy_name,
        timeframe=service.settings.live_signal_interval,
        state=SignalState.BUY,
        generated_at=utc_now().isoformat(),
        candle_timestamp=candle_ts,
        rate_timestamp=quote.timestamp,
        current_bid=quote.bid,
        current_ask=quote.ask,
        current_price=current_price,
        entry_price=entry_price,
        exit_price=take_profit,
        stop_loss=stop_loss,
        take_profit=take_profit,
        risk_reward_ratio=float(reward_to_risk) if reward_to_risk is not None else None,
        confidence=confidence,
        score=round(score_override if score_override is not None else confidence * 100.0, 2),
        tradable=trade_supported,
        supported=trade_supported,
        asset_class="equity",
        rationale=signal.rationale,
        indicators=indicators,
        metadata={
            "data_source": "eToro",
            "data_source_verified": True,
            "support_note": support_note,
            "strategy_selection": "catalog_best_buy",
            "selected_strategy": strategy_name,
            "selection_ranker": "screener_final_score" if score_override is not None else "confidence_rr",
            "screener_final_score": score_override,
            "evaluated_strategies": evaluated,
            **indicators,
        },
    )


def evaluate_equity(
    service: Any,
    symbol: str,
    candles: pd.DataFrame,
    quote: MarketQuote,
) -> LiveSignalSnapshot:
    from app.strategies.pullback_trend import PullbackTrendStrategy

    strategy = PullbackTrendStrategy(
        trend_window=service.settings.live_signal_trend_window,
        pullback_window=service.settings.live_signal_pullback_window,
    )
    signal = strategy.generate_signal(candles.copy(), symbol)
    frame = candles.copy()
    frame["trend_ma"] = frame["close"].rolling(service.settings.live_signal_trend_window).mean()
    frame["pullback_ma"] = frame["close"].rolling(service.settings.live_signal_pullback_window).mean()
    frame["ema_short"] = frame["close"].ewm(span=8, adjust=False).mean()
    frame["ema_long"] = frame["close"].ewm(span=21, adjust=False).mean()
    frame["momentum_20"] = frame["close"].pct_change(20)
    frame["recent_low_10"] = frame["low"].rolling(10).min()

    last = frame.iloc[-1]
    prev = frame.iloc[-2]
    trend_up = (
        last["close"] > last["trend_ma"]
        and last["ema_short"] > last["ema_long"]
        and last["trend_ma"] > frame["trend_ma"].iloc[-5]
    )
    pullback_active = prev["close"] <= prev["pullback_ma"] * 1.01
    resuming_higher = last["close"] > last["pullback_ma"] and last["close"] > prev["close"]
    trade_supported, support_note = trade_support(service, symbol)

    current_price = quote.last_execution or quote.ask or quote.bid or float(last["close"])
    entry_watch = float(last["pullback_ma"]) if pd.notna(last["pullback_ma"]) else current_price
    stop_loss = float(min(last["recent_low_10"], last["trend_ma"] * 0.98))
    entry_price = quote.ask or current_price
    if signal is not None:
        stop_loss = float(signal.stop_loss or stop_loss)
        entry_price = float(signal.price or entry_price)
    risk_per_share = max(entry_price - stop_loss, _risk_floor(service, frame, entry_price, 0.02), 0.01)
    take_profit = float((entry_price if signal is not None else entry_watch) + (risk_per_share * 2.0))
    state = SignalState(signal.action.value) if signal is not None else SignalState.NONE
    if signal is not None:
        rationale = signal.rationale
        confidence = signal.confidence
    elif trend_up and not pullback_active:
        rationale = "Trend is positive but price is extended above the pullback average; wait for a cleaner retracement."
        confidence = 0.45
    elif trend_up and pullback_active and not resuming_higher:
        rationale = "Trend is positive and a pullback is active, but the rebound candle has not confirmed yet."
        confidence = 0.5
    elif not trend_up:
        rationale = "Trend filter is not aligned for a long entry on the latest closed daily bar."
        confidence = 0.3
    else:
        rationale = "No fresh signal on the latest closed daily bar."
        confidence = 0.35

    score = score_equity_setup(
        state=state,
        last_close=float(last["close"]),
        trend_ma=float(last["trend_ma"]),
        pullback_ma=float(last["pullback_ma"]),
        ema_short=float(last["ema_short"]),
        ema_long=float(last["ema_long"]),
        momentum_20=float(last["momentum_20"] or 0.0),
    )
    indicator_payload = {
        "trend_ma": round(float(last["trend_ma"]), 4),
        "pullback_ma": round(float(last["pullback_ma"]), 4),
        "ema_short": round(float(last["ema_short"]), 4),
        "ema_long": round(float(last["ema_long"]), 4),
        "momentum_20_pct": round(float(last["momentum_20"]) * 100.0, 4) if pd.notna(last["momentum_20"]) else 0.0,
        "trend_up": bool(trend_up),
        "pullback_active": bool(pullback_active),
        "resuming_higher": bool(resuming_higher),
    }
    return LiveSignalSnapshot(
        symbol=symbol,
        strategy_name=f"pullback_trend_{service.settings.live_signal_trend_window}_{service.settings.live_signal_pullback_window}",
        timeframe=service.settings.live_signal_interval,
        state=state,
        generated_at=utc_now().isoformat(),
        candle_timestamp=last["timestamp"].isoformat(),
        rate_timestamp=quote.timestamp,
        current_bid=quote.bid,
        current_ask=quote.ask,
        current_price=current_price,
        entry_price=entry_price if state == SignalState.BUY else entry_watch,
        exit_price=float(last["trend_ma"]) if state != SignalState.SELL else (quote.bid or current_price),
        stop_loss=stop_loss if state != SignalState.SELL else None,
        take_profit=take_profit if state != SignalState.SELL else None,
        confidence=confidence,
        score=score,
        tradable=trade_supported,
        supported=trade_supported,
        asset_class="equity",
        rationale=rationale,
        indicators=indicator_payload,
        metadata={
            "data_source": "eToro",
            "data_source_verified": True,
            "support_note": support_note,
            **indicator_payload,
        },
    )


def evaluate_gold(
    service: Any,
    symbol: str,
    candles: pd.DataFrame,
    quote: MarketQuote,
) -> LiveSignalSnapshot:
    from app.strategies.gold_momentum import GoldMomentumStrategy

    strategy = GoldMomentumStrategy()
    signal = strategy.generate_signal(candles.copy(), symbol)
    frame = candles.copy()
    frame["trend_ma"] = frame["close"].rolling(20).mean()
    frame["breakout_high"] = frame["high"].rolling(15).max().shift(1)
    frame["mom_5"] = frame["close"].pct_change(5)
    last = frame.iloc[-1]
    current_price = quote.last_execution or quote.ask or quote.bid or float(last["close"])
    state = SignalState(signal.action.value) if signal is not None else SignalState.NONE
    rationale = signal.rationale if signal is not None else "No fresh gold momentum signal on the latest closed daily bar."
    confidence = signal.confidence if signal is not None else 0.35
    entry_price = float(signal.price or current_price) if signal is not None else float(last["breakout_high"])
    stop_loss = float(signal.stop_loss or last["trend_ma"] * 0.985) if signal is not None else float(last["trend_ma"] * 0.985)
    risk_per_share = max(entry_price - stop_loss, _risk_floor(service, frame, entry_price, 0.015), 0.01)
    take_profit = float(signal.take_profit or (entry_price + risk_per_share * 2.0)) if signal is not None else float(entry_price + risk_per_share * 2.0)
    trade_supported, support_note = trade_support(service, symbol)
    score = 100.0 if state == SignalState.BUY else 20.0
    indicator_payload = {
        "trend_ma": round(float(last["trend_ma"]), 4),
        "breakout_high": round(float(last["breakout_high"]), 4) if pd.notna(last["breakout_high"]) else None,
        "momentum_5_pct": round(float(last["mom_5"]) * 100.0, 4) if pd.notna(last["mom_5"]) else 0.0,
    }
    return LiveSignalSnapshot(
        symbol=symbol,
        strategy_name="gold_momentum_live",
        timeframe=service.settings.live_signal_interval,
        state=state,
        generated_at=utc_now().isoformat(),
        candle_timestamp=last["timestamp"].isoformat(),
        rate_timestamp=quote.timestamp,
        current_bid=quote.bid,
        current_ask=quote.ask,
        current_price=current_price,
        entry_price=entry_price,
        exit_price=float(last["trend_ma"]),
        stop_loss=stop_loss,
        take_profit=take_profit,
        confidence=confidence,
        score=score,
        tradable=trade_supported,
        supported=trade_supported,
        asset_class="commodity",
        rationale=rationale,
        indicators=indicator_payload,
        metadata={
            "data_source": "eToro",
            "data_source_verified": True,
            "support_note": support_note,
            **indicator_payload,
        },
    )


def trade_support(service: Any, symbol: str) -> tuple[bool, str | None]:
    try:
        service.resolver.resolve(symbol)
        return True, None
    except ValueError as exc:
        return False, str(exc)


def score_equity_setup(
    *,
    state: SignalState,
    last_close: float,
    trend_ma: float,
    pullback_ma: float,
    ema_short: float,
    ema_long: float,
    momentum_20: float,
) -> float:
    state_bonus = {
        SignalState.BUY: 100.0,
        SignalState.NONE: 45.0,
        SignalState.SELL: 0.0,
    }[state]
    trend_strength = max((last_close / max(trend_ma, 0.01) - 1.0) * 100.0, -20.0)
    proximity = max(0.0, 15.0 - abs(last_close / max(pullback_ma, 0.01) - 1.0) * 1000.0)
    ema_gap = max((ema_short / max(ema_long, 0.01) - 1.0) * 200.0, -10.0)
    momentum_score = max(min(momentum_20 * 100.0, 20.0), -20.0)
    return round(state_bonus + trend_strength * 2.5 + proximity + ema_gap + momentum_score, 2)
