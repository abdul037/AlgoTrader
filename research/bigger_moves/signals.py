"""Vectorised signals for the five strategies, bar for bar equal to the live classes.

Each function mirrors one class's ``generate_signal`` (default constructor kwargs unless given)
over one segment of bars, as the live bot calls it: on the last ``window`` bars (400 daily, 520
weekly: ``app/screener/service_backtests.bars_for_timeframe``). ``window=None`` gives the
engine's view instead (indicators over the whole frame, the prefix cache of
``app.indicators.precomputed_indicators``).

Rolling indicators are the same on any window that covers them. ``ewm(adjust=False)`` EMAs are
seeded at the window's first bar, so the full-history EMA is corrected exactly:
``EMA_w(t) = EMA_full(t) + (1 - a) ** (W - 1) * (x(t - W + 1) - EMA_full(t - W + 1))``.

``research/bigger_moves/parity.py`` checks every function against the real class.

Output per bar: ``buy`` / ``sell`` (the class returns that action), and for a BUY the signal's
``price``, ``stop`` and ``target`` (the plan the engine reads).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

LIVE_WINDOW = {"1d": 400, "1w": 520}
REQUIRED_BARS = {
    "inside_bar_narrow_range_breakout": 50,
    "relative_strength_momentum": 80,
    "connors_rsi2_reversion": 210,
    "overnight_drift": 210,
    "mean_reversion": 25,  # lookback + 5
}


def ema(values: np.ndarray, span: int, window: int | None) -> np.ndarray:
    full = pd.Series(values).ewm(span=span, adjust=False).mean().to_numpy()
    if window is None:
        return full
    decay = (1.0 - 2.0 / (span + 1.0)) ** (window - 1)
    start = np.arange(len(values)) - window + 1
    later = start > 0
    out = full.copy()
    out[later] = full[later] + decay * (values[start[later]] - full[start[later]])
    return out


def _base(frame: pd.DataFrame, window: int | None, emas: tuple[int, ...] = ()) -> dict[str, np.ndarray]:
    close = frame["close"].to_numpy(dtype="float64")
    high = frame["high"].to_numpy(dtype="float64")
    low = frame["low"].to_numpy(dtype="float64")
    volume = frame["volume"].to_numpy(dtype="float64")
    prev_close = np.concatenate([[np.nan], close[:-1]])
    with np.errstate(invalid="ignore"):
        true_range = np.fmax(np.fmax(high - low, np.abs(high - prev_close)), np.abs(low - prev_close))
    avg_volume = pd.Series(volume).rolling(20).mean().to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        relative_volume = volume / np.where(avg_volume == 0.0, np.nan, avg_volume)
    out = {
        "open": frame["open"].to_numpy(dtype="float64"),
        "high": high,
        "low": low,
        "close": close,
        "atr": pd.Series(true_range).rolling(14).mean().to_numpy(),
        "relative_volume": relative_volume,
        "swing_low_10": pd.Series(low).rolling(10).min().shift(1).to_numpy(),
        "n": np.arange(len(close)),
    }
    for span in emas:
        out[f"ema_{span}"] = ema(close, span, window)
    return out


def _shift(values: np.ndarray, k: int) -> np.ndarray:
    out = np.full_like(values, np.nan, dtype="float64")
    if k < len(values):
        out[k:] = values[: len(values) - k]
    return out


def _round_price(values: np.ndarray) -> np.ndarray:
    """``round(max(value, 0.01), 4)`` per element (``_common._round_price``)."""

    return np.array([round(max(float(v), 0.01), 4) if np.isfinite(v) else np.nan for v in values])


def _empty(n: int) -> dict[str, np.ndarray]:
    return {"buy": np.zeros(n, bool), "sell": np.zeros(n, bool), "price": np.full(n, np.nan),
            "stop": np.full(n, np.nan), "target": np.full(n, np.nan)}


def _enhanced_long(out: dict[str, np.ndarray], setup: np.ndarray, price: np.ndarray, stop: np.ndarray,
                   risk_multiple: float) -> dict[str, np.ndarray]:
    """``_common._long_signal`` then ``_build_signal``: rounded plan, stop < price < target."""

    with np.errstate(invalid="ignore"):
        risk = price - stop
        ok = setup & np.isfinite(stop) & (stop < price) & (risk > np.maximum(price * 0.0005, 0.01))
    idx = np.flatnonzero(ok)
    if len(idx):
        p = _round_price(price[idx])
        s = _round_price(stop[idx])
        t = _round_price(price[idx] + risk[idx] * risk_multiple)
        valid = (s < p) & (p < t)
        idx, p, s, t = idx[valid], p[valid], s[valid], t[valid]
        out["buy"][idx] = True
        out["price"][idx], out["stop"][idx], out["target"][idx] = p, s, t
    return out


def inside_bar_narrow_range_breakout(frame: pd.DataFrame, window: int | None, *, narrow_window: int = 7,
                                     minimum_relative_volume: float = 0.95, risk_multiple: float = 2.1
                                     ) -> dict[str, np.ndarray]:
    b = _base(frame, window)
    n = len(b["close"])
    out = _empty(n)
    high, low, close, atr = b["high"], b["low"], b["close"], b["atr"]
    prev_high, prev_low = _shift(high, 1), _shift(low, 1)
    high2, low2 = _shift(high, 2), _shift(low, 2)
    median_range = pd.Series(high - low).rolling(narrow_window).median().shift(1).to_numpy()
    with np.errstate(invalid="ignore"):
        narrow = (prev_high - prev_low) <= median_range * 0.75
        inside = (prev_high <= high2) & (prev_low >= low2)
        breakout = close > prev_high
        volume_ok = np.nan_to_num(b["relative_volume"], nan=0.0) >= minimum_relative_volume
        setup = (b["n"] >= REQUIRED_BARS["inside_bar_narrow_range_breakout"] - 1) & np.isfinite(atr) \
            & (narrow | inside) & breakout & volume_ok
    stop = np.fmin(prev_low, close - atr)
    return _enhanced_long(out, setup, close, stop, risk_multiple)


def relative_strength_momentum(frame: pd.DataFrame, window: int | None, *, roc_window: int = 20,
                               baseline_window: int = 60, minimum_relative_volume: float = 1.0,
                               risk_multiple: float = 2.4) -> dict[str, np.ndarray]:
    b = _base(frame, window, emas=(50, 200))
    n = len(b["close"])
    out = _empty(n)
    close, atr = b["close"], b["atr"]
    roc = close / _shift(close, roc_window) - 1.0
    baseline = pd.Series(roc).rolling(baseline_window).median().to_numpy()
    baseline = np.where(np.isfinite(baseline) & (baseline != 0.0), baseline, 0.0)
    ema_50 = np.where(np.isfinite(b["ema_50"]) & (b["ema_50"] != 0.0), b["ema_50"], close)
    ema_200 = np.where(np.isfinite(b["ema_200"]) & (b["ema_200"] != 0.0), b["ema_200"], ema_50)
    recent_low = pd.Series(b["low"]).rolling(12, min_periods=1).min().to_numpy()
    with np.errstate(invalid="ignore"):
        regime_ok = (close > ema_50) & (ema_50 >= ema_200 * 0.99)
        rs_ok = roc > np.maximum(0.015, baseline + 0.01)
        volume_ok = np.nan_to_num(b["relative_volume"], nan=0.0) >= minimum_relative_volume
        setup = (b["n"] >= REQUIRED_BARS["relative_strength_momentum"] - 1) & np.isfinite(atr) \
            & np.isfinite(roc) & regime_ok & rs_ok & volume_ok
    anchor = np.where(np.isfinite(recent_low) & (recent_low != 0.0), recent_low, close - atr)
    stop = np.fmax(anchor, close - 2.0 * atr)
    return _enhanced_long(out, setup, close, stop, risk_multiple)


def _rsi(close: np.ndarray, period: int) -> np.ndarray:
    delta = pd.Series(close).diff()
    gain = delta.clip(lower=0.0).rolling(period).mean()
    loss = (-delta.clip(upper=0.0)).rolling(period).mean()
    rs = gain / loss.replace(0.0, np.nan)
    return (100.0 - (100.0 / (1.0 + rs))).to_numpy()


def connors_rsi2_reversion(frame: pd.DataFrame, window: int | None, *, rsi_period: int = 2,
                           rsi_entry: float = 10.0, mean_window: int = 5, stop_atr_mult: float = 2.5
                           ) -> dict[str, np.ndarray]:
    b = _base(frame, window, emas=(200,))
    n = len(b["close"])
    out = _empty(n)
    close = b["close"]
    rsi = _rsi(close, rsi_period)
    mean_target = pd.Series(close).rolling(mean_window).mean().to_numpy()
    atr = np.maximum(np.maximum(b["atr"], close * 0.005), 0.01)
    with np.errstate(invalid="ignore"):
        setup = (b["n"] >= REQUIRED_BARS["connors_rsi2_reversion"] - 1) & np.isfinite(rsi) \
            & np.isfinite(b["ema_200"]) & np.isfinite(b["atr"]) & np.isfinite(mean_target) \
            & (close > b["ema_200"]) & (rsi <= rsi_entry)
    floor = np.where(np.isnan(b["swing_low_10"]), close - atr, b["swing_low_10"])
    stop = np.minimum(np.minimum(floor, close - atr * stop_atr_mult), close - atr)
    risk = np.maximum(np.maximum(np.maximum(close - stop, atr), close * 0.01), 0.01)
    target = np.where(mean_target <= close, close + risk * 1.5, mean_target)
    target = np.maximum(target, close + risk * 1.2)
    with np.errstate(invalid="ignore"):
        valid = setup & (stop > 0) & (stop < close) & (close < target)
    out["buy"] = valid
    out["price"] = np.where(valid, close, np.nan)
    out["stop"] = np.where(valid, stop, np.nan)
    out["target"] = np.where(valid, target, np.nan)
    return out


def overnight_drift(frame: pd.DataFrame, window: int | None, *, min_close_location: float = 0.6,
                    stop_atr_mult: float = 1.5, target_atr_mult: float = 1.0) -> dict[str, np.ndarray]:
    b = _base(frame, window, emas=(200,))
    n = len(b["close"])
    out = _empty(n)
    close, high, low, open_ = b["close"], b["high"], b["low"], b["open"]
    atr = np.maximum(np.maximum(b["atr"], close * 0.005), 0.01)
    bar_range = np.maximum(high - low, 1e-9)
    location = (close - low) / bar_range
    with np.errstate(invalid="ignore"):
        setup = (b["n"] >= REQUIRED_BARS["overnight_drift"] - 1) & np.isfinite(b["ema_200"]) \
            & np.isfinite(b["atr"]) & (close > b["ema_200"]) & (close > open_) & (location >= min_close_location)
    stop = close - atr * stop_atr_mult
    risk = np.maximum(np.maximum(np.maximum(close - stop, atr), close * 0.01), 0.01)
    target = close + atr * target_atr_mult
    target = np.where(target <= close, close + risk, target)
    with np.errstate(invalid="ignore"):
        valid = setup & (stop > 0) & (stop < close) & (close < target)
    out["buy"] = valid
    out["price"] = np.where(valid, close, np.nan)
    out["stop"] = np.where(valid, stop, np.nan)
    out["target"] = np.where(valid, target, np.nan)
    return out


def mean_reversion(frame: pd.DataFrame, window: int | None, *, lookback: int = 20,
                   zscore_threshold: float = 1.8) -> dict[str, np.ndarray]:
    close = frame["close"].to_numpy(dtype="float64")
    high = frame["high"].to_numpy(dtype="float64")
    low = frame["low"].to_numpy(dtype="float64")
    n = len(close)
    out = _empty(n)
    series = pd.Series(close)
    mean = series.rolling(lookback).mean().to_numpy()
    std = series.rolling(lookback).std().to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        z = (close - mean) / np.where(std == 0.0, np.nan, std)
    trend = series.rolling(lookback * 2).mean().to_numpy()
    recent_low = pd.Series(low).rolling(5).min().to_numpy()
    recent_high = pd.Series(high).rolling(5).max().to_numpy()
    enough = (np.arange(n) >= lookback + 5 - 1) & np.isfinite(z) & np.isfinite(trend)
    with np.errstate(invalid="ignore"):
        long_setup = enough & (z <= -zscore_threshold) & (close >= trend)
        short_setup = enough & (z >= zscore_threshold) & (close <= trend)
    stop = recent_low * 0.99
    risk = np.maximum(np.maximum(close - stop, close * 0.01), 0.01)
    target = np.where(mean <= close, close + risk * 1.8, mean)
    with np.errstate(invalid="ignore"):
        valid = long_setup & np.isfinite(stop) & (stop > 0) & (stop < close) & (close < target)
    out["buy"] = valid
    out["price"] = np.where(valid, close, np.nan)
    out["stop"] = np.where(valid, stop, np.nan)
    out["target"] = np.where(valid, target, np.nan)
    stop_s = recent_high * 1.01
    risk_s = np.maximum(np.maximum(stop_s - close, close * 0.01), 0.01)
    target_s = np.where(mean >= close, close - risk_s * 1.8, mean)
    with np.errstate(invalid="ignore"):
        out["sell"] = short_setup & np.isfinite(stop_s) & (target_s > 0) & (target_s < close) & (close < stop_s)
    return out


FUNCTIONS = {
    "inside_bar_narrow_range_breakout": inside_bar_narrow_range_breakout,
    "relative_strength_momentum": relative_strength_momentum,
    "connors_rsi2_reversion": connors_rsi2_reversion,
    "overnight_drift": overnight_drift,
    "mean_reversion": mean_reversion,
}


def compute(strategy: str, frame: pd.DataFrame, window: int | None, **kwargs: float) -> dict[str, np.ndarray]:
    return FUNCTIONS[strategy](frame, window, **kwargs)
