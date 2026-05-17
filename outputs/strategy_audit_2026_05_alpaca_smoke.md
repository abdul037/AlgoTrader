# Strategy Audit 2026-05

- Audit date: 2026-05-17T14:10:16.013600+00:00
- Window: 2020-01-01 to 2024-12-31
- Universe: top100_us (2 requested)
- Timeframe: 1d
- Data provider: alpaca
- n_trials: 12
- Walk-forward: 180/14/14/1/28 days (train/test/step/embargo/holdout)
- Cost model defaults: name=alpaca, spread_bps=2.0, extended_hours_spread_bps=10.0, overnight_fee_daily_pct=0.0, weekend_multiplier=1.0, fx_spread_bps=0.0, min_position_usd=1.0
- Registered strategies evaluated: 1 (prompt n_trials remains 12)
- Execution method: full-history warmup with entries gated to walk-forward test windows; trades are grouped by test-window entry time.

Note: strategies with `vwap` in the name are intraday by nature; 1d-bar results likely understate their real performance and are flagged below.

## Ranked Results

| rank | strategy | intraday limit | deflated_sharpe | sharpe | max_dd_pct | win_rate | profit_factor | expectancy_R | trades |
|---:|---|---|---:|---:|---:|---:|---:|---:|---:|
| 1 | trend_following | no | 0.000000 | -1.547900 | 2.3529 | 27.7778 | 0.181564 | -0.113961 | 18 |

## Verdict

- deflated_sharpe >= 0.95: production candidate
- 0.80 <= deflated_sharpe < 0.95: needs more data
- deflated_sharpe < 0.80: no edge at this confidence

- production candidate: 0
- needs more data: 0
- no edge at this confidence: 1
