# Alpaca Paper-to-Live Roadmap After Smoke Test

## Summary

- Current state: Telegram to FastAPI to proposal/queue to Alpaca Paper routing is proven. The smoke order reached Alpaca.
- Next goal: move from manual smoke testing to approved paper trading, then monitored paper automation, then VPS-hosted validation, then live micro-size only if the decision gate passes.
- Telegram remains the primary control surface. `/docs` is for admin and recovery. A frontend is not required before paper automation.
- The trading backend must run on a VPS or persistent container host. Supabase can be used for managed Postgres in Phase D1, but not as the trading bot runtime.

## Phase Plan

| Week | Phase | Goal | Operating mode | Exit criteria |
|---:|---|---|---|---|
| 2 | Phase C validation + 48h observation | Bot runs Alpaca paper with human approval. Collect operational data. | `paper`, Alpaca, manual approval, no auto-execute | 1 market-hours paper fill, idempotency verified, kill switch drill passed, 48h logs clean |
| 3-4 | Phase D1 | Postgres migration, Grafana dashboards, structured logs. Bot still trades only after approval. | `paper`, manual approval | Postgres migration tested, dashboards live, alerts working, backup/restore verified |
| 5 | Phase D2 | Self-monitoring: auto-deactivation, Bayesian allocator, drawdown circuit breaker, blacklist. Mandatory before D3. | `paper`, manual approval | Bad strategies can deactivate, circuit breaker pauses trading, blacklist blocks symbols, Telegram status commands work |
| 6 | Phase D3 | Paper auto-execution turns on. Telegram becomes notification/control surface. | `paper`, auto-propose, paper auto-approval, auto-execute | No approval taps required, risk defaults tightened, every auto order logged/reconcilable |
| 7+ | Phase E | Production VPS deployment and 4-week paper-auto validation. | VPS-hosted `paper-auto` | 4 weeks stable, monitored, backed up, no unresolved critical incidents |
| 11+ | Decision gate | If all 8 criteria pass, start live micro-size with manual approval temporarily restored. | `live`, micro-size, manual approval | All criteria green; otherwise remain paper and fix gaps |

## Phase C Validation

Keep safe configuration:

```env
EXECUTION_MODE=paper
PAPER_BROKER=alpaca
ENABLE_REAL_TRADING=false
REQUIRE_APPROVAL=true
AUTO_PROPOSE_ENABLED=false
AUTO_EXECUTE_AFTER_APPROVAL=false
```

Required checks:

- Execute one Alpaca paper order during market hours through a real strategy proposal.
- Re-process the same queue item and verify no duplicate Alpaca order appears.
- Run the kill switch drill and confirm open orders are canceled and paper positions close.
- Observe at least 48 hours with no unhandled exceptions or unexplained queue blocks.
- Reserve `/paper_smoke_run` for broker-routing checks only. It is not a strategy-approved trade.

## Phase D1: Observability And Postgres

Implementation targets:

- Migrate persistence from SQLite to Postgres. Default production target is Supabase Postgres; local tests should use Docker Postgres or an isolated test database.
- Add structured JSON logs with `proposal_id`, `queue_id`, `execution_id`, `client_order_id`, `broker_order_id`, `strategy`, `symbol`, and `event_type`.
- Add a Prometheus-style `/metrics` endpoint and Grafana dashboards for health, scheduler, proposals, queue, executions, Alpaca errors, PnL, drawdown, and data freshness.

Acceptance checks:

- SQLite snapshot migrates to Postgres with proposals, queue items, executions, run logs, and runtime settings intact.
- Bot restart does not create duplicate orders or lose queue/execution state.
- Grafana dashboards show fresh health, queue, Alpaca, PnL, and data freshness metrics.
- Backup and restore are tested before moving to D2.

## Phase D2: Self-Monitoring

Implementation targets:

- Add strategy health monitoring, instrument blacklist, Bayesian allocation weights, and drawdown circuit breakers.
- Add Telegram commands: `/strategy_status`, `/allocator_status`, `/blacklist`, `/circuit_status`, and `/reactivate_strategy`.
- Auto-deactivation must block proposal creation for failed strategies without deleting historical records.

Acceptance checks:

- Simulated losing strategy outcomes trigger auto-deactivation.
- Simulated drawdown breach pauses trading through the circuit breaker.
- Blacklisted symbols are excluded from scans/proposals.
- Telegram explains exactly why a strategy, symbol, or execution path is blocked.

## Phase D3: Paper Auto-Execution

Implementation targets:

- Add explicit paper-only auto-approval, for example `PAPER_AUTO_APPROVE_PROPOSALS=true`, instead of weakening global approval safety.
- Enable `AUTO_PROPOSE_ENABLED=true`, `SCREENER_SCHEDULER_ENABLED=true`, and `AUTO_EXECUTE_AFTER_APPROVAL=true`, while keeping `ENABLE_REAL_TRADING=false`.
- Tighten paper-auto defaults:
  - `MAX_TRADES_PER_DAY=3`
  - `MAX_OPEN_POSITIONS=2`
  - `MAX_DAILY_LOSS_USD=25`
  - `DEFAULT_TRADE_AMOUNT_USD=100`
  - `MAX_RISK_PER_TRADE_PCT=0.25`
- Telegram sends notifications for proposed, approved, executed, blocked, deactivated, blacklist, and circuit-breaker events.

Acceptance checks:

- Paper auto-execution creates, approves, queues, and submits without approval taps.
- Every auto order remains idempotent and reconcilable.
- Telegram notification stream is complete enough to monitor without `/docs`.

## Phase E: VPS Validation

Implementation targets:

- Deploy FastAPI to a VPS with Docker Compose, persistent Postgres, real HTTPS domain, Telegram webhook, restart policy, backups, and monitoring.
- Do not use Vercel for the trading backend. Vercel can host a future dashboard frontend only.

Acceptance checks:

- Run 4 weeks of paper-auto validation on VPS.
- Backup restore drill passes.
- Monitoring alerts reach Telegram.
- No unresolved critical incidents remain before the live decision gate.

## Live Decision Gate

All 8 criteria must pass before live trading:

1. 99%+ market-hours uptime over the 4-week paper-auto window.
2. Zero duplicate Alpaca orders; idempotency verified in logs and dashboard.
3. 100% order reconciliation between bot DB and Alpaca orders/activity.
4. Kill switch, drawdown breaker, max trades, max positions, and daily-loss limits all tested successfully.
5. Strategy health layer is active; underperforming strategies auto-deactivate correctly.
6. Paper slippage, fill behavior, and rejection rates are within expected Alpaca assumptions.
7. Dashboards, structured logs, backups, and Telegram alerts are working and reviewed.
8. Paper performance is acceptable: positive expectancy after costs, no uncontrolled drawdown, and no strategy kept active only by manual override.

If all 8 pass, switch to live only with temporary manual approval restored and micro-size limits:

```env
EXECUTION_MODE=live
ENABLE_REAL_TRADING=true
REQUIRE_APPROVAL=true
MAX_TRADES_PER_DAY=1
MAX_OPEN_POSITIONS=1
DEFAULT_TRADE_AMOUNT_USD=25
MAX_DAILY_LOSS_USD=25
```

Live micro-size can increase only after another reviewed validation period.
