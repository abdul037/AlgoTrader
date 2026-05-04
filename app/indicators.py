"""Reusable technical indicator helpers for strategy and scoring modules."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd


INTRADAY_TIMEFRAMES = {"1m", "5m", "10m", "15m"}


def enrich_technical_indicators(data: pd.DataFrame, *, timeframe: str) -> pd.DataFrame:
    """Return a copy of the OHLCV frame with a broad indicator set attached."""

    frame = data.copy().reset_index(drop=True)
    if frame.empty:
        return frame

    close = frame["close"].astype("float64")
    high = frame["high"].astype("float64")
    low = frame["low"].astype("float64")
    volume = frame["volume"].astype("float64")

    frame["sma_20"] = close.rolling(20).mean()
    frame["sma_50"] = close.rolling(50).mean()
    frame["ema_9"] = close.ewm(span=9, adjust=False).mean()
    frame["ema_20"] = close.ewm(span=20, adjust=False).mean()
    frame["ema_50"] = close.ewm(span=50, adjust=False).mean()
    frame["ema_200"] = close.ewm(span=200, adjust=False).mean()
    frame["ema_9_slope"] = frame["ema_9"].diff(3)
    frame["ema_20_slope"] = frame["ema_20"].diff(5)
    frame["ema_50_slope"] = frame["ema_50"].diff(8)

    delta = close.diff()
    gains = delta.clip(lower=0.0)
    losses = (-delta).clip(lower=0.0)
    avg_gain = gains.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    avg_loss = losses.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    frame["rsi_14"] = 100 - (100 / (1 + rs))
    rsi_min = frame["rsi_14"].rolling(14).min()
    rsi_max = frame["rsi_14"].rolling(14).max()
    frame["stoch_rsi"] = ((frame["rsi_14"] - rsi_min) / (rsi_max - rsi_min).replace(0.0, np.nan)).clip(0.0, 1.0)

    frame["macd_line"] = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    frame["macd_signal"] = frame["macd_line"].ewm(span=9, adjust=False).mean()
    frame["macd_hist"] = frame["macd_line"] - frame["macd_signal"]

    frame["bb_mid"] = close.rolling(20).mean()
    frame["bb_std"] = close.rolling(20).std()
    frame["bb_upper"] = frame["bb_mid"] + (frame["bb_std"] * 2.0)
    frame["bb_lower"] = frame["bb_mid"] - (frame["bb_std"] * 2.0)
    frame["bb_width_pct"] = ((frame["bb_upper"] - frame["bb_lower"]) / frame["bb_mid"].replace(0.0, np.nan)) * 100

    frame["prev_close"] = close.shift(1)
    frame["true_range"] = pd.concat(
        [
            high - low,
            (high - frame["prev_close"]).abs(),
            (low - frame["prev_close"]).abs(),
        ],
        axis=1,
    ).max(axis=1)
    frame["atr_14"] = frame["true_range"].rolling(14).mean()
    frame["atr_pct"] = (frame["atr_14"] / close.replace(0.0, np.nan)) * 100

    plus_dm = (high.diff()).clip(lower=0.0)
    minus_dm = (-low.diff()).clip(lower=0.0)
    plus_dm = plus_dm.where(plus_dm > minus_dm, 0.0)
    minus_dm = minus_dm.where(minus_dm > plus_dm, 0.0)
    atr_smooth = frame["true_range"].ewm(alpha=1 / 14, adjust=False).mean()
    plus_di = 100 * (plus_dm.ewm(alpha=1 / 14, adjust=False).mean() / atr_smooth.replace(0.0, np.nan))
    minus_di = 100 * (minus_dm.ewm(alpha=1 / 14, adjust=False).mean() / atr_smooth.replace(0.0, np.nan))
    dx = ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)) * 100
    frame["adx_14"] = dx.ewm(alpha=1 / 14, adjust=False).mean()

    direction = close.diff().fillna(0.0)
    frame["obv"] = (direction.gt(0).astype(int) - direction.lt(0).astype(int)) * volume
    frame["obv"] = frame["obv"].cumsum()

    frame["avg_volume_20"] = volume.rolling(20).mean()
    frame["relative_volume"] = volume / frame["avg_volume_20"].replace(0.0, np.nan)
    frame["dollar_volume"] = close * volume
    frame["avg_dollar_volume_20"] = frame["dollar_volume"].rolling(20).mean()
    frame["volume_spike"] = volume >= (frame["avg_volume_20"] * 1.5)

    frame["swing_high_10"] = high.rolling(10).max().shift(1)
    frame["swing_low_10"] = low.rolling(10).min().shift(1)
    frame["range_high_20"] = high.rolling(20).max().shift(1)
    frame["range_low_20"] = low.rolling(20).min().shift(1)
    frame["prior_day_high"] = high.shift(1).rolling(1).max()
    frame["prior_day_low"] = low.shift(1).rolling(1).min()

    if timeframe.lower() in INTRADAY_TIMEFRAMES and "timestamp" in frame.columns:
        session_date = pd.to_datetime(frame["timestamp"], utc=True).dt.date
        frame["session_date"] = session_date
        cumulative_volume = frame.groupby("session_date")["volume"].cumsum().replace(0.0, np.nan)
        cumulative_price_volume = (frame["close"] * frame["volume"]).groupby(frame["session_date"]).cumsum()
        frame["vwap"] = cumulative_price_volume / cumulative_volume
        frame["session_bar"] = frame.groupby("session_date").cumcount() + 1
        opening_range = frame[frame["session_bar"] <= 5].groupby("session_date").agg(
            opening_range_high=("high", "max"),
            opening_range_low=("low", "min"),
        )
        frame = frame.join(opening_range, on="session_date")
    else:
        cumulative_volume = volume.cumsum().replace(0.0, np.nan)
        frame["vwap"] = (close * volume).cumsum() / cumulative_volume
        frame["opening_range_high"] = np.nan
        frame["opening_range_low"] = np.nan

    return frame


def compute_confluence_score(row: pd.Series, *, is_short: bool = False) -> float:
    """Score how many useful technical conditions align on the latest bar."""

    checks = [
        _bool_score(row.get("ema_9") < row.get("ema_20") < row.get("ema_50")) if is_short else _bool_score(row.get("ema_9") > row.get("ema_20") > row.get("ema_50")),
        _bool_score(row.get("close", 0.0) < row.get("vwap", 0.0)) if is_short else _bool_score(row.get("close", 0.0) > row.get("vwap", 0.0)),
        _bool_score(row.get("rsi_14", 0.0) >= 35 and row.get("rsi_14", 0.0) <= 48) if is_short else _bool_score(row.get("rsi_14", 0.0) >= 52 and row.get("rsi_14", 0.0) <= 68),
        _bool_score(row.get("macd_hist", 0.0) < 0.0) if is_short else _bool_score(row.get("macd_hist", 0.0) > 0.0),
        _bool_score(row.get("relative_volume", 0.0) >= 1.1),
        _bool_score(row.get("adx_14", 0.0) >= 18.0),
    ]
    return round(sum(checks) / len(checks), 4)


def detect_rsi_divergence(frame: pd.DataFrame) -> dict[str, bool]:
    """Return simple bullish/bearish RSI divergence flags."""

    if len(frame) < 8 or "rsi_14" not in frame.columns:
        return {"bullish": False, "bearish": False}
    recent = frame.tail(8).reset_index(drop=True)
    left = recent.iloc[:4]
    right = recent.iloc[4:]
    bullish = float(right["low"].min()) < float(left["low"].min()) and float(right["rsi_14"].min()) > float(left["rsi_14"].min())
    bearish = float(right["high"].max()) > float(left["high"].max()) and float(right["rsi_14"].max()) < float(left["rsi_14"].max())
    return {"bullish": bool(bullish), "bearish": bool(bearish)}


def indicator_summary(row: pd.Series) -> dict[str, Any]:
    """Return a compact indicator state payload suitable for signals and Telegram output."""

    return {
        "rsi_14": _safe_round(row.get("rsi_14")),
        "stoch_rsi": _safe_round(row.get("stoch_rsi")),
        "ema_9": _safe_round(row.get("ema_9")),
        "ema_20": _safe_round(row.get("ema_20")),
        "ema_50": _safe_round(row.get("ema_50")),
        "ema_200": _safe_round(row.get("ema_200")),
        "ema_9_slope": _safe_round(row.get("ema_9_slope")),
        "ema_20_slope": _safe_round(row.get("ema_20_slope")),
        "vwap": _safe_round(row.get("vwap")),
        "macd_hist": _safe_round(row.get("macd_hist")),
        "bb_width_pct": _safe_round(row.get("bb_width_pct")),
        "atr_pct": _safe_round(row.get("atr_pct")),
        "adx_14": _safe_round(row.get("adx_14")),
        "relative_volume": _safe_round(row.get("relative_volume")),
        "avg_dollar_volume_20": _safe_round(row.get("avg_dollar_volume_20")),
        "opening_range_high": _safe_round(row.get("opening_range_high")),
        "opening_range_low": _safe_round(row.get("opening_range_low")),
        "swing_high_10": _safe_round(row.get("swing_high_10")),
        "swing_low_10": _safe_round(row.get("swing_low_10")),
        "range_high_20": _safe_round(row.get("range_high_20")),
        "range_low_20": _safe_round(row.get("range_low_20")),
    }


def _safe_round(value: Any) -> float | None:
    if value is None or pd.isna(value):
        return None
    return round(float(value), 4)


def _bool_score(condition: Any) -> float:
    return 1.0 if bool(condition) else 0.0
