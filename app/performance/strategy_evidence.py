"""Phase 2 evidence gate: which strategies have earned live paper entries.

A per-symbol backtest is noisy (a 5m strategy sees ~20-30 trades per symbol),
so the verdict pools each strategy+timeframe across the whole universe: the
newest walk-forward OOS row per symbol, summed. A strategy passes when it has

* at least ``strategy_evidence_min_trades`` OOS trades (default 40),
* positive expectancy per trade after costs (the engine applies the cost model), and at
  least ``strategy_evidence_min_expectancy_r`` R when set (1 R = the batch backtest's
  $100 risk per trade, 1% of $10,000) -- a "> 0" bar lets breakeven strategies through,
* at least ``strategy_evidence_min_holdout_trades`` trades in the sealed
  holdout windows with positive holdout expectancy.

Verdicts are recomputed in maintenance and cached in ``runtime_state`` so the
proposal gate reads them cheaply. With ``require_strategy_oos_evidence`` on,
failing strategies stop creating proposals; their candidates are still scanned,
alerted and tracked to target/stop as shadow signals, so a strategy can earn its
way back in.

Pass streaks (operator 2026-10-18, for the eToro live copy only): the cached payload also
holds ``passing_since`` -- for every key that passes, when its current unbroken run of
passing refreshes began. A refresh where the key fails (or is missing) ends the run; a gap
of more than ``EVIDENCE_STREAK_MAX_GAP_HOURS`` since the previous refresh (no one checked in
between) restarts every run. The previous payload lives in ``runtime_state`` (the database),
so a deploy or restart keeps the runs. The verdicts, their fields and the paper gate
(``evidence_blocker``) are unchanged; only ``passing_hours`` (the live copy, the deep-scan
hint) reads the streaks.

Venue costs (operator 2026-10-20): every field above is priced with eToro's fees, and every
eToro use reads only those. A 1m/5m/10m/15m key also carries ``cost_profiles`` (the same pools
under Alpaca and stress costs, app/backtesting/venue_costs.py) and ``paper_passed`` /
``paper_reasons``. The main paper account's gate (``evidence_blocker(paper=True)``) passes
such an equity strategy only when the fee-inclusive verdict passes AND both profiles clear the
same bars, so the profiles only ever tighten it: a fee-band strategy (passing without eToro's
fees but not with them) waits for a separate paper account (operator decision 3, 2026-10-20).
1h/1d/1w keys are unchanged.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from typing import Any

from app.utils.time import utc_now

VERDICTS_KEY = "strategy_evidence:verdicts"
BACKTEST_RISK_USD = 100.0  # batch backtests risk 1% of a $10,000 account per trade
_LATEST_OOS_SQL = """
    SELECT b.strategy_name, b.file_path, b.metrics_json
    FROM backtests b
    JOIN (
        SELECT strategy_name, file_path, MAX(completed_at) AS completed_at
        FROM backtests
        WHERE file_path LIKE ? AND completed_at >= ?
        GROUP BY strategy_name, file_path
    ) latest
      ON b.strategy_name = latest.strategy_name
     AND b.file_path = latest.file_path
     AND b.completed_at = latest.completed_at
