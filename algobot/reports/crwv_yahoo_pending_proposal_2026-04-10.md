# CRWV Yahoo-Based Pending Proposal

Date: 2026-04-10
Status: `pending_approval`
Execution status: `not_submitted`
Data source for signal: `Yahoo Finance`
Broker for any later execution: `eToro`

## Signal Snapshot

As of `2026-04-10 14:55:00-04:00`:

- Last price: `103.12`
- Session open: `91.66`
- Session high: `105.90`
- Session low: `91.37`
- Session VWAP: `99.393`
- Daily 50-period trend MA: `85.708`
- Daily 10-period pullback MA: `83.231`
- Daily EMA8 / EMA21: `88.7625 / 84.6207`

Read:

- Daily trend is up.
- Price is above the session open and above VWAP.
- There is no short signal.
- The cleaner setup is a conditional breakout long above the current session high.

## Trade Decision

Preferred setup: `buy`

Reason:

`CRWV` is in a constructive daily trend and is still holding above both session open and
intraday VWAP. That favors continuation rather than fading the move. The cleaner entry is a
break above the session high rather than chasing the current price under resistance.

## Pending Proposal

This is a pending proposal only. It must not be sent until all approval and broker checks pass.

```json
{
  "symbol": "CRWV",
  "account_mode": "real",
  "status": "pending",
  "direction": "buy",
  "order_style": "conditional_breakout",
  "amount_usd": 50.0,
  "leverage": 1,
  "entry_trigger": 106.01,
  "stop_loss": 99.30,
  "take_profit": 116.08,
  "strategy_name": "yahoo_intraday_breakout_review",
  "rationale": "Daily trend is constructive and intraday price remains above both session open and VWAP. Buy only on a confirmed break above the session high.",
  "notes": "Same-day tactical idea. Cancel if not triggered before market close."
}
```

## Risk Notes

- This is a high-volatility stock.
- The trade value cap is `<= $50`.
- At `1x`, the position would likely be fractional if submitted by amount.
- Using the proposed trigger and stop, the approximate dollar risk is about `3.16` on a `$50` position.
- Do not enter if price loses VWAP before trigger.
- Cancel the idea if the breakout never confirms before the market closes.

## Approval Checklist

Before any live submission:

1. `CRWV` remains explicitly allowed in env
2. `REQUIRE_APPROVAL=true`
3. fresh live eToro quote is checked
4. eToro spread and market status are acceptable
5. risk validation passes again immediately before submission
6. proposal is explicitly approved by the reviewer

## Approval Block

- Reviewer:
- Decision:
- Time:
- Notes:
