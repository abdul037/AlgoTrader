# Railway Shadow-Mode Deployment

Railway runs the long-lived AlgoBot FastAPI process, in-process scheduler, and
Alpaca reconciliation worker. Supabase remains the PostgreSQL backend.

The first deployment must remain in bootstrap mode. It may reconcile the
dedicated Alpaca Paper account and exercise scheduled maintenance, but it must
not scan, approve, or execute orders. After verifying a clean reconciliation,
transition to scan-only shadow mode.

## Create The Service

1. In Railway, create a project from the GitHub repository
   `abdul037/AlgoTrader`.
2. Select the `main` branch and repository root.
3. Select a region near the Supabase Sydney project.
4. Keep exactly one replica.
5. Generate a Railway public domain for the API and Telegram webhook.

Railway reads `railway.json` from the repository. It builds the Dockerfile,
runs `alembic upgrade head` before deployment, checks `/health`, disables
application sleeping, and prevents overlapping old/new deployments.

## Configure Variables

Use Railway's raw variable editor. Supply the rotated Supabase and Alpaca
credentials directly in Railway; never commit them.

Generate a validated shadow-mode bundle from the local `.env`, copy it to the
clipboard, and paste it into Railway's raw variable editor:

```bash
python3 scripts/export_railway_env.py
pbcopy < .railway.env
```

The generated `.railway.env` is Git-ignored and has owner-only permissions.

Required secret variables:

```env
DATABASE_URL=postgresql+psycopg://postgres.PROJECT_REF:URL_ENCODED_PASSWORD@POOLER_HOST:5432/postgres?sslmode=require
ALPACA_API_KEY=
ALPACA_SECRET_KEY=
CONTROL_API_TOKEN=
```

Generate `CONTROL_API_TOKEN` with at least 32 random bytes. Send it as the
`X-Control-Token` header for every mutating API request. The Telegram webhook
continues to use its separate Telegram secret header.

Required scan-only shadow-mode variables:

```env
DEPLOYMENT_STAGE=shadow

ALPACA_BASE_URL=https://paper-api.alpaca.markets
ALPACA_DATA_URL=https://data.alpaca.markets
ALPACA_DATA_FEED=iex
ALPACA_ENABLED=true
ALPACA_EXPECTED_ACCOUNT_NUMBER=PA3B287XBZYU
ALPACA_RECONCILIATION_ENABLED=true
ALPACA_RECONCILIATION_INTERVAL_SECONDS=60
ALPACA_REQUIRE_BRACKET_ORDERS=true

EXECUTION_MODE=paper
PAPER_BROKER=alpaca
BROKER_FOR_EQUITIES=alpaca
ETORO_ACCOUNT_MODE=demo
ENABLE_REAL_TRADING=false
REQUIRE_APPROVAL=true
PAPER_SIMULATED_FALLBACK_ENABLED=false

AUTOMATION_PAUSED_DEFAULT=false
KILL_SWITCH_ENABLED=false
KILL_SWITCH_AUTO_CLOSE_POSITIONS=false
PAPER_AUTO_APPROVE_PROPOSALS=false
AUTO_EXECUTION_WORKER_ENABLED=false
AUTO_PROPOSE_ENABLED=false
AUTO_EXECUTE_AFTER_APPROVAL=false

SCREENER_SCHEDULER_ENABLED=true
LEDGER_CYCLE_ENABLED=false

MAX_TRADE_AMOUNT_USD=1000
MAX_TRADES_PER_DAY=5
MAX_OPEN_POSITIONS=3
MAX_DAILY_LOSS_USD=100
MAX_WEEKLY_LOSS_USD=300
MAX_RISK_PER_TRADE_PCT=1
MAX_CONSECUTIVE_LOSSES_BEFORE_COOLDOWN=2
```

Optional Telegram webhook variables:

```env
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_ALLOWED_CHAT_IDS=
TELEGRAM_WEBHOOK_URL=https://YOUR-RAILWAY-DOMAIN/telegram/webhook
TELEGRAM_WEBHOOK_SECRET=
TELEGRAM_POLLING_ENABLED=false
```

Do not set `PORT`; Railway injects it automatically. The application must use
the Supabase Session Pooler URL because Railway services may use IPv4 egress.

Railway replaces the Compose runtime, so the Compose Prometheus, Grafana, and
backup services are not started. Use Railway logs and `/metrics` during the
initial observation period. Configure an independent scheduled PostgreSQL
export before unattended activation.

## Verify Deployment

Review the pre-deploy migration and application logs, then verify:

```bash
curl -fsS https://YOUR-RAILWAY-DOMAIN/health
curl -fsS https://YOUR-RAILWAY-DOMAIN/automation/status
curl -fsS https://YOUR-RAILWAY-DOMAIN/automation/reconciliation
curl -fsS https://YOUR-RAILWAY-DOMAIN/metrics
```

Expected bootstrap safety state:

- Account number is `PA3B287XBZYU`.
- Reconciliation status is `ok`.
- Automation is paused.
- Kill switch is enabled.
- Paper auto-approval is disabled.
- Auto-execution worker is disabled.
- No actionable queue records, unknown positions, or unprotected positions.

After a clean bootstrap verification, set `DEPLOYMENT_STAGE=shadow`,
`AUTOMATION_PAUSED_DEFAULT=false`, and `KILL_SWITCH_ENABLED=false`. Redeploy,
run reconciliation, then call `/automation/resume` using `X-Control-Token`.
Shadow mode permits scheduled scans while the validator continues to force all
proposal, approval, and execution automation flags off.

## Activation Gates

Start the clean 48-hour observation only after the deployment has remained
healthy, reconciliation is clean, and scan-only shadow mode is active. Do not
enable either unattended flag until the supervised-session gates are complete.
