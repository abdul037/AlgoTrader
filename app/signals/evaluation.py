"""Signal evaluation helpers for the live signal service."""

from __future__ import annotations

from typing import Any

import pandas as pd

from app.live_signal_schema import LiveSignalSnapshot, MarketQuote, SignalState
from app.utils.time import utc_now


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
        snapshot = evaluate_equity(service, symbol.upper(), candles, quote)
    return service._attach_backtest_context(snapshot)


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
    risk_per_share = max(entry_price - stop_loss, entry_price * 0.02, 0.01)
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
    risk_per_share = max(entry_price - stop_loss, entry_price * 0.015, 0.01)
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
