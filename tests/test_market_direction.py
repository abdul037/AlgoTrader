"""No new intraday longs while the market is falling (SPY, plus QQQ for tech).

2026-09-28: four tech longs opened into a down day all stopped out (-$601).
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd

from app.risk.market_direction import benchmark_state, market_direction_reasons

NOW = pd.Timestamp("2026-10-05T16:00:00Z")  # Monday 12:00 ET


def _day(prior_close: float, today_path: list[float]) -> pd.DataFrame:
    rows = [
        {
            "timestamp": pd.Timestamp("2026-10-02T19:55:00Z"),
            "open": prior_close,
            "high": prior_close,
            "low": prior_close,
            "close": prior_close,
            "volume": 1000,
        }
    ]
    for i, price in enumerate(today_path):
        ts = pd.Timestamp("2026-10-05T13:30:00Z") + pd.Timedelta(minutes=5 * i)
        rows.append(
            {
                "timestamp": ts,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": 1000,
            }
        )
    return pd.DataFrame(rows)


class _Data:
    def __init__(self, frames: dict[str, pd.DataFrame | Exception]):
        self.frames = frames
        self.asked: list[str] = []

    def get_history(self, symbol, *, timeframe, bars):
        self.asked.append(symbol)
        frame = self.frames[symbol]
        if isinstance(frame, Exception):
            raise frame
        return frame


class _Logs:
    def __init__(self):
        self.events = []

    def log(self, event, payload):
        self.events.append(event)


SETTINGS = SimpleNamespace(market_direction_min_drop_pct=0.3)
FALLING = _day(100.0, [100.0, 99.8, 99.5, 99.4])  # -0.6%, below VWAP
RISING = _day(100.0, [100.0, 100.3, 100.6, 100.8])
DOWN_BUT_RECOVERING = _day(100.0, [98.0, 98.5, 99.2, 99.6])  # -0.4% but above VWAP


def _check(data, symbol="COST", timeframe="15m", side="buy", logs=None):
    return market_direction_reasons(
        SETTINGS, data, symbol=symbol, timeframe=timeframe, side=side, logs=logs, now=NOW
    )


def test_state_uses_today_vwap_and_prior_close() -> None:
    state = benchmark_state(FALLING, now=NOW)
    assert round(state["change_pct"], 2) == -0.6
    assert state["last"] < state["vwap"]


def test_blocks_intraday_buy_on_a_falling_market() -> None:
    assert _check(_Data({"SPY": FALLING})) == ["market_direction_down:SPY:-0.60%"]


def test_allows_rising_or_recovering_market() -> None:
    assert _check(_Data({"SPY": RISING})) == []
    assert _check(_Data({"SPY": DOWN_BUT_RECOVERING})) == []


def test_tech_names_also_check_qqq() -> None:
    data = _Data({"SPY": RISING, "QQQ": FALLING})
    assert _check(data, symbol="NVDA") == ["market_direction_down:QQQ:-0.60%"]
    assert data.asked == ["SPY", "QQQ"]


def test_swing_entries_and_sells_are_exempt() -> None:
    data = _Data({"SPY": FALLING})
    assert _check(data, timeframe="1d") == []
    assert _check(data, side="sell") == []
    assert data.asked == []


def test_missing_or_stale_data_never_blocks() -> None:
    logs = _Logs()
    assert _check(_Data({"SPY": RuntimeError("feed down")}), logs=logs) == []
    stale = FALLING[FALLING["timestamp"] < pd.Timestamp("2026-10-05", tz="UTC")]  # no bars today
    assert _check(_Data({"SPY": stale}), logs=logs) == []
    assert logs.events == ["market_direction_check_unavailable"] * 2


def test_off_when_threshold_zero() -> None:
    off = SimpleNamespace(market_direction_min_drop_pct=0.0)
    assert (
        market_direction_reasons(
            off, _Data({"SPY": FALLING}), symbol="COST", timeframe="5m", side="buy", now=NOW
        )
        == []
    )
