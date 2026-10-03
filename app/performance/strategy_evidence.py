"""Phase 2 evidence gate: which strategies have earned live paper entries.

A per-symbol backtest is noisy (a 5m strategy sees ~20-30 trades per symbol),
so the verdict pools each strategy+timeframe across the whole universe: the
newest walk-forward OOS row per symbol, summed. A strategy passes when it has

* at least ``strategy_evidence_min_trades`` OOS trades (default 40),
* positive expectancy per trade after costs (the engine applies the cost model),
* at least ``strategy_evidence_min_holdout_trades`` trades in the sealed
  holdout windows with positive holdout expectancy.

Verdicts are recomputed in maintenance and cached in ``runtime_state`` so the
proposal gate reads them cheaply. With ``require_strategy_oos_evidence`` on,
failing strategies stop creating proposals; their candidates are still scanned,
alerted and tracked to target/stop as shadow signals, so a strategy can earn its
way back in.
"""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

from app.utils.time import utc_now

VERDICTS_KEY = "strategy_evidence:verdicts"
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


def compute_verdicts(db: Any, settings: Any, *, now: Any = None) -> dict[str, dict[str, Any]]:
    """Pooled OOS evidence per ``"<strategy>:<timeframe>"``."""

    lookback = int(getattr(settings, "strategy_evidence_lookback_days", 7) or 7)
    min_trades = int(getattr(settings, "strategy_evidence_min_trades", 40) or 40)
    min_holdout = int(getattr(settings, "strategy_evidence_min_holdout_trades", 10) or 10)
    cutoff = ((now or utc_now()) - timedelta(days=lookback)).isoformat()
    with db.connect() as connection:
        rows = connection.execute(_LATEST_OOS_SQL, ("%:walk_forward_oos", cutoff)).fetchall()

    pooled: dict[str, dict[str, float]] = {}
    for row in rows:
        try:
            metrics = json.loads(row["metrics_json"] or "{}")
        except (TypeError, ValueError):
            continue
        key = f"{row['strategy_name']}:{_timeframe(row['file_path'])}"
        bucket = pooled.setdefault(
            key, {"symbols": 0, "trades": 0, "pnl": 0.0, "holdout_trades": 0, "holdout_pnl": 0.0}
        )
        trades = int(metrics.get("number_of_trades", 0) or 0)
        holdout_trades = int(metrics.get("holdout_trades", 0) or 0)
        bucket["symbols"] += 1
        bucket["trades"] += trades
        bucket["pnl"] += float(metrics.get("expectancy_usd", 0.0) or 0.0) * trades
        bucket["holdout_trades"] += holdout_trades
        bucket["holdout_pnl"] += (
            float(metrics.get("holdout_expectancy_usd", 0.0) or 0.0) * holdout_trades
        )

    verdicts: dict[str, dict[str, Any]] = {}
    for key, b in pooled.items():
        expectancy = b["pnl"] / b["trades"] if b["trades"] else 0.0
        holdout_expectancy = b["holdout_pnl"] / b["holdout_trades"] if b["holdout_trades"] else 0.0
        reasons = []
        if b["trades"] < min_trades:
            reasons.append("too_few_oos_trades")
        if expectancy <= 0:
            reasons.append("oos_expectancy_not_positive")
        if b["holdout_trades"] < min_holdout:
            reasons.append("too_few_holdout_trades")
        elif holdout_expectancy <= 0:
            reasons.append("holdout_expectancy_not_positive")
        verdicts[key] = {
            "passed": not reasons,
            "reasons": reasons,
            "symbols": int(b["symbols"]),
            "oos_trades": int(b["trades"]),
            "oos_expectancy_usd": round(expectancy, 4),
            "holdout_trades": int(b["holdout_trades"]),
            "holdout_expectancy_usd": round(holdout_expectancy, 4),
        }
    return verdicts


def refresh_verdicts(
    db: Any, settings: Any, runtime_state: Any, run_logs: Any | None = None
) -> dict[str, dict[str, Any]]:
    verdicts = compute_verdicts(db, settings)
    runtime_state.set(
        VERDICTS_KEY, json.dumps({"computed_at": utc_now().isoformat(), "verdicts": verdicts})
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


def evidence_blocker(
    settings: Any, runtime_state: Any, *, strategy: str, timeframe: str | None
) -> str | None:
    """``"strategy_lacks_oos_evidence"`` when enforcement is on and the strategy fails."""

    if not bool(getattr(settings, "require_strategy_oos_evidence", False)) or runtime_state is None:
        return None
    try:
        cached = json.loads(runtime_state.get(VERDICTS_KEY) or "{}")
    except (TypeError, ValueError):
        cached = {}
    verdict = (cached.get("verdicts") or {}).get(f"{strategy}:{str(timeframe or '').lower()}")
    if verdict is None or not verdict.get("passed"):
        return "strategy_lacks_oos_evidence"
    return None
