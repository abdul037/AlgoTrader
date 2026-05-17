# Strategy Audit 2026-05

- Audit date: 2026-05-17T14:14:51.165668+00:00
- Window: 2020-01-01 to 2024-12-31
- Universe: top100_us (100 requested)
- Timeframe: 1d
- Data provider: alpaca
- n_trials: 12
- Walk-forward: 180/14/14/1/28 days (train/test/step/embargo/holdout)
- Cost model defaults: name=alpaca, spread_bps=2.0, extended_hours_spread_bps=10.0, overnight_fee_daily_pct=0.0, weekend_multiplier=1.0, fx_spread_bps=0.0, min_position_usd=1.0
- Registered strategies evaluated: 12 (prompt n_trials remains 12)
- Execution method: full-history warmup with entries gated to walk-forward test windows; trades are grouped by test-window entry time.

Note: strategies with `vwap` in the name are intraday by nature; 1d-bar results likely understate their real performance and are flagged below.

## Ranked Results

| rank | strategy | intraday limit | deflated_sharpe | sharpe | max_dd_pct | win_rate | profit_factor | expectancy_R | trades |
|---:|---|---|---:|---:|---:|---:|---:|---:|---:|
| 1 | ema_trend_stack | no | 0.000000 | 0.711206 | 2.6076 | 36.1065 | 2.722394 | 0.101865 | 601 |
| 2 | gold_momentum | no | 0.000000 | 0.452897 | 3.9158 | 45.6036 | 1.798799 | 0.044327 | 671 |
| 3 | intraday_vwap_trend | yes | 0.000000 | 0.561240 | 4.5909 | 42.8571 | 2.121246 | 0.075452 | 490 |
| 4 | ma_crossover | no | 0.000000 | 0.672826 | 4.9384 | 46.6129 | 2.487105 | 0.078895 | 620 |
| 5 | mean_reversion | no | 0.000000 | 0.127133 | 13.7218 | 38.2857 | 1.181915 | 0.066932 | 175 |
| 6 | momentum_breakout | no | 0.000000 | 0.542791 | 7.4945 | 31.3788 | 1.729618 | 0.059809 | 1262 |
| 7 | pullback_trend | no | 0.000000 | 0.435348 | 3.6195 | 37.0642 | 1.955729 | 0.046483 | 545 |
| 8 | rsi_reversal | no | 0.000000 | -0.032122 | 13.9391 | 41.3333 | 0.955019 | -0.014996 | 225 |
| 9 | rsi_trend_continuation | no | 0.000000 | -0.063655 | 23.0990 | 35.8779 | 0.907032 | -0.047115 | 131 |
| 10 | rsi_vwap_ema_confluence | yes | 0.000000 | 0.142641 | 6.7148 | 45.2381 | 1.292966 | 0.137199 | 84 |
| 11 | trend_following | no | 0.000000 | 0.739493 | 4.5324 | 31.3797 | 2.317246 | 0.095641 | 819 |
| 12 | vwap_reclaim | yes | 0.000000 | 0.169870 | 9.6258 | 38.6100 | 1.244691 | 0.050008 | 259 |

## Verdict

- deflated_sharpe >= 0.95: production candidate
- 0.80 <= deflated_sharpe < 0.95: needs more data
- deflated_sharpe < 0.80: no edge at this confidence

- production candidate: 0
- needs more data: 0
- no edge at this confidence: 12

## Run Errors: ema_trend_stack

- NVDA: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-1239.974, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- AMZN: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-5127.2480000000005, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- GOOGL: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-194.12065974839902, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- GOOG: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-4942.336, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- AVGO: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-3343.520000000001, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- TSLA: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-1128.7200000000003, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- WMT: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-207.88400000000001, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- PANW: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-673.81, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- LRCX: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-1638.893, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- ANET: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-753.4060000000002, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- SHOP: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-25.74826974933673, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- APH: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-76.02000000000001, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than

## Run Errors: intraday_vwap_trend

- NVDA: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-1084.3149999999998, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- AMZN: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-4649.79, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- GOOGL: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-4457.87, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- GOOG: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-4483.04, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- META: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-5.180000000000007, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- AVGO: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-3023.9750000000004, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- TSLA: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-999.1899999999999, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- WMT: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-183.57, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- NFLX: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-59.005000000000024, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- ISRG: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-1111.03, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- PLTR: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-0.07500000000000107, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- INTC: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-1.9999999999999964, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- SCHW: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-0.31500000000001904, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- PANW: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-592.6999999999999, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- LRCX: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-1482.53, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- ANET: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-672.8650000000001, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- CRWD: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-10.97999999999999, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- SHOP: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-689.87, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- SNOW: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-8.275000000000006, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- MELI: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-61.50999999999988, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- ... 2 more

## Run Errors: momentum_breakout

- NVDA: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-7.0321500000000015, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- AMZN: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-148.2568, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- GOOGL: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-100.80774999999964, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- GOOG: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-92.86640000000004, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- SHOP: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-41.59659999999999, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than

## Run Errors: rsi_vwap_ema_confluence

- NVDA: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-107.58001129638589, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- GOOGL: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-455.0819458897258, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- CRM: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-11.13708602844406, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- UBER: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-5.79381234674204, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- ISRG: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-448.9652296070948, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- PLTR: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-16.289971349541254, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- ANET: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-132.68152196136427, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- SHOP: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-410.55162445739944, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- SNOW: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-52.59678897748037, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- PYPL: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-215.8446895898985, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- NKE: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-24.752602168069956, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than

## Run Errors: trend_following

- NVDA: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-60.507918490479, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- AMZN: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-4649.79, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- GOOGL: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-167.84332704399907, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- GOOG: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-170.497339743102, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- AVGO: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-122.50303644337811, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- WMT: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-183.57, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- PANW: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-592.6999999999999, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- LRCX: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-1460.51, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- SHOP: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-20.676608863033387, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than

## Run Errors: vwap_reclaim

- NVDA: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-1088.5085, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- GOOGL: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-4453.494, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- GOOG: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-4530.566985955452, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- AVGO: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-3177.5000000000005, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- TSLA: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-994.487, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- WMT: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-188.34550000000004, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- ISRG: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-1116.914, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- PANW: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-631.4399999999999, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- LRCX: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-1492.1360000000002, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- ANET: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-709.7335000000002, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
- APH: 1 validation error for Signal
take_profit
  Input should be greater than 0 [type=greater_than, input_value=-83.70700000000001, input_type=float]
    For further information visit https://errors.pydantic.dev/2.12/v/greater_than
