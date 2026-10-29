"""How far a daily setup had already run at its signal bar (eToro copy rule 4, 2026-10-18).

The post-mortem of the 4-16 Oct losers: AMD was bought +23.4% in 20 sessions with RSI 69.7,
the day after its record close; META +20.4% in 20 sessions. The live eToro copy refuses such
chased entries (``app/broker/etoro_live_mirror.py`` holds the thresholds). These are its
inputs, computed where the bars are: the screener when it builds a snapshot's metadata (and so
the proposal's order metadata) and the pre-market deep scan. Nothing on paper reads them: no
score, filter, ranking, approval, sizing or risk gate.

``entry_extension`` returns one flat dict, stored NESTED under ``metadata["entry_extension"]``:
the learning layer turns every top-level numeric metadata field into a model feature (and a
new feature counts as schema drift, which can roll the model back); a nested dict is skipped.

Daily bars only -- on any other timeframe a bar is not a session, so every value is None.

* ``return_20d_pct``: the signal bar's close against the close 20 bars (sessions) earlier, in %.
* ``rsi_14``: Wilder RSI(14) on the closes, the formula ``app.indicators`` uses (where that
  gives NaN for a window with gains and no losses, this reads 100).
* ``new_high_252_signal_bar`` / ``new_high_252_prior_bar``: that bar's close is the highest
  close of the 252 sessions ending at it (a tie counts); ``new_high_252_recent`` is either one.

A value that cannot be computed (too few bars, a missing, non-finite or non-positive close)
is None -- and the copy refuses an unknown input. The function never raises.
"""

from __future__ import annotations

import math
from typing import Any

EXTENSION_FIELD = "entry_extension"
EXTENSION_TIMEFRAME = "1d"
RETURN_SESSIONS = 20
RSI_PERIOD = 14
HIGH_SESSIONS = 252


def entry_extension(frame: Any, *, timeframe: Any) -> dict[str, Any]:
    """The extension inputs for the last bar of ``frame`` (the signal bar)."""

    empty: dict[str, Any] = {
        "timeframe": str(timeframe or "").lower(),
        "bar_timestamp": None,
        "bars": 0,
        "return_20d_pct": None,
        "rsi_14": None,
        "new_high_252_signal_bar": None,
        "new_high_252_prior_bar": None,
        "new_high_252_recent": None,
    }
    try:
        if empty["timeframe"] != EXTENSION_TIMEFRAME or frame is None or "close" not in frame:
            return empty
        closes = [_price(value) for value in frame["close"].tolist()]
        if not closes:
            return empty
        signal_high = _closing_high(closes, len(closes) - 1)
        prior_high = _closing_high(closes, len(closes) - 2)
        if signal_high is True or prior_high is True:
            recent: bool | None = True
        elif signal_high is False and prior_high is False:
            recent = False
        else:
            recent = None
        stamp = frame["timestamp"].iloc[-1] if "timestamp" in frame else None
        return {
            **empty,
            "bar_timestamp": None if stamp is None else str(stamp),
            "bars": len(closes),
            "return_20d_pct": _return_pct(closes),
            "rsi_14": _rsi(closes),
            "new_high_252_signal_bar": signal_high,
            "new_high_252_prior_bar": prior_high,
            "new_high_252_recent": recent,
        }
    except Exception:  # noqa: BLE001 - an input it cannot read is unknown, never a scan error
        return empty


def _price(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _return_pct(closes: list[float | None]) -> float | None:
    if len(closes) <= RETURN_SESSIONS:
        return None
    last, base = closes[-1], closes[-1 - RETURN_SESSIONS]
    if last is None or base is None:
        return None
    return round((last / base - 1.0) * 100.0, 4)


def _closing_high(closes: list[float | None], index: int) -> bool | None:
    """Is bar ``index``'s close the highest of the HIGH_SESSIONS closes ending at it?"""

    start = index - HIGH_SESSIONS + 1
    if index < 0 or start < 0:
        return None
    window = closes[start : index + 1]
    if any(close is None for close in window):
        return None
    return bool(closes[index] >= max(window))  # type: ignore[type-var]


def _rsi(closes: list[float | None]) -> float | None:
    if len(closes) <= RSI_PERIOD or any(close is None for close in closes):
        return None
    import pandas as pd

    delta = pd.Series(closes, dtype="float64").diff()
    smooth = {"alpha": 1 / RSI_PERIOD, "min_periods": RSI_PERIOD, "adjust": False}
    gain = float(delta.clip(lower=0.0).ewm(**smooth).mean().iloc[-1])
    loss = float((-delta).clip(lower=0.0).ewm(**smooth).mean().iloc[-1])
    if not (math.isfinite(gain) and math.isfinite(loss)):
        return None
    if loss == 0.0:
        return 100.0 if gain > 0.0 else None  # flat for the whole window: undefined
    return round(100.0 - 100.0 / (1.0 + gain / loss), 4)
