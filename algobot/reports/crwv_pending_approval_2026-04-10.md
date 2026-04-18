# CRWV Real-Account Pending Approval Memo

Date: 2026-04-10
Status: `pending_manual_approval_only`
Execution status: `not_submitted`

## Purpose

This memo records a small, high-risk, same-day `CRWV` trade idea for manual review only.
It is not an executable order. It must not be submitted until all real-trading safeguards
have passed and a fresh live eToro quote is checked at approval time.

## Constraint Notes

- This repo is designed for swing trading, not intraday day trading.
- `CRWV` is unsupported by default in the current allowlist and must remain blocked from
  execution unless explicitly added to `ALLOWED_INSTRUMENTS`.
- Real trading must remain approval-gated.
- Max trade value for this idea: `$50`.
- Leverage: `1x` only.

## Approval Checklist

All of the following must be true before any live submission:

1. `ETORO_ACCOUNT_MODE=real`
2. `ENABLE_REAL_TRADING=true`
3. `REQUIRE_APPROVAL=true`
4. `CRWV` explicitly added to `ALLOWED_INSTRUMENTS`
5. Trade value is `<= $50`
6. Leverage is `1x`
7. Stop loss is defined
8. Risk validation passes immediately before submit
9. Fresh eToro live quote is re-checked at approval time
10. Manual approval is recorded before execution

## Trade Thesis

`CRWV` is a volatile AI infrastructure stock. The thesis is momentum continuation only.
This is not a long-term investment memo. This is a tightly sized, same-day tactical idea.

Because a fresh eToro broker quote could not be safely anchored into this memo at write time,
the setup is conditional and must be evaluated against live eToro market data at approval time.

## Conditional Setup

Direction: `TBD at approval time`

### Long Setup

- Consider only if `CRWV` is strong versus its opening range and reclaiming momentum.
- Entry condition:
  - price breaks above the session opening-range high on live eToro data
  - and holds above that level on confirmation
- Stop:
  - below the opening-range low or the nearest intraday support
  - whichever is tighter while keeping total risk small
- Target:
  - first target at `1.5R`
  - hard exit before market close if still open

### Short Setup

- Consider only if `CRWV` loses the session opening-range low and continues weak.
- Entry condition:
  - price breaks below the session opening-range low on live eToro data
  - and fails to reclaim it on confirmation
- Stop:
  - above the opening-range high or the nearest intraday resistance
  - whichever is tighter while keeping total risk small
- Target:
  - first target at `1.5R`
  - hard exit before market close if still open

### No-Trade Condition

Do not trade if any of the following apply:

- spread is abnormally wide
- price is chopping inside the opening range
- no directional confirmation appears
- risk cannot be defined tightly enough for a `$50` max-value trade
- eToro quote or instrument resolution is unavailable

## Pending Proposal Payload

```json
{
  "symbol": "CRWV",
  "account_mode": "real",
  "status": "pending",
  "amount_usd": 50.0,
  "leverage": 1,
  "direction": "conditional_long_or_short",
  "proposed_price": null,
  "stop_loss": null,
  "take_profit": null,
  "strategy_name": "manual_intraday_opening_range_review",
  "rationale": "Conditional same-day tactical setup on CRWV using live eToro price confirmation. No execution without manual approval, live quote refresh, and full risk validation.",
  "notes": "Outside the bot's normal swing-trading mandate. Use only as a manually reviewed exception."
}
```

## Reviewer Decision Block

- Reviewer:
- Decision:
- Time:
- Notes:

## Final Execution Rule

Even if approved, this memo still requires a final live eToro quote check and a full risk
re-validation before any order is sent.
