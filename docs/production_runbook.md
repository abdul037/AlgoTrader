# CX AlgoBot Production Runbook

## Target Runtime

- Run the bot on a Cloud VPS with Docker Compose.
- Use a permanent HTTPS domain for `TELEGRAM_WEBHOOK_URL`; do not use ngrok for production.
- Keep `EXECUTION_MODE=paper`, `ENABLE_REAL_TRADING=false`, `AUTO_PROPOSE_ENABLED=false`, and `AUTO_EXECUTE_AFTER_APPROVAL=false` until paper testing is complete.

## Deploy Or Restart

```bash
cd /opt/CX_AlgoBot
docker compose up -d --build
docker compose ps
curl -fsS http://127.0.0.1:8011/health
curl -fsS http://127.0.0.1:8011/automation/status
curl -fsS http://127.0.0.1:8011/telegram/webhook/status
```

## Daily Operations

- Telegram status: `/auto_status`
- Deep scan: `/scan top100 tf=1m,5m,10m,15m,1h,1d,1w`
- Paper dashboard: `/performance`
- Pause scheduled scans and auto proposals: `/pause_auto reason`
- Resume scheduled scans: `/resume_auto reason`
- Emergency stop: `/kill_switch reason`

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

## Live Trading Rollout

1. Paper-only automated proposals for at least 2 weeks.
2. Approval-gated paper execution with daily review.
3. Approval-gated live execution with very small notional size only.
4. Increase size only after at least 30 closed paper trades, acceptable profit factor, controlled drawdown, and no unresolved execution failures.

Live queue processing remains blocked unless all are true: `EXECUTION_MODE=live`, `ENABLE_REAL_TRADING=true`, `REQUIRE_APPROVAL=true`, runtime kill switch off, direct fresh eToro quote, drift within limit, and risk checks pass.
