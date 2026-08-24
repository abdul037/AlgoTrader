# Self-Sustaining Paper-Auto Checklist

The concrete path from the current safe defaults to a bot that scans, proposes,
executes, manages, and exits **paper** trades hands-off — and an honest split
between what is now **code-complete** and what only **live operation** can prove.

Read alongside [`alpaca_paper_to_live_roadmap.md`](alpaca_paper_to_live_roadmap.md)
(the phase plan) and [`rollout_build_plan.md`](rollout_build_plan.md) (built-vs-missing).

> Live-money trading is out of scope here. Everything below keeps
> `ENABLE_REAL_TRADING=false`. Graduating to real capital is a separate,
> signed decision gate in the roadmap.

## What is code-complete (this branch)

The autonomous machinery now exists and is tested (405 passing):

- **Runtime.** A dedicated `SchedulerWorker` owns the cadence (scans, queue
  processing, reconciliation, paper-position refresh) decoupled from Telegram,
  with a liveness heartbeat, per-job exception isolation, and a **watchdog** that
  revives a dead worker on each readiness probe. `GET /health/ready` reports true
  readiness (worker heartbeat + DB) and is the orchestrator healthcheck.
- **Fills.** Alpaca `trade_updates` websocket records fills/exits in real time
  (flag-gated), with the reconciliation sweep as backstop.
- **Signals.** The live path runs the whole strategy catalog and selects the
  best long setup with the screener's full 21-component ranker, against an
  **honest** out-of-sample drawdown metric and a **schedulable** backtester that
  keeps the validation gate populated. Stops use a volatility-aware ATR floor.
- **Risk.** Sector and correlation exposure caps enforce against *existing*
  positions; the daily/weekly loss caps count *open* unrealized losses, not just
  realized. Mandatory stop-loss, dual proposal+execution validation, kill switch,
  and circuit breaker remain in force.

## The graduation sequence

Each step is a config change plus an evidence gate. Do not skip a gate.

### Phase C — Supervised (human approval)

```env
EXECUTION_MODE=paper
PAPER_BROKER=alpaca
ENABLE_REAL_TRADING=false
REQUIRE_APPROVAL=true
PAPER_AUTO_OPERATION_MODE=shadow        # observe only, no auto-exec
BACKGROUND_SCHEDULER_ENABLED=true
SCREENER_SCHEDULER_ENABLED=true
ALPACA_ENABLED=true                     # with paper keys
ALPACA_RECONCILIATION_ENABLED=true
```

Optional but recommended now that they exist:

```env
LIVE_SIGNAL_USE_STRATEGY_CATALOG=true
LIVE_SIGNAL_USE_SCREENER_RANKER=true
BACKTEST_SCHEDULER_ENABLED=true         # populates the validation gate
ALPACA_TRADE_STREAM_ENABLED=true        # real-time fills
```

**Evidence gate (operational — only live sessions produce it):**
- One Alpaca paper order during market hours through a real strategy proposal.
- Re-process the same queue item → no duplicate order (idempotency verified).
- Kill-switch drill: open orders cancel, paper positions close.
- 48 hours with no unhandled exceptions or unexplained queue blocks.
- Rotate any previously exposed broker/database credentials.

### Phase D2 — Self-monitoring on

Turn on the portfolio controls and confirm they bite:

```env
INSTITUTIONAL_PORTFOLIO_CONTROLS_ENABLED=true
LOSS_LIMIT_INCLUDES_UNREALIZED=true
```

**Evidence gate:** a simulated losing streak auto-deactivates a strategy; a
drawdown breach pauses trading via the circuit breaker; blacklisted symbols are
excluded; concentration caps reject an over-exposed order.

### Phase D3 — Unattended paper-auto (no taps)

Only after the Phase C evidence gate and ≥ `PAPER_AUTO_MIN_CLEAN_SUPERVISED_LIFECYCLES`
clean supervised lifecycles:

```env
PAPER_AUTO_OPERATION_MODE=unattended
AUTO_PROPOSE_ENABLED=true
PAPER_AUTO_APPROVE_PROPOSALS=true
AUTO_EXECUTION_WORKER_ENABLED=true
PAPER_AUTO_APPROVAL_TIER=tier2_strict_valid   # or tier3 with per-strategy evidence
# Tightened auto defaults:
MAX_TRADES_PER_DAY=3
MAX_OPEN_POSITIONS=2
MAX_DAILY_LOSS_USD=25
MAX_RISK_PER_TRADE_PCT=0.25
```

The auto path (`app/automation/unattended.py`) still enforces ~20
`candidate_blockers` (regular hours, Alpaca account verified, backtest-validated,
score threshold, brackets present, institutional readiness, …). At this point no
approval taps are required and every auto order is logged and reconcilable.

**Evidence gate:** paper auto-execution creates → approves → queues → submits
with no taps; every order stays idempotent and reconcilable; the Telegram
notification stream is complete enough to monitor without `/docs`.

### Phase E — VPS validation

Deploy on a persistent host (Docker Compose or Railway) with Postgres, backups,
monitoring, and a real HTTPS Telegram webhook. Run **4 weeks** of paper-auto,
then review against the roadmap's 8-criteria decision gate.

## What code cannot finish (you must supply)

These are the load-bearing gates that no code change can satisfy — they need
real market sessions, real credentials, and human review:

1. Rotate previously exposed broker/database credentials.
2. The clean 48-hour observation and the supervised Alpaca sessions.
3. Accumulated clean unattended lifecycles with no safety blockers.
4. A licensed point-in-time, corporate-action-aware research dataset, validated.
5. At least one strategy audit that passes every promotion threshold
   (deflated + rolling Sharpe, after-cost expectancy, honest drawdown).
6. Managed monitoring, independent off-site backups, and a restore drill.
7. Signed micro-live and legal/compliance gates before any real-money or
   external-capital use.

`GET /institutional/readiness` remains the source of truth for unresolved gates.

## Bottom line

The bot is **code-ready to run hands-off in paper** once you enable the flags in
Phases C→D3 above and it clears each evidence gate. It is **not** — and must not
be — flipped straight to unattended or live without those live-session gates. The
remaining work is operational validation, not missing code.
