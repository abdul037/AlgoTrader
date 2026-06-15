# CX AlgoBot Unattended Paper Runbook

## Safety Baseline

- Dedicated Alpaca Paper account: `PA3B287XBZYU`
- Keep `EXECUTION_MODE=paper` and `ENABLE_REAL_TRADING=false`.
- Keep `PAPER_AUTO_APPROVE_PROPOSALS=false` and `AUTO_EXECUTION_WORKER_ENABLED=false` until the staged activation gates pass.
- Do not place manual trades in the dedicated account.
- Every automated Alpaca entry must be a broker-native long-equity bracket order.
- Account mismatch, unknown positions, missing protection, or unresolved reconciliation failures activate the circuit breaker and pause automation.

Required staged environment:

```env
ALPACA_ENABLED=true
ALPACA_EXPECTED_ACCOUNT_NUMBER=PA3B287XBZYU
ALPACA_RECONCILIATION_ENABLED=true
ALPACA_RECONCILIATION_INTERVAL_SECONDS=60
ALPACA_REQUIRE_BRACKET_ORDERS=true
EXECUTION_MODE=paper
PAPER_BROKER=alpaca
BROKER_FOR_EQUITIES=alpaca
ENABLE_REAL_TRADING=false
REQUIRE_APPROVAL=true
PAPER_AUTO_APPROVE_PROPOSALS=false
AUTO_EXECUTION_WORKER_ENABLED=false
MAX_TRADE_AMOUNT_USD=1000
MAX_TRADES_PER_DAY=5
MAX_OPEN_POSITIONS=3
MAX_DAILY_LOSS_USD=100
MAX_WEEKLY_LOSS_USD=300
MAX_RISK_PER_TRADE_PCT=1
MAX_CONSECUTIVE_LOSSES_BEFORE_COOLDOWN=2
```

## PostgreSQL Backend

Supabase managed PostgreSQL is the recommended production database. The bot,
scheduler, reconciliation worker, Prometheus, and Grafana still run on the VPS.
Do not run the continuous trading runtime in Supabase Edge Functions.

Create a dedicated Supabase project in a region close to the VPS. Use the
Direct connection from an IPv6-capable VPS or a project with the IPv4 add-on.
Use the Session Pooler as the fallback from an IPv4-only VPS. Store these
values only in the VPS `.env` file:

```env
# SQLAlchemy application and Alembic URL. Direct connection example:
DATABASE_URL=postgresql+psycopg://postgres:PASSWORD@db.PROJECT_REF.supabase.co:5432/postgres?sslmode=require

# Standard direct libpq URL used by pg_dump. Do not include "+psycopg".
POSTGRES_BACKUP_URL=postgresql://postgres:PASSWORD@db.PROJECT_REF.supabase.co:5432/postgres?sslmode=require

# Match the target PostgreSQL server's major version.
PG_DUMP_IMAGE=postgres:17-alpine
```

For an IPv4-only VPS without Supabase's IPv4 add-on, use the Session Pooler
host and `postgres.PROJECT_REF` user in both URLs instead.

The optional Compose-local PostgreSQL fallback is isolated behind the
`local-db` profile:

```bash
docker compose --profile local-db up -d postgres
```

For that fallback, set:

```env
DATABASE_URL=postgresql+psycopg://algobot:strong-password@postgres:5432/algobot
POSTGRES_BACKUP_URL=postgresql://algobot:strong-password@postgres:5432/algobot
POSTGRES_PASSWORD=strong-password
```

## PostgreSQL Migration

Back up the current SQLite database, initialize PostgreSQL, then migrate only into an empty target:

```bash
mkdir -p backups
cp etoro_bot.db "backups/etoro_bot_$(date -u +%Y%m%dT%H%M%SZ).db"
set -a
. ./.env
set +a
alembic upgrade head
python3 scripts/migrate_sqlite_to_postgres.py \
  --source ./etoro_bot.db \
  --target-url "$DATABASE_URL"
```

The migration copies proposals, executions, queue records, logs, runtime state, and all other existing repository tables. Validate record counts before starting the application.

## Deployment

```bash
cd /opt/CX_AlgoBot
git pull --ff-only origin main
docker compose up -d --build
docker compose ps
curl -fsS http://127.0.0.1:8011/health
curl -fsS http://127.0.0.1:8011/automation/status
curl -fsS http://127.0.0.1:8011/automation/reconciliation
curl -fsS http://127.0.0.1:8011/metrics
```

Prometheus is internal to Compose. Grafana is exposed on port `3000` with the provisioned `CX AlgoBot Paper Trading` dashboard.