"""


def _timeframe(file_path: str) -> str:
    parts = str(file_path).split(":")
    return parts[1].lower() if len(parts) >= 3 else ""


def compute_verdicts(
    db: Any, settings: Any, *, now: Any = None, pattern: str = "%:walk_forward_oos"
) -> dict[str, dict[str, Any]]:
    """Pooled OOS evidence per ``"<strategy>:<timeframe>"``.

    ``pattern`` is a bound LIKE parameter (never inlined: under Postgres a literal ``:name``
    in the SQL would parse as a bind). The default matches only the long rows.
    """

    from app.backtesting.intraday_plan import is_minute_bar
    from app.backtesting.venue_costs import COST_PROFILES_FIELD, VENUE_PROFILE_NAMES

    lookback = int(getattr(settings, "strategy_evidence_lookback_days", 7) or 7)
    thresholds = (
        int(getattr(settings, "strategy_evidence_min_trades", 40) or 40),
        int(getattr(settings, "strategy_evidence_min_holdout_trades", 10) or 10),
        float(getattr(settings, "strategy_evidence_min_expectancy_r", 0.0) or 0.0),
    )
    cutoff = ((now or utc_now()) - timedelta(days=lookback)).isoformat()
    with db.connect() as connection:
        rows = connection.execute(_LATEST_OOS_SQL, (pattern, cutoff)).fetchall()

    pooled: dict[str, dict[str, Any]] = {}
    for row in rows:
        try:
            metrics = json.loads(row["metrics_json"] or "{}")
        except (TypeError, ValueError):
            continue
        key = f"{row['strategy_name']}:{_timeframe(row['file_path'])}"
        bucket = pooled.setdefault(key, {**_empty_pool(), "profiles": {}})
        _pool(bucket, metrics)
        profiles = metrics.get(COST_PROFILES_FIELD)
        for name, profile in (profiles if isinstance(profiles, dict) else {}).items():
            if isinstance(profile, dict):
                _pool(bucket["profiles"].setdefault(name, _empty_pool()), profile)

    verdicts: dict[str, dict[str, Any]] = {}
    for key, b in pooled.items():
        reasons, expectancy, holdout_expectancy = _judge(b, *thresholds)
        verdicts[key] = {
            "passed": not reasons,
            "reasons": reasons,
            "symbols": int(b["symbols"]),
            "oos_trades": int(b["trades"]),
            "oos_expectancy_usd": round(expectancy, 4),
            "oos_expectancy_r": round(expectancy / BACKTEST_RISK_USD, 4),
            "holdout_trades": int(b["holdout_trades"]),
            "holdout_expectancy_usd": round(holdout_expectancy, 4),
        }
        if is_minute_bar(_timeframe_of_key(key)):
            verdicts[key].update(_paper_verdict(b["profiles"], VENUE_PROFILE_NAMES, thresholds))
    return verdicts


def _empty_pool() -> dict[str, Any]:
    return {"symbols": 0, "trades": 0, "pnl": 0.0, "holdout_trades": 0, "holdout_pnl": 0.0}


def _pool(bucket: dict[str, Any], metrics: dict[str, Any]) -> None:
    trades = int(metrics.get("number_of_trades", 0) or 0)
    holdout_trades = int(metrics.get("holdout_trades", 0) or 0)
    bucket["symbols"] += 1
    bucket["trades"] += trades
    bucket["pnl"] += float(metrics.get("expectancy_usd", 0.0) or 0.0) * trades
    bucket["holdout_trades"] += holdout_trades
    bucket["holdout_pnl"] += float(metrics.get("holdout_expectancy_usd", 0.0) or 0.0) * holdout_trades


def _judge(
    b: dict[str, Any], min_trades: int, min_holdout: int, min_r: float
) -> tuple[list[str], float, float]:
    """A pool's failing reasons (none = pass) and its OOS / holdout expectancy in $."""

    expectancy = b["pnl"] / b["trades"] if b["trades"] else 0.0
    holdout_expectancy = b["holdout_pnl"] / b["holdout_trades"] if b["holdout_trades"] else 0.0
    reasons = []
    if b["trades"] < min_trades:
        reasons.append("too_few_oos_trades")
    if expectancy <= 0:
        reasons.append("oos_expectancy_not_positive")
    elif min_r > 0 and expectancy < min_r * BACKTEST_RISK_USD:
        reasons.append(f"oos_expectancy_below_{min_r:g}R")
    if b["holdout_trades"] < min_holdout:
        reasons.append("too_few_holdout_trades")
    elif holdout_expectancy <= 0:
        reasons.append("holdout_expectancy_not_positive")
    return reasons, expectancy, holdout_expectancy


# Minute-bar keys only (operator 2026-10-20): true when the OOS and holdout pools are positive
# under both venue cost profiles (app/backtesting/venue_costs.py), with the same trade counts
# and R bar as ``passed``. The main paper gate asks for it on top of ``passed``, never instead.
PAPER_PASSED_FIELD = "paper_passed"


def _paper_verdict(
    profiles: dict[str, dict[str, Any]], names: tuple[str, ...], thresholds: tuple[int, int, float]
) -> dict[str, Any]:
    reasons: list[str] = []
    numbers: dict[str, dict[str, Any]] = {}
    for name in names:
        pool = profiles.get(name)
        if pool is None:  # rows written before the profiles existed, or crypto only
            reasons.append(f"{name}:no_cost_profile_rows")
            continue
        failing, expectancy, holdout_expectancy = _judge(pool, *thresholds)
        reasons += [f"{name}:{reason}" for reason in failing]
        numbers[name] = {
            "symbols": int(pool["symbols"]),
            "oos_trades": int(pool["trades"]),
            "oos_expectancy_usd": round(expectancy, 4),
            "holdout_trades": int(pool["holdout_trades"]),
            "holdout_expectancy_usd": round(holdout_expectancy, 4),
        }
    return {PAPER_PASSED_FIELD: not reasons, "paper_reasons": reasons, "cost_profiles": numbers}


