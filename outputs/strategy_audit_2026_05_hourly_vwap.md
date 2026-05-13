# Strategy Audit 2026-05

- Audit date: 2026-05-13T20:36:07.292538+00:00
- Window: 2024-05-14 to 2026-05-13
- Universe: top100_us (100 requested)
- Timeframe: 1h
- n_trials: 3
- Walk-forward: 60/7/7/1/14 days (train/test/step/embargo/holdout)
- Cost model defaults: spread_bps=10.0, extended_hours_spread_bps=30.0, overnight_fee_daily_pct=0.00015, weekend_multiplier=3.0, fx_spread_bps=0.0, min_position_usd=50.0
- Registered strategies evaluated: 3 (prompt n_trials remains 3)
- Execution method: full-history warmup with entries gated to walk-forward test windows; trades are grouped by test-window entry time.

Note: strategies with `vwap` in the name are intraday by nature; 1d-bar results likely understate their real performance and are flagged below.

## Ranked Results

| rank | strategy | intraday limit | deflated_sharpe | sharpe | max_dd_pct | win_rate | profit_factor | expectancy_R | trades |
|---:|---|---|---:|---:|---:|---:|---:|---:|---:|
| 1 | intraday_vwap_trend | yes | 0.000000 | 0.395612 | 11.3943 | 31.8000 | 1.096908 | 0.008006 | 1000 |
| 2 | vwap_reclaim | yes | 0.000000 | 0.086610 | 15.1798 | 32.8321 | 1.022022 | 0.005416 | 399 |
| 3 | rsi_vwap_ema_confluence | yes | 0.000000 | -0.582394 | 15.6303 | 35.9375 | 0.819085 | -0.087811 | 128 |

## Verdict

- deflated_sharpe >= 0.95: production candidate
- 0.80 <= deflated_sharpe < 0.95: needs more data
- deflated_sharpe < 0.80: no edge at this confidence

- production candidate: 0
- needs more data: 0
- no edge at this confidence: 3

## Data Fetch Errors

- MMC: failed to fetch MMC: yfinance returned no rows

## Run Errors: rsi_vwap_ema_confluence

- UNH: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-70.47626596358094, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- AMD: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-50.166931493700815, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- PYPL: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-31.882573025910716, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
