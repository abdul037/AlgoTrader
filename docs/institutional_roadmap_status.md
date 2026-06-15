# Institutional Roadmap Status

## Implemented

- Versioned strategy, audit, promotion, broker, reconciliation, comparison,
  portfolio-risk, and signed rollout-gate persistence.
- Production promotion thresholds for out-of-sample trades, deflated and
  rolling Sharpe, profit factor, after-cost expectancy, drawdown, audit
  errors, and protective-exit coverage.
- Read-only institutional readiness, broker, comparison, strategy, portfolio
  risk, and rollout-gate APIs. Mutations use the control API token.
- Portfolio drawdown, exposure, allocation, micro-live risk, and deferred
  short-sale borrow/margin controls.
- Separate Alpaca paper/live credentials, URLs, and expected account identity.
- Official eToro Demo hybrid adapter: v2 order create/lookup/cost checks and
  official v1 demo portfolio/position close.
- Durable eToro request idempotency, eToro Demo reconciliation, shared
  kill-switch fanout, and guarded parallel broker mirroring.
- Dynamic liquidity-universe builder, point-in-time universe model, and OHLCV
  integrity checks. Strategy audits reject invalid datasets and unexplained
  errors.
- Invalid strategy stop/target plans are rejected before audit/backtest entry.
- Prometheus institutional metrics and a provisioned Grafana dashboard.
- Explicit `shadow`, `supervised`, and `unattended` paper-auto operation modes.

## Disabled By Default

- `INSTITUTIONAL_PORTFOLIO_CONTROLS_ENABLED=false`
- `PAPER_AUTO_OPERATION_MODE=shadow`
- `ETORO_DEMO_V2_ENABLED=false`
- `ETORO_PARALLEL_COMPARISON_ENABLED=false`
- `SHORT_TRADING_ENABLED=false`
- Real trading remains disabled.

## Evidence Still Required

These cannot be completed by code changes alone:

- Rotate previously exposed broker/database credentials.
- Finish the clean 48-hour observation and two supervised Alpaca sessions.
- Procure and load a licensed point-in-time, corporate-action-aware research
  dataset and validate it.
- Produce at least one strategy audit that passes every promotion threshold.
- Complete 20 shadow sessions, then 60 dual-broker paper sessions and required
  matched/closed trade counts.
- Verify the actual eToro Demo account identity and supervised order lifecycle.
- Configure managed monitoring, independent off-site backups, and a restore
  drill.
- Complete signed micro-live and legal/compliance gates before live or
  external-capital use.

`GET /institutional/readiness` is the source of truth for unresolved gates.
