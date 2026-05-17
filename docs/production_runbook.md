# CX AlgoBot 24/7 Production Runbook

## Target Runtime

- Run the trading backend on a Cloud VPS or another persistent container host with Docker Compose.
- Do not deploy the trading backend to Vercel. Vercel is suitable for a future dashboard frontend, but this bot needs a long-running FastAPI process, an in-process workflow scheduler, broker API access, durable storage, and restart recovery.
- Use a permanent HTTPS domain for `TELEGRAM_WEBHOOK_URL`; do not use ngrok for 24/7 operation.
- Keep `EXECUTION_MODE=paper`, `PAPER_BROKER=alpaca`, `ENABLE_REAL_TRADING=false`, and `REQUIRE_APPROVAL=true` through the Alpaca paper validation period.

References:

- Vercel function duration limits: https://vercel.com/docs/functions/limitations
- Vercel cron jobs run request-triggered functions, not a persistent trading process: https://vercel.com/docs/cron-jobs

## 24/7 Deployment

The repository already contains the production container shape:

- `Dockerfile` runs `uvicorn app.main:app` on port `8011`.
- `docker-compose.yml` uses `restart: unless-stopped`.
- The `cx_algobot_data` volume persists SQLite state and market-data cache.
- The `cx-algobot-backup` sidecar copies `/data/etoro_bot.db` to `./backups` daily and keeps 14 days.

Deploy or restart:

```bash
cd /opt/CX_AlgoBot
git pull --ff-only origin main
docker compose up -d --build
docker compose ps
curl -fsS http://127.0.0.1:8011/health
curl -fsS http://127.0.0.1:8011/automation/status
curl -fsS http://127.0.0.1:8011/telegram/webhook/status
```

Minimum VPS environment:

```env
ALPACA_ENABLED=true
EXECUTION_MODE=paper
PAPER_BROKER=alpaca
BROKER_FOR_EQUITIES=alpaca
ENABLE_REAL_TRADING=false
REQUIRE_APPROVAL=true
AUTO_PROPOSE_ENABLED=false
AUTO_EXECUTE_AFTER_APPROVAL=false
SCREENER_SCHEDULER_ENABLED=false
TELEGRAM_WEBHOOK_URL=https://your-domain.example/telegram/webhook
```

## Alpaca Paper Rollout

The manual Alpaca paper smoke path has been proven through Telegram and Alpaca Paper. Continue in this order:

1. Phase C validation: complete the 48-hour paper observation checklist in [`alpaca_paper_start.md`](alpaca_paper_start.md).
2. Phase D1: migrate to Postgres, structured logs, and Grafana dashboards before unattended trading.
3. Phase D2: add self-monitoring, auto-deactivation, drawdown circuit breaker, and blacklist controls.
4. Phase D3: enable paper auto-execution only after the monitoring layer is green.
5. Phase E: run 4 weeks of VPS-hosted paper-auto validation before any live micro-size decision gate.

The decision-complete roadmap is maintained in [`alpaca_paper_to_live_roadmap.md`](alpaca_paper_to_live_roadmap.md).

## Daily Operations

- Telegram status: `/auto_status`
- Scheduler buckets: `/schedule_status`
- Deep scan: `/scan top100 tf=1m,5m,10m,15m,1h,1d,1w`
- Paper dashboard: `/performance`
- Queue inspection: `/queue`
- Pause scheduled scans and auto proposals: `/pause_auto reason`
- Resume scheduled scans: `/resume_auto reason`
- Emergency stop: `/kill_switch reason`

FastAPI admin checks:

```bash
curl -fsS http://127.0.0.1:8011/health
curl -fsS http://127.0.0.1:8011/automation/status
curl -fsS http://127.0.0.1:8011/workflow/status
curl -fsS http://127.0.0.1:8011/execution/queue
```

## Backups

- The Compose backup sidecar copies `/data/etoro_bot.db` to `./backups` daily and keeps 14 days.
- Before high-risk changes, run:

```bash
mkdir -p backups
docker cp cx-algobot:/data/etoro_bot.db "backups/etoro_bot_manual_$(date -u +%Y%m%dT%H%M%SZ).db"
```

## Restore

```bash
docker compose stop cx-algobot
docker cp backups/etoro_bot_YYYYMMDDTHHMMSSZ.db cx-algobot:/data/etoro_bot.db
docker compose start cx-algobot
curl -fsS http://127.0.0.1:8011/health
```

## Webhook Reset

1. Confirm DNS and HTTPS are working for the production domain.
2. Set `TELEGRAM_WEBHOOK_URL=https://your-domain.example/telegram/webhook`.
3. Restart the app:

```bash
docker compose up -d
curl -fsS http://127.0.0.1:8011/telegram/webhook/status
```

## Live Trading Gate

Live trading is out of scope until the paper-auto validation period completes and all 8 criteria in [`alpaca_paper_to_live_roadmap.md`](alpaca_paper_to_live_roadmap.md) pass. Live queue processing must remain blocked unless all are true:

- `EXECUTION_MODE=live`
- `ENABLE_REAL_TRADING=true`
- `REQUIRE_APPROVAL=true`
- Runtime kill switch is off
- Broker quote is fresh
- Entry drift is within limit
- Risk checks pass
- Phase D self-monitoring and drawdown circuit breakers are active
