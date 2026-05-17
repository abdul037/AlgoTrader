# Strategy Audit 2026-05

- Audit date: 2026-05-17T14:27:15.265378+00:00
- Window: 2024-05-18 to 2026-05-17
- Universe: top100_us (100 requested)
- Timeframe: 1h
- Data provider: alpaca
- n_trials: 3
- Walk-forward: 60/7/7/1/14 days (train/test/step/embargo/holdout)
- Cost model defaults: name=alpaca, spread_bps=2.0, extended_hours_spread_bps=10.0, overnight_fee_daily_pct=0.0, weekend_multiplier=1.0, fx_spread_bps=0.0, min_position_usd=1.0
- Registered strategies evaluated: 3 (prompt n_trials remains 3)
- Execution method: full-history warmup with entries gated to walk-forward test windows; trades are grouped by test-window entry time.

Note: strategies with `vwap` in the name are intraday by nature; 1d-bar results likely understate their real performance and are flagged below.

## Ranked Results

| rank | strategy | intraday limit | deflated_sharpe | sharpe | max_dd_pct | win_rate | profit_factor | expectancy_R | trades |
|---:|---|---|---:|---:|---:|---:|---:|---:|---:|
| 1 | intraday_vwap_trend | yes | 0.000000 | -0.229222 | 13.8605 | 40.0402 | 0.920138 | -0.004514 | 1491 |
| 2 | vwap_reclaim | yes | 0.000000 | 0.536982 | 11.6454 | 35.4402 | 1.207204 | 0.044165 | 443 |
| 3 | rsi_vwap_ema_confluence | yes | 0.000000 | 0.343430 | 10.0950 | 38.9381 | 1.231929 | 0.102607 | 113 |

## Verdict

- deflated_sharpe >= 0.95: production candidate
- 0.80 <= deflated_sharpe < 0.95: needs more data
- deflated_sharpe < 0.80: no edge at this confidence

- production candidate: 0
- needs more data: 0
- no edge at this confidence: 3

## Run Errors: intraday_vwap_trend

- NFLX: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-1951.7900000000002, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- NOW: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-1148.08, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- BKNG: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-7880.389999999999, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- PANW: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-209.34999999999994, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- LRCX: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-1414.24, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- ANET: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-512.285, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than

## Run Errors: vwap_reclaim

- NFLX: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-2025.4669999999999, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- PANW: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-212.864, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- ANET: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-543.2345, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than

## Run Errors: rsi_vwap_ema_confluence

- AVGO: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-49.911113496771634, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- NOW: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-1095.0147296640114, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- LRCX: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-329.7755565066049, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