## Scheduled Scans

Configure `MARKET_UNIVERSE_NAME=top100_us`, `MARKET_UNIVERSE_LIMIT=100`, `WORKFLOW_SCAN_DEFAULT_UNIVERSE_LIMIT=100`, `SCALP_SCAN_BATCH_SIZE=20`, `INTRADAY_REPEATED_SCAN_ENABLED=true`, `INTRADAY_SCAN_INTERVAL_MINUTES=15`, and `SWING_SCAN_INTERVAL_MINUTES=60`.

The scheduler performs full premarket, market-open, hourly swing, and end-of-day scans, plus rotating batches of 20 symbols every 15 minutes during market hours. Unsupported symbols are logged and skipped.

## Operations

Telegram commands:

- `/auto_status`
- `/schedule_status`
- `/reconciliation`
- `/strategy_status`
- `/blacklist`
- `/circuit_status`
- `/clear_circuit CONFIRM`
- `/kill_switch reason`

HTTP checks:

```bash
curl -fsS http://127.0.0.1:8011/automation/status
curl -fsS http://127.0.0.1:8011/automation/reconciliation
curl -fsS -X POST http://127.0.0.1:8011/automation/reconciliation/run
curl -fsS http://127.0.0.1:8011/automation/blacklist
curl -fsS http://127.0.0.1:8011/automation/strategy-health
curl -fsS http://127.0.0.1:8011/execution/queue
curl -fsS http://127.0.0.1:8011/institutional/readiness
curl -fsS http://127.0.0.1:8011/institutional/strategies
curl -fsS http://127.0.0.1:8011/institutional/brokers
curl -fsS http://127.0.0.1:8011/institutional/portfolio-risk
```

Do not clear the circuit breaker until the broker account is flat or all positions are confirmed bot-owned and protected, reconciliation is clean, and the initiating alert has been reviewed.

## Backups And Restore

Supabase managed backups do not replace an independently controlled export.
The `cx-algobot-backup` profile creates a compressed daily PostgreSQL dump in
`./backups` and retains 14 days. Ensure `PG_DUMP_IMAGE` matches the database
server's PostgreSQL major version, then enable the backup and off-host sync
after configuring rclone:

```bash
docker compose --profile backup --profile offsite-backup up -d cx-algobot-backup offsite-backup
```

Restore into an empty database using standard PostgreSQL URLs:

```bash
docker compose stop cx-algobot
docker run --rm -i "$PG_DUMP_IMAGE" \
  pg_restore --dbname="$POSTGRES_BACKUP_URL" --clean --if-exists \
  < backups/algobot_TIMESTAMP.dump
docker compose start cx-algobot
```

## Activation Gates

1. Shadow mode: scheduler and reconciliation run with both unattended flags disabled.
2. Complete the strategy-approved order, idempotency retry, post-order kill-switch drill, and clean 48-hour observation.
3. Enable `SCREENER_SCHEDULER_ENABLED=true`, `PAPER_AUTO_APPROVE_PROPOSALS=true`, and `AUTO_EXECUTION_WORKER_ENABLED=true` for two supervised market sessions.
4. Leave unattended paper-auto enabled only after zero duplicate orders, zero unprotected positions, clean reconciliation, and no unresolved critical alerts.

Live trading remains out of scope.

## Institutional Readiness Records

Strategy promotion, broker identity/capability evidence, broker comparisons,
portfolio risk, and rollout gates are stored in PostgreSQL and exposed under
`/institutional`. Mutations require `X-Control-Token` when
`CONTROL_API_TOKEN` is configured.

Do not mark a rollout gate as `passed` without attaching evidence and a
reviewer identity. `/institutional/readiness` remains blocked until the
current stage's gates pass, a strategy passes every production threshold, and
the latest portfolio risk snapshot is clean.

The eToro Demo adapter is additive and disabled by default. It uses official
v2 demo create/lookup/cost endpoints and official v1 demo portfolio/position
close endpoints. Set `ETORO_DEMO_V2_ENABLED=true` only after installing Demo
credentials, configuring an expected account ID, and completing a supervised
order lifecycle. It is not routed into unattended execution by this rollout.

`INSTITUTIONAL_PORTFOLIO_CONTROLS_ENABLED`, `ETORO_DEMO_V2_ENABLED`, and
`ETORO_PARALLEL_COMPARISON_ENABLED` must remain `false` during the current
observation period. Promote them only through signed rollout-gate evidence.
Live Alpaca mode requires separate `ALPACA_LIVE_*` credentials, a non-paper
base URL, and a separately verified live account number.
