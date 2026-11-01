"""Vectorised ``ema_trend_stack`` (daily) signals, bar for bar equal to the live strategy.

Mirrors ``app.indicators._enrich`` (the columns the strategy reads) and
``EMATrendStackStrategy.generate_signal`` with weak signals off (the default:
``paper_strategy_weak_signal_emission_enabled = False``). ``research/pullback/parity.py``
proves the equality against the real class on rolling 400-bar windows (the live 1d fetch).

Indicators are computed once over a symbol's whole cleaned history. The live bot computes
them over its last 400 bars; the EMA seed differs, but after 400 bars the 50-day EMA's seed
weight is (49/51)**400 ~ 1e-7, i.e. float noise.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

REQUIRED_BARS = 120  # EMATrendStackStrategy.required_bars
TARGET_R = 2.2
RISK_FLOOR_ATR = 0.85


def enrich(frame: pd.DataFrame) -> pd.DataFrame:
    """The subset of ``app.indicators._enrich`` the strategy reads, computed the same way."""

    out = frame.reset_index(drop=True).copy()
    close = out["close"].astype("float64")
    high = out["high"].astype("float64")
    low = out["low"].astype("float64")
    volume = out["volume"].astype("float64")
    out["ema_9"] = close.ewm(span=9, adjust=False).mean()
    out["ema_20"] = close.ewm(span=20, adjust=False).mean()
    out["ema_50"] = close.ewm(span=50, adjust=False).mean()
    out["ema_20_slope"] = out["ema_20"].diff(5)
    out["ema_50_slope"] = out["ema_50"].diff(8)
    prev_close = close.shift(1)
    true_range = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    out["atr_14"] = true_range.rolling(14).mean()
    avg_volume = volume.rolling(20).mean()
    out["relative_volume"] = volume / avg_volume.replace(0.0, np.nan)
    out["swing_low_10"] = low.rolling(10).min().shift(1)
    out["swing_high_10"] = high.rolling(10).max().shift(1)
    return out


def compute_signals(frame: pd.DataFrame) -> pd.DataFrame:
    """Add ``buy`` / ``sell`` flags and the BUY's ``stop`` / ``target`` to an OHLCV frame.

    ``buy``: the strategy returns a BUY on this bar's close (bounce or crossover).
    ``sell``: it returns a SELL instead (bearish bounce or crossover; only when no BUY).
    Bars before the 120th have neither (``insufficient_data``), nor bars without an ATR.
    """

    out = enrich(frame)
    close = out["close"].to_numpy()
    low = out["low"].to_numpy()
    high = out["high"].to_numpy()
    e9 = out["ema_9"].to_numpy()
    e20 = out["ema_20"].to_numpy()
    e50 = out["ema_50"].to_numpy()
    s20 = out["ema_20_slope"].to_numpy()
    s50 = out["ema_50_slope"].to_numpy()
    atr = out["atr_14"].to_numpy()
    swing = out["swing_low_10"].to_numpy()
    swing_high = out["swing_high_10"].to_numpy()
    n = len(out)

    def prev(values: np.ndarray) -> np.ndarray:
        shifted = np.empty_like(values)
        shifted[0] = np.nan
        shifted[1:] = values[:-1]
        return shifted

    p_close, p_e9, p_e20 = prev(close), prev(e9), prev(e20)
    with np.errstate(invalid="ignore"):
        eligible = (np.arange(n) >= REQUIRED_BARS - 1) & np.isfinite(atr) & (atr > 0)
        long_stack = (e9 > e20) & (e20 > e50) & (s20 > 0) & (s50 > 0)
        bounce = long_stack & (p_close <= p_e20) & (close > e20)
        cross_up = (p_e9 <= p_e20) & (e9 > e20) & (close > e50)
        buy_setup = eligible & (bounce | cross_up)
        short_stack = (e9 < e20) & (e20 < e50) & (s20 < 0) & (s50 < 0)
        bear_bounce = short_stack & (p_close >= p_e20) & (close < e20)
        cross_down = (p_e9 >= p_e20) & (e9 < e20) & (close < e50)

    # ``last.get("swing_low_10") or last["low"]``: a 0.0 swing low falls back to the bar's low
    # (a NaN one would not, but it cannot occur once 120 bars are required).
    anchor = np.where(swing == 0.0, low, swing)
    stop = np.minimum(np.minimum(anchor, e50), close - atr)
    risk = np.maximum(np.maximum(close - stop, atr * RISK_FLOOR_ATR), 0.01)
    target = close + risk * TARGET_R
    # ``_build_signal`` returns None unless every plan price is finite and > 0 and
    # stop < entry < target; such a bar falls through to no signal (the strategy returns it).
    with np.errstate(invalid="ignore"):
        buy = buy_setup & np.isfinite(stop) & (stop > 0) & (stop < close) & (close < target)
        anchor_high = np.where(swing_high == 0.0, high, swing_high)
        stop_s = np.maximum(np.maximum(anchor_high, e50), close + atr)
        risk_s = np.maximum(np.maximum(stop_s - close, atr * RISK_FLOOR_ATR), 0.01)
        target_s = close - risk_s * TARGET_R
        sell = (
            eligible & ~buy_setup & (bear_bounce | cross_down)
            & np.isfinite(target_s) & (target_s > 0) & (target_s < close) & (close < stop_s)
        )

    out["buy"] = buy
    out["sell"] = sell
    out["setup"] = np.where(buy & bounce, "bounce", np.where(buy, "crossover", ""))
    out["stop"] = np.where(buy, stop, np.nan)
    out["target"] = np.where(buy, target, np.nan)
    out["signal_risk"] = np.where(buy, risk, np.nan)
    return out
