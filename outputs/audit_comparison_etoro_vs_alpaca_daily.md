# Audit Comparison: Baseline vs Candidate Cost Model

- Baseline: outputs/strategy_audit_2026_05.json
- Candidate: outputs/strategy_audit_2026_05_alpaca_daily.json

Sorted by sharpe_delta descending. Positive delta means strategy improved under candidate cost model.

| strategy | base_sharpe | cand_sharpe | delta_sharpe | base_deflated | cand_deflated | delta_deflated | base_verdict | cand_verdict | changed |
|---|---:|---:|---:|---:|---:|---:|---|---|:-:|
| ma_crossover | 0.2457 | 0.6728 | +0.4271 | 0.0000 | 0.0000 | +0.0000 | no edge at this confidence | no edge at this confidence |  |
| ema_trend_stack | 0.3245 | 0.7112 | +0.3867 | 0.0000 | 0.0000 | +0.0000 | no edge at this confidence | no edge at this confidence |  |
| gold_momentum | 0.1015 | 0.4529 | +0.3514 | 0.0000 | 0.0000 | +0.0000 | no edge at this confidence | no edge at this confidence |  |
| trend_following | 0.4517 | 0.7395 | +0.2878 | 0.0000 | 0.0000 | +0.0000 | no edge at this confidence | no edge at this confidence |  |
| momentum_breakout | 0.3050 | 0.5428 | +0.2378 | 0.0000 | 0.0000 | +0.0000 | no edge at this confidence | no edge at this confidence |  |
| vwap_reclaim | 0.0000 | 0.1699 | +0.1699 | 0.0000 | 0.0000 | +0.0000 | no edge at this confidence | no edge at this confidence |  |
| mean_reversion | 0.0184 | 0.1271 | +0.1087 | 0.0000 | 0.0000 | +0.0000 | no edge at this confidence | no edge at this confidence |  |
| pullback_trend | 0.3951 | 0.4353 | +0.0403 | 0.0000 | 0.0000 | +0.0000 | no edge at this confidence | no edge at this confidence |  |
| intraday_vwap_trend | 0.5241 | 0.5612 | +0.0372 | 0.0000 | 0.0000 | +0.0000 | no edge at this confidence | no edge at this confidence |  |
| rsi_vwap_ema_confluence | 0.1299 | 0.1426 | +0.0128 | 0.0000 | 0.0000 | +0.0000 | no edge at this confidence | no edge at this confidence |  |
| rsi_reversal | 0.0900 | -0.0321 | -0.1222 | 0.0000 | 0.0000 | +0.0000 | no edge at this confidence | no edge at this confidence |  |
| rsi_trend_continuation | 0.1433 | -0.0637 | -0.2069 | 0.0000 | 0.0000 | +0.0000 | no edge at this confidence | no edge at this confidence |  |

## Summary

- Verdict changes: 0 of 12 strategies
- Newly production candidate: 0
- Newly no edge at this confidence: 0
- Largest sharpe improvement: ma_crossover (+0.4271)
- Largest sharpe degradation: rsi_trend_continuation (-0.2069)