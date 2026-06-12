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

## PostgreSQL Migration

Back up the current SQLite database, initialize PostgreSQL, then migrate only into an empty target:

```bash
mkdir -p backups
cp etoro_bot.db "backups/etoro_bot_$(date -u +%Y%m%dT%H%M%SZ).db"
docker compose up -d postgres
DATABASE_URL='postgresql+psycopg://algobot:password@localhost:5432/algobot' alembic upgrade head
python3 scripts/migrate_sqlite_to_postgres.py \
  --source ./etoro_bot.db \
  --target-url 'postgresql+psycopg://algobot:password@localhost:5432/algobot'
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
```

Do not clear the circuit breaker until the broker account is flat or all positions are confirmed bot-owned and protected, reconciliation is clean, and the initiating alert has been reviewed.

## Backups And Restore

The `cx-algobot-backup` service creates a compressed daily PostgreSQL dump in `./backups` and retains 14 days. Enable the daily off-host sync after configuring rclone:

```bash
docker compose --profile offsite-backup up -d offsite-backup
```

Restore into an empty database:

```bash
docker compose stop cx-algobot
docker compose exec -T postgres dropdb -U algobot algobot
docker compose exec -T postgres createdb -U algobot algobot
docker compose exec -T postgres pg_restore -U algobot -d algobot --clean --if-exists < backups/algobot_TIMESTAMP.dump
docker compose start cx-algobot
```

## Activation Gates

1. Shadow mode: scheduler and reconciliation run with both unattended flags disabled.
2. Complete the strategy-approved order, idempotency retry, post-order kill-switch drill, and clean 48-hour observation.
3. Enable `SCREENER_SCHEDULER_ENABLED=true`, `PAPER_AUTO_APPROVE_PROPOSALS=true`, and `AUTO_EXECUTION_WORKER_ENABLED=true` for two supervised market sessions.
4. Leave unattended paper-auto enabled only after zero duplicate orders, zero unprotected positions, clean reconciliation, and no unresolved critical alerts.

Live trading remains out of scope.
