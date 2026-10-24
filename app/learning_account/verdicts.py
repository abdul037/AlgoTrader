"""Learning verdicts: which refused strategy:timeframe keys the learning account may trade.

The main gate (app/performance/strategy_evidence.py) prices every row with eToro's fees. A key
the main path refuses ONLY for its evidence (``strategy_lacks_oos_evidence``) is

* ``fee_band`` when the same pooled evidence passes WITHOUT eToro's fees (the same thresholds:
  OOS trades, "> 0" and any R bar, holdout trades and "> 0"), and
* ``lane`` when it fails both, with at least ``LANE_MIN_OOS_TRADES`` pooled OOS trades and a
  fee-free pooled OOS expectancy of at least ``LANE_MIN_EXPECTANCY_R``.

Fee-free numbers come from each row's additive ``etoro_fees_usd`` / ``holdout_etoro_fees_usd``
(app/backtesting/batch.py): fee-free P&L = ``expectancy_usd`` x trades + fees, pooled across
rows like the main pool (a row written before those fields counts no fees: never looser).
A 1m/5m/10m/15m key is judged on its Stage 1 Alpaca cost profile (``cost_profiles.alpaca``)
instead, so a minute key that passes with eToro's fees but has ``paper_passed`` false (its
stress profile fails) is classified on the Alpaca profile (e.g. ``fee_band``).

Option B: while its paper OOS bar is on, a key the main gate admits through it
(``paper_oos_passed``) is the main path's (``passes_the_paper_oos_bar``); ``classify`` mirrors
the gate exactly (``evidence_blocker``; crypto is judged on ``passed`` alone).

Coupling (stated): the pooling and judging are strategy_evidence.py's own helpers (``_pool``,
``_empty_pool``, ``_judge``, ``_paper_full``, ``_oos_bar``), imported, never copied, and that
file is not edited; tests/test_learning_account_verdicts.py pins a learning verdict for fixed
rows, so a main-side change to them shows up there. The verdicts live under their own key
(``LEARNING_VERDICTS_KEY``), computed on the learning thread; the main verdict cache is never
written here.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from app.utils.time import utc_now

LANE_MIN_OOS_TRADES = 30
LANE_MIN_EXPECTANCY_R = -0.05
FEE_BAND, LANE = "fee_band", "lane"
FEE_FREE_BASIS, ALPACA_BASIS = "fee_free", "alpaca"
VERDICTS_MAX_AGE_HOURS = 24.0
_SQL = """
    SELECT b.strategy_name, b.file_path, b.metrics_json
    FROM backtests b
    JOIN (
        SELECT strategy_name, file_path, MAX(completed_at) AS completed_at
        FROM backtests
        WHERE file_path LIKE ? AND completed_at >= ? AND strategy_name IN ({marks})
        GROUP BY strategy_name, file_path
    ) latest
      ON b.strategy_name = latest.strategy_name
     AND b.file_path = latest.file_path
     AND b.completed_at = latest.completed_at
