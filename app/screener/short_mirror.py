"""The screener's short side (paper shorts, operator 2026-11-16; production finding 11-16).

With ``paper_short_entries_enabled`` on, a bracketed ``entry_short`` signal must meet every
screener quality gate as the mirror image of a long: it passes only where its mirror-image long
would. Audit (docs/notes/paper_shorts_screener.md):

* already mirrored before this module (verified by tests): every market-context score the
  filters read from ``MarketIntelligenceService`` (it reads ``signal_role``: market regime, trend,
  breadth, sector and benchmark strength, timeframe alignment, relative strength negated, the
  momentum state and the extension), the context's momentum and regime alignment, the accuracy
  profile's levels, VWAP, EMA stack and MACD, the trade plan and the risk:reward. Trend strength,
  efficiency, ATR, relative volume, spread and liquidity are direction-free magnitudes.
* mirrored here, only for a short and only while the switch is on (``mirrors_short``):
  - the RSI bands of the accuracy profile and of the indicator confluence: RSI is exactly
    ``100 - RSI`` under a price reflection, so a long's 48-72 constructive band, its > 78
    exhaustion and the confluence's 52-68 band mirror to 28-52, < 22 and 32-48 (a short used
    30-52, < 24 and 35-48: stricter than its mirror);
  - the per-symbol backtest the snapshot, the filters and the ranking read: the short's OWN
    rows (``<strategy>:short``, the short research pass's per-symbol walk-forward rows), never
    the long strategy's; a symbol with none is treated as a long with no backtest;
  - the paper near-miss promotion (app/screener/scan_promotion.py), which promotes a long that
    fails only soft gates and was closed to every short: a short is promoted on the same rules,
    with its bracket mirrored (target < entry < stop).
* left long-only on purpose: the supervised weak-valid promotion (it waits for a person's
  approval; a paper short is automatic only and can never be approved by hand) and the
  long-only enhanced strategies (they emit no short; making them do so would be a new signal).

Off: nothing here changes anything, and a long is never touched.
"""

from __future__ import annotations

from typing import Any

SHORT_BACKTEST_NAME_SUFFIX = ":short"  # the short research pass's per-symbol row names
# RSI bands: a long's band and its exact mirror (RSI -> 100 - RSI under a price reflection).
LONG_CONSTRUCTIVE_RSI = (48.0, 72.0)
SHORT_CONSTRUCTIVE_RSI = (100.0 - 72.0, 100.0 - 48.0)  # 28-52
LONG_EXHAUSTED_RSI = 78.0  # above
SHORT_EXHAUSTED_RSI = 100.0 - 78.0  # below: 22


def is_short_signal(signal: Any) -> bool:
    """A bracketed short setup: ``signal_role`` "entry_short" (watches and exits are not)."""

    metadata = getattr(signal, "metadata", None) or {}
    return str(metadata.get("signal_role") or "entry_long").lower() == "entry_short"


def mirrors_short(settings: Any, signal: Any) -> bool:
    """Whether this signal is a short the screener judges as a mirrored long (switch on)."""

    if settings is None or not is_short_signal(signal):
        return False
    from app.risk.short_side import paper_shorts_enabled

    return paper_shorts_enabled(settings)


def backtest_strategy_name(settings: Any, signal: Any, strategy_name: str) -> str:
    """The strategy name whose per-symbol backtest judges ``signal``: a mirrored short's own
    short rows, else ``strategy_name`` unchanged."""

    if signal is not None and mirrors_short(settings, signal):
        return f"{strategy_name}{SHORT_BACKTEST_NAME_SUFFIX}"
    return strategy_name
