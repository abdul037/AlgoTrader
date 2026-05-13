# Strategy Audit 2026-05

- Audit date: 2026-05-13T18:39:23.576276+00:00
- Window: 2020-01-01 to 2024-12-31
- Universe: top100_us (100 requested)
- Timeframe: 1d
- n_trials: 12
- Walk-forward: 180/14/14/1/28 days (train/test/step/embargo/holdout)
- Cost model defaults: spread_bps=10.0, extended_hours_spread_bps=30.0, overnight_fee_daily_pct=0.00015, weekend_multiplier=3.0, fx_spread_bps=0.0, min_position_usd=50.0
- Registered strategies evaluated: 12 (prompt n_trials remains 12)
- Execution method: full-history warmup with entries gated to walk-forward test windows; trades are grouped by test-window entry time.

Note: strategies with `vwap` in the name are intraday by nature; 1d-bar results likely understate their real performance and are flagged below.

## Ranked Results

| rank | strategy | intraday limit | deflated_sharpe | sharpe | max_dd_pct | win_rate | profit_factor | expectancy_R | trades |
|---:|---|---|---:|---:|---:|---:|---:|---:|---:|
| 1 | ema_trend_stack | no | 0.000000 | 0.324499 | 6.1245 | 35.9091 | 1.585631 | 0.066108 | 440 |
| 2 | gold_momentum | no | 0.000000 | 0.101520 | 9.0983 | 37.3171 | 1.14984 | 0.016625 | 410 |
| 3 | intraday_vwap_trend | yes | 0.000000 | 0.524055 | 4.0575 | 41.7143 | 2.121955 | 0.118321 | 350 |
| 4 | ma_crossover | no | 0.000000 | 0.245741 | 5.1653 | 39.9510 | 1.414846 | 0.041645 | 408 |
| 5 | mean_reversion | no | 0.000000 | 0.018392 | 14.1959 | 40.0966 | 1.024266 | 0.008510 | 207 |
| 6 | momentum_breakout | no | 0.000000 | 0.304991 | 14.4996 | 30.1435 | 1.395681 | 0.046860 | 836 |
| 7 | pullback_trend | no | 0.000000 | 0.395074 | 3.0634 | 35.8663 | 1.88907 | 0.053593 | 329 |
| 8 | rsi_reversal | no | 0.000000 | 0.090043 | 9.3927 | 43.2161 | 1.132209 | 0.042214 | 199 |
| 9 | rsi_trend_continuation | no | 0.000000 | 0.143277 | 9.8637 | 38.0952 | 1.242466 | 0.098311 | 147 |
| 10 | rsi_vwap_ema_confluence | yes | 0.000000 | 0.129858 | 11.7426 | 53.0000 | 1.24069 | 0.107931 | 100 |
| 11 | trend_following | no | 0.000000 | 0.451719 | 6.6439 | 40.2044 | 1.720895 | 0.086836 | 587 |
| 12 | vwap_reclaim | yes | 0.000000 | 0.000000 | 0.0000 | 0.0000 | 0.0 | 0.000000 | 0 |

## Verdict

- deflated_sharpe >= 0.95: production candidate
- 0.80 <= deflated_sharpe < 0.95: needs more data
- deflated_sharpe < 0.80: no edge at this confidence

- production candidate: 0
- needs more data: 0
- no edge at this confidence: 12

## Data Fetch Errors

- MMC: failed to fetch MMC: yfinance returned no rows

## Run Errors: intraday_vwap_trend

- META: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-5.080009460449219, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- NFLX: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-5.851001739501953, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- PLTR: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-0.07999897003173828, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- INTC: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-2.020000457763672, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- SCHW: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-0.6300010681152344, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- CRWD: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-12.180023193359375, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- SNOW: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-4.5000152587890625, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- MELI: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-61.94000244140625, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- PYPL: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-6.529991149902344, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than

## Run Errors: rsi_vwap_ema_confluence

- DIS: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-10.84040771479431, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- UBER: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-23.90241400647072, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- PLTR: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-18.990697491859375, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- SHOP: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-3.8903586489176973, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- SNOW: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-65.01432780540011, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- NKE: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-12.42575133975103, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