"""


def _thresholds(settings: Any) -> tuple[int, int, float]:
    return (
        int(getattr(settings, "strategy_evidence_min_trades", 40) or 40),
        int(getattr(settings, "strategy_evidence_min_holdout_trades", 10) or 10),
        float(getattr(settings, "strategy_evidence_min_expectancy_r", 0.0) or 0.0),
    )


def _number(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def fee_free_metrics(metrics: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    """The row's pooled fields with eToro's fees added back, and whether it recorded them."""

    from app.backtesting.cost_model import ETORO_FEES_FIELD, HOLDOUT_ETORO_FEES_FIELD

    trades = int(metrics.get("number_of_trades", 0) or 0)
    holdout = int(metrics.get("holdout_trades", 0) or 0)
    pnl = _number(metrics.get("expectancy_usd")) * trades + _number(metrics.get(ETORO_FEES_FIELD))
    holdout_pnl = _number(metrics.get("holdout_expectancy_usd")) * holdout + _number(
        metrics.get(HOLDOUT_ETORO_FEES_FIELD)
    )
    adjusted = {
        "number_of_trades": trades,
        "expectancy_usd": pnl / trades if trades else 0.0,
        "holdout_trades": holdout,
        "holdout_expectancy_usd": holdout_pnl / holdout if holdout else 0.0,
    }
    return adjusted, ETORO_FEES_FIELD in metrics


def compute_learning_verdicts(
    db: Any, settings: Any, strategies: set[str] | frozenset[str], *, now: Any = None
) -> dict[str, dict[str, Any]]:
    """Pooled fee-free (or, for minute keys, Alpaca-profile) evidence per key of ``strategies``.

    An empty set reads nothing (Postgres rejects ``IN ()``)."""

    from app.backtesting.intraday_plan import is_minute_bar
    from app.backtesting.venue_costs import COST_PROFILES_FIELD
    from app.performance.strategy_evidence import (
        BACKTEST_RISK_USD,
        _empty_pool,
        _judge,
        _pool,
        _timeframe,
    )

    names = sorted({str(name) for name in strategies if str(name)})
    if not names:
        return {}
    lookback = int(getattr(settings, "strategy_evidence_lookback_days", 7) or 7)
    cutoff = ((now or utc_now()) - timedelta(days=lookback)).isoformat()
    sql = _SQL.format(marks=", ".join("?" for _ in names))
    with db.connect() as connection:
        rows = connection.execute(sql, ("%:walk_forward_oos", cutoff, *names)).fetchall()

    pools: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            metrics = json.loads(row["metrics_json"] or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(metrics, dict):
            continue
        timeframe = _timeframe(row["file_path"])
        key = f"{row['strategy_name']}:{timeframe}"
        bucket = pools.setdefault(key, {"pool": _empty_pool(), "rows": 0, "without_fees": 0})
        bucket["rows"] += 1
        if is_minute_bar(timeframe):
            profiles = metrics.get(COST_PROFILES_FIELD)
            profile = profiles.get(ALPACA_BASIS) if isinstance(profiles, dict) else None
            if isinstance(profile, dict):
                _pool(bucket["pool"], profile)
            continue
        adjusted, recorded = fee_free_metrics(metrics)
        _pool(bucket["pool"], adjusted)
        bucket["without_fees"] += 0 if recorded else 1

    thresholds = _thresholds(settings)
    verdicts: dict[str, dict[str, Any]] = {}
    for key, bucket in pools.items():
        pool = bucket["pool"]
        minute = is_minute_bar(key.rsplit(":", 1)[-1])
        reasons, expectancy, holdout_expectancy = _judge(pool, *thresholds)
        if minute and not pool["symbols"]:
            reasons = ["no_cost_profile_rows", *reasons]
        verdicts[key] = {
            "basis": ALPACA_BASIS if minute else FEE_FREE_BASIS,
            "passed": not reasons,
            "reasons": reasons,
            "symbols": int(pool["symbols"]),
            "oos_trades": int(pool["trades"]),
            "oos_expectancy_usd": round(expectancy, 4),
            "oos_expectancy_r": round(expectancy / BACKTEST_RISK_USD, 4),
            "holdout_trades": int(pool["holdout_trades"]),
            "holdout_expectancy_usd": round(holdout_expectancy, 4),
            "rows": int(bucket["rows"]),
            "rows_without_fees": int(bucket["without_fees"]),
        }
    return verdicts


def classify(
    settings: Any, verdict: Any, learning: Any, timeframe: Any, *, crypto: bool
) -> tuple[str | None, str]:
    """``(FEE_BAND | LANE, reason)``, or ``(None, why not)``, for a key the main path refused.

    ``verdict`` is the main gate's verdict for the key, ``learning`` this module's. The main
    gate is mirrored exactly: in paper (not crypto) a key passes on ``passed`` (and a minute
    key's ``paper_passed``) or, while option B's bar is on, ``paper_oos_passed``; crypto is
    judged on ``passed`` alone."""

    from app.performance.strategy_evidence import _oos_bar, _paper_full

    if not isinstance(verdict, dict):
        return None, "no_evidence_verdict"
    if crypto:
        if bool(verdict.get("passed")):
            return None, "passes_the_main_gate"
    else:
        if _paper_full(verdict, timeframe):
            return None, "passes_the_main_gate"
        if _oos_bar(settings, verdict):
            return None, "passes_the_paper_oos_bar"
    if not isinstance(learning, dict):
        return None, "no_learning_verdict"
    if bool(learning.get("passed")):
        return FEE_BAND, f"passes_{learning.get('basis') or FEE_FREE_BASIS}"
    trades = int(_number(learning.get("oos_trades")))
    expectancy_r = _number(learning.get("oos_expectancy_r"))
    if trades < LANE_MIN_OOS_TRADES:
        return None, "lane_too_few_oos_trades"
    if expectancy_r < LANE_MIN_EXPECTANCY_R:
        return None, "lane_expectancy_below_floor"
    return LANE, "near_miss"


def rank_key(kind: str, learning: dict[str, Any], score: Any) -> tuple[int, float, float]:
    """Near-miss first: fee-band before lane, then the higher pooled expectancy (on the key's
    basis), then the higher scan score."""

    return (0 if kind == FEE_BAND else 1, -_number(learning.get("oos_expectancy_r")), -_number(score))


def stale(payload: Any, *, now: Any = None) -> bool:
    """True when the stored learning verdicts are missing a stamp or older than a day."""

    from datetime import datetime

    try:
        computed = datetime.fromisoformat(str(payload.get("computed_at")))
    except (AttributeError, TypeError, ValueError):
        return True
    if computed.tzinfo is None:
        return True
    return (now or utc_now()) - computed > timedelta(hours=VERDICTS_MAX_AGE_HOURS)
