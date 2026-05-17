# Alpaca Paper Start Checklist

Use this checklist for the first real Alpaca paper trade through the bot. This procedure keeps real-money trading disabled and keeps Telegram approval mandatory.

## Preconditions

- FastAPI is running from `main` or the reviewed deployment branch.
- Alpaca paper credentials are present only in local `.env` or the VPS secret store.
- Alpaca Paper dashboard is open in a browser: https://app.alpaca.markets/
- `ENABLE_REAL_TRADING=false`
- `EXECUTION_MODE=paper`
- `PAPER_BROKER=alpaca`
- `BROKER_FOR_EQUITIES=alpaca`
- `REQUIRE_APPROVAL=true`
- `AUTO_PROPOSE_ENABLED=false`
- `AUTO_EXECUTE_AFTER_APPROVAL=false`
- `SCREENER_SCHEDULER_ENABLED=false`

Verify:

```bash
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/automation/status
curl -fsS http://127.0.0.1:8000/config/summary
```

Telegram:

```text
/auto_status
```

If the kill switch is on, resume only when ready for the manual paper test:

```text
/resume_auto phase c paper start
```

## First Paper Trade

Prefer a backtest-gated top proposal:

```text
/propose_top 1000 10
```

If no proposal is created, try a single allowed liquid equity:

```text
/propose NVDA 1000
```

If Telegram returns a proposal ID:

```text
/approve <proposal_id>
/enqueue <proposal_id>
/queue
/process_queue <queue_id>
```

Expected result:

- Telegram reports the queue record as processed.
- Alpaca Paper dashboard shows the order under Orders or Activity.
- If the order fills, Alpaca Paper Positions shows the symbol.
- The bot remains in paper mode and real trading remains disabled.

## Idempotency Check

Process the same queue item a second time:

```text
/process_queue <same_queue_id>
```

Expected result:

- The bot returns the existing execution state.
- Alpaca Paper does not show a duplicate order.

## Kill Switch Drill

After the first paper order appears in Alpaca:

```text
/kill_switch phase c drill
/auto_status
```

Expected result:

- Open Alpaca paper orders are canceled.
- Alpaca paper positions are closed if any were open.
- New proposals and queue processing are blocked while the kill switch is on.

When the drill is complete:

```text
/resume_auto phase c drill complete
/auto_status
```

## 48-Hour Observation

For the first two market sessions keep:

- `AUTO_PROPOSE_ENABLED=false`
- `AUTO_EXECUTE_AFTER_APPROVAL=false`
- `SCREENER_SCHEDULER_ENABLED=false`

Operate manually through Telegram:

```text
/scan 5
/propose_top 1000 10
/proposals pending
/approve <proposal_id>
/enqueue <proposal_id>
/process_queue <queue_id>
/performance
/auto_status
```

Do not enable automatic proposal creation until the first paper trade, idempotency check, and kill-switch drill all pass.
