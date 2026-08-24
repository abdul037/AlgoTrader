# AlgoTrader Rollout & Build Plan

Status of this document: living plan produced from a full-codebase audit
(execution/broker, autonomous run-loop, strategies/signals, risk/data/testing/
deployment). It complements — and does not replace —
[`alpaca_paper_to_live_roadmap.md`](alpaca_paper_to_live_roadmap.md) and
[`institutional_roadmap_status.md`](institutional_roadmap_status.md). Where the
paper-to-live roadmap defines the *phases*, this document records *what is
actually built vs. missing* and the concrete build work per phase.

## Honest state assessment

The system is far more complete than a typical trading-bot repo, but it is
deliberately architected to **stay in paper**, and the "auto" path has a few
load-bearing gaps that mean it does not yet run truly hands-off. Remaining work
is roughly **30% build, 70% wiring-hardening + validation** — not a rewrite.

| Layer | State |
|---|---|
| Alpaca paper execution (bracket entry+stop+target, 3-layer idempotency, reconciliation) | Built, real SDK, end-to-end |
| Kill switch / circuit breaker / emergency stop | Built |
| Dual risk gates (proposal + execution) + mandatory stop-loss | Built |
| 28-strategy library, walk-forward backtest (embargo/holdout), cost model, deflated-Sharpe governance | Built (scaffolding) |
| Full auto lifecycle code (shadow -> supervised -> unattended) | Built, gated off |
| Deploy stack (Compose: Postgres, backups, offsite sync, Prometheus, Grafana; Railway; Alembic) | Built |
| Offline test suite | Built (347 passing) |
| Independent scheduler / real-time fills / live edge / CI | **Gaps — see below** |

## The gaps that actually block "auto"

1. **No independent scheduler.** The whole cadence (scans, proposals, queue
   processing, reconciliation, fill ingestion) rides a single 60-second daemon
   thread piggybacked on the Telegram loop (`app/notifications/scheduler.py`).
   If `telegram_polling_enabled=True`, the web process starts no scheduler at
   all (`app/main.py:589`). Single-threaded (a slow scan stalls everything),
   swallows exceptions, no watchdog, dies with the process. The one "cron"
   entrypoint (`scripts/run_workflow_cycle.py`) is built without proposal/
   automation deps, so it can scan and alert but cannot propose or execute.
2. **No real-time fill handling.** No Alpaca `trade_updates` websocket. Fills
   and bracket exits are only recorded on the reconciliation sweep, on the same
   fragile thread. The sim-ledger exits (`refresh_open_positions`) are only
   reachable via a manual `POST /paper/refresh`, not scheduled.
3. **Health endpoint misleads the orchestrator.** Railway's healthcheck hits
   `/health`, which always returns static `"ok"` (`app/main.py:613`). A stalled
   worker or broker outage still reports healthy. The real signal is in
   `/health/details`, which nothing monitors.
4. **The live "brain" is one hardcoded strategy.** Two disconnected pipelines:
   the 28-strategy library only feeds the *screener*, while the live Telegram
   signal path hardcodes one strategy per asset (`pullback_trend` equities,
   `gold_momentum` gold — `app/signals/evaluation.py:36`). Those two use
   fixed-percentage, volatility-blind stops.
5. **Packaging broke on a clean install.** `pyarrow` was used but undeclared
   (Alpaca data cache -> ImportError); unbounded pins (`pandas>=2.2` -> 3.0)
   broke the suite; there was no CI. *(Addressed in Phase 0 below.)*

## Rollout phases

Phase names align with `alpaca_paper_to_live_roadmap.md`. Phase 0 and Phase C.5
are inserted for hygiene and autonomy-runtime work that must precede any
meaningful unattended run.

### Phase 0 — Hygiene (DONE)

- [x] Remove the legacy `algobot/` tree (last touched at the first commit;
      nothing in `Dockerfile`/`docker-compose.yml`/`railway.json` references it).
- [x] Pin dependencies with upper bounds and declare `pyarrow`
      (`requirements.txt`, `pyproject.toml`).
- [x] Fix forward-incompatible pandas `"1H"` alias in `tests/`.
- [x] Fix a latent walk-forward edge bug: `split()` accepts `embargo_days=0`
      but the window invariant rejected the resulting `train_end == test_start`
      fold; relaxed to the correct half-open invariant `train_end <= test_start`.
- [x] Disable the Hypothesis deadline on the walk-forward property test
      (machine-speed flake, not a perf test).
- [x] Add GitHub Actions CI (`.github/workflows/ci.yml`): ruff + black + mypy
      (advisory) and pytest (hard gate).
- [ ] Follow-up: a dedicated `ruff --fix` + `black .` sweep, then flip lint to
      hard gates in CI. Kept separate so the reformat diff stays reviewable.

### Phase C.5 — Make autonomy actually run unattended (real build work)

- [x] Own the scheduler: `app/automation/scheduler_worker.py` `SchedulerWorker`
      replaces the Telegram-piggyback thread. Each job (`workflow_cadence`,
      `telegram_hourly_alerts`, `paper_position_refresh`) runs on its own
      independent cadence; a failing job never stops the loop; a liveness
      heartbeat is persisted to `runtime_state` every tick. Wired into
      `main.py` startup, decoupled from the Telegram command service.
