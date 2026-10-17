"""Walk-forward plan for intraday (5m/15m/1h) backtests.

The daily plan (180-day train, 14-day test, 28-day holdout ~ 223 calendar days)
was applied to every timeframe, but intraday runs fetched only 350 bars (about
4.5 sessions of 5m), so no fold ever fit and the intraday strategies -- 16 of
the first 18 live paper trades -- never got an out-of-sample result.

Intraday strategies fit nothing on the train window (it is indicator warm-up
only), so it can be short. Defaults: ~120 calendar days of history, 5-day
warm-up, 7-day test folds stepping 7 days, 1-day embargo, last 14 days sealed
as holdout -- about 13 folds / ~90 days of OOS per symbol and strategy.
"""

from __future__ import annotations

import math
from typing import Any

from app.backtesting.walk_forward import WalkForwardSplitter

_MINUTES = {"1m": 1, "2m": 2, "3m": 3, "5m": 5, "10m": 10, "15m": 15, "30m": 30, "1h": 60}
SESSION_MINUTES = 390


def is_intraday(timeframe: str) -> bool:
    return str(timeframe or "").lower() in _MINUTES


def history_bars_for(timeframe: str, settings: Any) -> int:
    tf = str(timeframe or "").lower()
    if tf == "1w":
        return 520
    if not is_intraday(tf):
        return 500
    lookback_days = int(getattr(settings, "walk_forward_intraday_lookback_days", 120) or 120)
    sessions = lookback_days * 5 / 7
    return int(math.ceil(sessions * SESSION_MINUTES / _MINUTES[tf]))


def splitter_for(timeframe: str, settings: Any) -> WalkForwardSplitter:
    if is_intraday(timeframe):
        return WalkForwardSplitter(
            train_days=int(getattr(settings, "walk_forward_intraday_train_days", 5)),
            test_days=int(getattr(settings, "walk_forward_intraday_test_days", 7)),
            step_days=int(getattr(settings, "walk_forward_intraday_step_days", 7)),
            embargo_days=int(getattr(settings, "walk_forward_intraday_embargo_days", 1)),
            holdout_days=int(getattr(settings, "walk_forward_intraday_holdout_days", 14)),
        )
    return WalkForwardSplitter(
        train_days=int(getattr(settings, "walk_forward_train_days", 180)),
        test_days=int(getattr(settings, "walk_forward_test_days", 14)),
        step_days=int(getattr(settings, "walk_forward_step_days", 14)),
        embargo_days=int(getattr(settings, "walk_forward_embargo_days", 1)),
        holdout_days=int(getattr(settings, "walk_forward_holdout_days", 28)),
    )


def intraday_variants(settings: Any) -> list[tuple[str, dict[str, Any]]]:
    """Engine-config overrides evaluated alongside the live-matching baseline.

    Each name is ``hold_overnight`` or ``stop_floor_<k>`` (k prior-session ATRs).
    """

    variants: list[tuple[str, dict[str, Any]]] = []
    for name in getattr(settings, "walk_forward_intraday_variants", []) or []:
        name = str(name).strip().lower()
        if name == "hold_overnight":
            variants.append((name, {"flatten_at_session_end": False}))
        elif name.startswith("stop_floor_"):
            try:
                multiple = float(name.removeprefix("stop_floor_"))
            except ValueError:
                continue
            if multiple > 0:
                variants.append((name, {"min_stop_session_atr_multiple": multiple}))
    return variants
