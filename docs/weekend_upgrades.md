# Weekend upgrades — operator guide

This documents the profit-roadmap work landed on `claude/repo-audit-962zde`: what
changed, the new settings flags (with safe defaults), what's visible on the
dashboard, and the Monday runbook. Everything here is **paper-only**; nothing
enables real-money trading.

## What changed, by area

### Validation gate (backtesting)
- **Realistic costs** — the cost model now charges commission, non-spread
  slippage, SEC/FINRA sell-side regulatory fees, and a √-participation market
  impact term. Cost drag flows into persisted `metrics` and the ranking gate.
- **Real Deflated Sharpe Ratio** — the production qualification audit computes
  the Bailey/López de Prado DSR (deflated for the number of strategies tested),
  instead of passing raw Sharpe into the 0.95 confidence gate.
- **Sealed holdout gates promotion** — the batch backtester now scores the
  permanent holdout window; a strategy that lost on it is `blocked_holdout_negative`
  no matter how good its walk-forward folds looked.

### Risk & sizing
- **Portfolio-heat cap** — the sum of open per-trade risk is capped at
  `portfolio_max_heat_pct` (default 6%) in the pre-trade guardrail.
- **Drawdown governor** — new order sizes shrink as the day's realized loss
  deepens (default **off**, see flags).
- Pure helpers for vol-target sizing, fractional Kelly, and time-stops live in
  `app/risk/volatility_target.py`.

### Strategies
- Audited all 28 registered strategies (all real; the set is saturated in
  trend-pullback and breakout families).
- Added **Connors RSI(2)** (`connors_rsi2_reversion`) — the one genuine gap: a
  short-term mean reversion that buys a sharp RSI(2) dip inside a 200-EMA uptrend.
- **Regime router** — toggles whole strategy families by market regime
  (momentum on healthy trends, mean-reversion in chop, defensive in downtrends).
  Default **off**.

### Measurement & visibility (dashboard)
- Per-strategy live performance + a **live-vs-backtest decay monitor**
  (keep / watch / demote).
- **Equity curve** + daily/total realized-P&L tiles.
- **Execution quality** — signed fill slippage stamped on every execution, shown
  as a Slip-bps column and an average-slippage tile.
- **"Path to $1k/day"** — Stage 3 capital sizing + readiness gates.

### Reporting
- The daily Telegram summary gained a **today-focused P&L block** (today's
  realized P&L, best/worst strategy). Sends only when Telegram is configured.

### Stage 3 tools
- Capital-sizing calculator (measured $/day-per-$100k → capital required for a
  $/day target), Stage 3 readiness gates, and a real-capital preflight that
  **reports** readiness but can never enable real trading.

## New settings flags

| Flag | Default | Effect |
|---|---|---|
| `portfolio_max_heat_pct` | `6.0` | Aggregate open-risk cap (% of equity). 0 disables. |
| `regime_router_enabled` | `false` | Toggle strategy families by market regime. |
| `regime_router_cache_seconds` | `300` | Reuse the computed regime within a scan. |
| `drawdown_governor_enabled` | `false` | Scale new order size down as the day's loss deepens. |
| `drawdown_governor_soft_pct` | `2.0` | Daily-loss % where scaling begins. |
| `drawdown_governor_hard_pct` | `5.0` | Daily-loss % where scaling hits the floor. |
| `drawdown_governor_floor` | `0.25` | Minimum size multiplier in deep drawdown. |
| `stage1_decay_min_trades` | `20` | Trades before the decay monitor judges a strategy. |
| `daily_profit_target_usd` | `1000.0` | Stage 3 capital-sizing target. |

The two behavior-changing features (`regime_router_enabled`,
`drawdown_governor_enabled`) ship **off**. Validate them first (below), then
enable one at a time in the Railway env.

## Validate the default-off features (read-only)

```
python -m scripts.validate_gated_features
```

Prints, without changing any setting or placing a trade:
1. the current market regime and which strategy families the router would switch
   off, per timeframe; and
2. a replay of the drawdown governor over the paper track record, with P&L and
   max-drawdown deltas and a recommendation.

## Monday runbook

1. **Pre-open (~13:45 UTC)** — confirm the bot wakes up trading; the dashboard
   equity curve, per-strategy table, and Slip-bps column begin filling in.
2. **Validate** — run `scripts.validate_gated_features`; read both reports.
3. **Enable, one at a time** — if a report matches intent, set
   `regime_router_enabled` and/or `drawdown_governor_enabled` to `true` in the
   Railway env. Both are reversible.
4. **Let Stage 1 accrue** — as trades close, the decay verdicts and the
   "Path to $1k/day" panel populate. Demote what decays.
5. **Two decisions only you can make** — approve the ~$99/mo full-market SIP data
   feed (biggest execution-quality lever), and confirm the Telegram bot token so
   the daily P&L push sends.

## Guardrail

`ENABLE_REAL_TRADING` stays `false`. The real-capital preflight only *reports*
readiness; flipping real trading on is always an explicit human action.