def _timeframe_of_key(key: str) -> str:
    return key.rsplit(":", 1)[-1]


def refresh_verdicts(
    db: Any, settings: Any, runtime_state: Any, run_logs: Any | None = None
) -> dict[str, dict[str, Any]]:
    verdicts = compute_verdicts(db, settings)
    now = utc_now()
    try:  # the previous refresh carries the streaks over; unreadable means they restart
        previous = json.loads(runtime_state.get(VERDICTS_KEY) or "{}")
    except Exception:  # noqa: BLE001 - never fail the refresh the paper gate depends on
        previous = {}
    runtime_state.set(
        VERDICTS_KEY,
        json.dumps(
            {
                "computed_at": now.isoformat(),
                "verdicts": verdicts,
                PASSING_SINCE_FIELD: _passing_since(previous, verdicts, now),
            }
        ),
    )
    if run_logs is not None:
        run_logs.log(
            "strategy_evidence_refreshed",
            {
                "passed": sorted(k for k, v in verdicts.items() if v["passed"]),
                "evaluated": len(verdicts),
                "enforced": bool(getattr(settings, "require_strategy_oos_evidence", False)),
            },
        )
    return verdicts


PASSING_SINCE_FIELD = "passing_since"
# Refreshes run every 30 min; with no refresh for longer than this nobody can vouch for the
# time in between, so every streak restarts (and the live copy reads the history as stale).
EVIDENCE_STREAK_MAX_GAP_HOURS = 2.0
_CLOCK_SKEW = timedelta(minutes=5)  # a payload stamped slightly "ahead" is still continuous


def _passing_since(previous: Any, verdicts: dict[str, Any], now: datetime) -> dict[str, str]:
    """When each passing key's unbroken run began, carried over from ``previous``."""

    previous = previous if isinstance(previous, dict) else {}
    since_map = previous.get(PASSING_SINCE_FIELD)
    prior_verdicts = previous.get("verdicts")
    last = _parse_time(previous.get("computed_at"))
    continuous = (
        isinstance(since_map, dict)
        and isinstance(prior_verdicts, dict)
        and last is not None
        and -_CLOCK_SKEW <= now - last <= timedelta(hours=EVIDENCE_STREAK_MAX_GAP_HOURS)
    )
    streaks: dict[str, str] = {}
    for key, verdict in verdicts.items():
        if not verdict.get("passed"):
            continue  # a failed refresh ends the run
        start = None
        prior = prior_verdicts.get(key) if continuous else None
        if isinstance(prior, dict) and prior.get("passed"):
            start = _parse_time(since_map.get(key))
        streaks[key] = start.isoformat() if start is not None and start <= now else now.isoformat()
    return streaks


def passing_hours(
    cached: Any, *, strategy: str, timeframe: str | None, now: datetime | None = None
) -> float | None:
    """Hours ``<strategy>:<timeframe>`` has passed every refresh without a break.

    The run ends at the last refresh (``computed_at``, never later than ``now``): nobody has
    checked the evidence since, so that time is not counted. ``cached`` is the
    ``VERDICTS_KEY`` value (JSON text or the parsed dict). 0.0 when the key does not pass
    now; None when the history cannot vouch for it: no payload, one written before streaks
    existed, a refresh older than ``EVIDENCE_STREAK_MAX_GAP_HOURS``, or a passing key without
    a readable start.
    """

    try:
        payload = cached if isinstance(cached, dict) else json.loads(cached or "{}")
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    now = now or utc_now()
    computed = _parse_time(payload.get("computed_at"))
    since_map = payload.get(PASSING_SINCE_FIELD)
    verdicts = payload.get("verdicts")
    if computed is None or not isinstance(since_map, dict) or not isinstance(verdicts, dict):
        return None
    if not -_CLOCK_SKEW <= now - computed <= timedelta(hours=EVIDENCE_STREAK_MAX_GAP_HOURS):
        return None
    key = f"{strategy}:{str(timeframe or '').lower()}"
    verdict = verdicts.get(key)
    if not isinstance(verdict, dict) or not verdict.get("passed"):
        return 0.0
    since = _parse_time(since_map.get(key))
    if since is None or since > computed + _CLOCK_SKEW:
        return None
    # Review of 1400c62: ending the run at ``now`` counted up to 2 h of unchecked time.
    verified_until = min(now, computed)
    return max((verified_until - since).total_seconds() / 3600.0, 0.0)