- [x] Schedule sim-ledger `refresh_open_positions` as the
      `paper_position_refresh` job (was previously reachable only via
      `POST /paper/refresh`).
- [x] Real health probe: `GET /health/ready` returns 503 when the worker
      heartbeat is missing/stale or the DB is unreachable, 200 otherwise (and
      "not managed here" when a separate polling process owns the cadence).
      Railway `healthcheckPath` now points at it.
- [x] Fix the autonomy-blind cron path: `scripts/run_workflow_cycle.py` now
      builds the full `create_app` wiring and drives the same worker jobs, so a
      cron-run `scheduled` cycle can actually propose and execute (previously
      scan/alert only).
- [ ] Real fill handling: subscribe to Alpaca `trade_updates` so fills/exits
      post immediately, with the reconciliation sweep as backstop. (Next.)
- [ ] Watchdog/auto-restart of the worker process (currently `restartPolicyType:
      ALWAYS` restarts the container; readiness now makes a stalled worker
      visible to the orchestrator).

Definition of done: a full scan -> propose -> execute -> fill -> exit cycle
completes with the web process down and Telegram in webhook mode, and a killed
worker is detected and restarted.

### Phase D1 — Observability & Postgres

- [ ] SQLite -> Postgres (Alembic + `scripts/migrate_sqlite_to_postgres.py`
      exist). Add `target_metadata` to `migrations/env.py` for autogenerate +
      drift detection (today `storage/db.py` DDL and Alembic are two unlinked
      sources of truth).
- [ ] Wire Prometheus/Grafana into the Railway path (currently Compose-only).

### Phase D2 — Self-monitoring & risk correctness

- [ ] Fix live exposure accounting: `risk/context.py` never populates
      `exposure_by_sector_pct` / `correlated_exposure_pct`, so sector and
      correlation caps only see the new order — accumulated concentration is not
      enforced (material for a tech-heavy universe).
- [ ] Make loss limits consider unrealized drawdown, not just realized.
- [ ] Enable `institutional_portfolio_controls_enabled` and either wire the dead
      `allocate_candidates` allocator or remove it.
- [ ] Strategy auto-deactivation, instrument blacklist, drawdown circuit
      breaker, Telegram status commands.

### Phase D3 — Paper auto-execution ON

- [ ] Flip `PAPER_AUTO_OPERATION_MODE=unattended`, enable the three auto flags,
      tighten paper-auto defaults (max 3 trades/day, 2 positions, $25 daily
      loss). This is a graduation of existing code (`automation/unattended.py`),
      not new build.

### Phase E + Live decision gate

- [ ] Unchanged from `alpaca_paper_to_live_roadmap.md`: 4-week VPS paper-auto
      validation, then the 8-criteria live gate.

## Strategy edge work (highest-value, most honest gap)

The plumbing to trade is done; the edge is not demonstrated.

1. Unify the pipelines — make the live path consume the ranked screener output
   instead of one hardcoded strategy per asset.
2. Stop trusting hardcoded quality scores — many `execution_quality` /
   `trend_quality` / `confidence` values are literal constants fed to the ranker
   as if measured, inflating scores circularly.
3. Fix the validation gate — permissive thresholds (PF >= 1.2, 10 trades) and,
   crucially, a default of "watchlist" (not "block") when no backtest exists,
   with nothing scheduling the batch backtester to populate summaries.
4. Add real regime + correlation filters — today "regime" is price-vs-EMA
   geometry and "relative strength" compares a symbol to itself.
5. Volatility-based (ATR) stops on the live path, replacing fixed-% stops.
6. Fix understated drawdown — aggregation takes `max` over 2-week folds, hiding
   multi-month drawdowns; compute over the full concatenated OOS equity curve.

Recommendation: graduate to unattended paper-auto on **one proven strategy**
(strict OOS gate: deflated Sharpe, honest drawdown, cost-adjusted positive
expectancy), not on strategy count.

## How to run & test

Test (fully offline, no credentials — `tests/conftest.py` supplies a MockBroker
and `paper_broker="self_simulated"`):

```bash
pip install -r requirements.txt
pip install -e ".[dev,ml]"
pytest -q            # 347 passing
```

Run locally (paper, safe defaults preserved):

```bash
cp .env.example .env    # fill Alpaca paper keys + Telegram creds
docker compose up       # app + postgres + prometheus + grafana
```

Prove the path before trusting it (Phase C checklist, still the right first
step): one Alpaca paper order during market hours via a real proposal;
re-process the same queue item and confirm no duplicate; run the kill-switch
drill; observe 48h with no unhandled exceptions.

## Priority order

- **P0** (bot cannot truly auto-run without these): independent scheduler +
  watchdog; real-time fills; real health probe; packaging/CI (done).
- **P1** (correctness/edge): unify live pipeline; fix sector/correlation
  exposure accounting; fix backtest gate + honest drawdown; regime/correlation
  filters.
- **P2** (hardening): Alembic drift detection; Railway metrics; HA/failover;
  `SecretStr`; legacy eToro idempotency.