def _parse_time(value: Any) -> datetime | None:
    """An aware ISO timestamp, else None (a naive one cannot be compared with UTC now)."""

    try:
        parsed = datetime.fromisoformat(str(value)) if value else None
    except (TypeError, ValueError):
        return None
    return parsed if parsed is not None and parsed.tzinfo is not None else None


# Short research (app/backtesting/short_research.py): rows are named "<strategy>:short" and
# end ":walk_forward_oos_short", so the long pattern never matches them. Their verdicts keep
# the native "<strategy>:short:<tf>" keys under their own key. Research only: evidence_blocker,
# VERDICTS_KEY and the trading path never read it; only the live-scorecard report does.
SHORT_OOS_PATTERN = "%:walk_forward_oos_short"
SHORT_VERDICTS_KEY = "strategy_evidence:short_verdicts"


def refresh_short_verdicts(db: Any, settings: Any, runtime_state: Any) -> dict[str, dict[str, Any]]:
    """Pool the short rows with the long rules (same thresholds, same R) for reporting only."""

    verdicts = compute_verdicts(db, settings, pattern=SHORT_OOS_PATTERN)
    payload = {"computed_at": utc_now().isoformat(), "research_only": True, "verdicts": verdicts}
    runtime_state.set(SHORT_VERDICTS_KEY, json.dumps(payload))
    return verdicts


EDGE_BELOW_BAR = "strategy_edge_below_bar"


def evidence_blocker(
    settings: Any,
    runtime_state: Any,
    *,
    strategy: str,
    timeframe: str | None,
    min_expectancy_r: float = 0.0,
    paper: bool = False,
) -> str | None:
    """``"strategy_lacks_oos_evidence"`` when enforcement is on and the strategy fails.

    ``min_expectancy_r`` > 0 also asks a passing strategy for at least that pooled OOS
    expectancy in R (``EDGE_BELOW_BAR`` otherwise) -- the eToro live copy's own bar.
    ``paper`` (the main Alpaca paper proposal gate only) also asks a 1m/5m/10m/15m strategy
    for ``PAPER_PASSED_FIELD`` (Alpaca and stress costs) on top of the fee-inclusive
    ``passed``, and a verdict without it fails: it only ever tightens. A fee-band strategy
    (passing without eToro's fees but not with them) never trades on the main account
    (operator decision 3, 2026-10-20) until it has its own. Every other caller, the eToro ones
    included, reads ``passed`` alone.
    """

    if not bool(getattr(settings, "require_strategy_oos_evidence", False)) or runtime_state is None:
        return None
    try:
        cached = json.loads(runtime_state.get(VERDICTS_KEY) or "{}")
    except (TypeError, ValueError):
        cached = {}
    verdict = (cached.get("verdicts") or {}).get(f"{strategy}:{str(timeframe or '').lower()}")
    if verdict is None or not verdict.get("passed"):
        return "strategy_lacks_oos_evidence"
    if paper:
        from app.backtesting.intraday_plan import is_minute_bar

        if is_minute_bar(timeframe) and not verdict.get(PAPER_PASSED_FIELD):
            return "strategy_lacks_oos_evidence"
    if _stale(cached.get("computed_at")):  # refreshed every 30 min; a day-old pass is not trusted
        return "strategy_evidence_stale"
    if min_expectancy_r > 0 and not _edge_r(verdict) >= min_expectancy_r:
        return EDGE_BELOW_BAR
    return None


def _edge_r(verdict: dict[str, Any]) -> float:
    """The verdict's pooled OOS expectancy in R; NaN (fails every bar) when unreadable."""

    for value, scale in (
        (verdict.get("oos_expectancy_r"), 1.0),
        (verdict.get("oos_expectancy_usd"), BACKTEST_RISK_USD),
    ):
        if value is None:
            continue
        try:
            number = float(value) / scale
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            return number
    return math.nan


EVIDENCE_MAX_AGE_HOURS = 24.0


def _stale(computed_at: Any) -> bool:
    from datetime import datetime, timedelta

    if not computed_at:
        return False  # older caches had no timestamp; the 30-min refresh adds it
    try:
        return utc_now() - datetime.fromisoformat(str(computed_at)) > timedelta(
            hours=EVIDENCE_MAX_AGE_HOURS
        )
    except (TypeError, ValueError):
        return True
