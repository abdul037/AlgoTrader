# Bigger-moves study (research only)

Do five daily strategies (inside_bar_narrow_range_breakout, overnight_drift,
connors_rsi2_reversion, relative_strength_momentum, mean_reversion) clear production's eToro
costs when the same signals aim for bigger moves: a target x2 or x3, no target with a 10- or
20-bar time exit, or weekly bars? The report, with the pre-registered method, is
`docs/research/bigger_moves_study.md`.

Nothing here is imported by `app/`, and nothing here changes live trading. The package imports
`app` read-only (the strategy classes, `BacktestEngine`, the cost model and the walk-forward
helpers) and reuses `research/pullback` (data cleaning, clustered t, Holm) and
`research/momentum` (the HWM break).

## Rerun

From the repository root, with Python 3.12 and pandas, numpy, scipy, pyarrow (yfinance and bs4
only to rebuild the cache). `-I` keeps the downloaded data and the current directory off
`sys.path`; `cli.py` adds the repository root itself. Keep `DATA` and `OUT` outside the
repository (never commit data).

```
python -I research/pullback/cli.py data       --data-dir DATA            # the price cache (pullback study)
python -I research/bigger_moves/cli.py parity    --data-dir DATA --out OUT  # vs the real classes and engine (~10 min)
python -I research/bigger_moves/cli.py run       --data-dir DATA --out OUT  # main period, 2011-01 .. 2025-10
python -I research/bigger_moves/cli.py holdout   --data-dir DATA --out OUT  # sealed last 12 months (after run)
python -I research/bigger_moves/cli.py calibrate --data-dir DATA --out OUT  # V0 vs production's walk-forward
python -I research/bigger_moves/report.py OUT > OUT/tables.md              # the report's tables
```

`DATA` is the pullback study's cache: `prices/` (yfinance `auto_adjust=True` daily bars),
`raw/` (the same bars unadjusted, `auto_adjust=False`, used only to find corporate-action
breaks) and `constituents/members.csv` (today's members with "date added"). The pullback
`data` command writes `prices/` and `constituents/`; `raw/` is the same download with
`auto_adjust=False`. The study used the cache downloaded on 2026-11-01 (last bar 2026-10-30).
Yahoo revises history, so a later download can move numbers slightly.

`run` writes `OUT/trades_main.parquet` (every trade with its matched random-control mean) and
`OUT/results_main.json`; `holdout` writes `OUT/trades_holdout.parquet` and
`OUT/results_holdout.json` with the final verdicts.

## Modules

| file | what |
|---|---|
| `data.py` | cache loading, segments split at corporate-action breaks, weekly bars, periods, the live 24 names |
| `signals.py` | vectorised signals of the five classes on the live window (400 daily / 520 weekly bars, exact EMA seed correction) |
| `variants.py` | the 29-variant grid, the signal transform per variant, a wrapper applying it to the real class |
| `paths.py` | trade paths under the engine's rules, the production cost model in R, the one-position chain, the random-entry control |
| `parity.py` | signals vs the real classes on live windows; trade paths vs the real `BacktestEngine` |
| `study.py` | per-symbol simulation (point-in-time and unrestricted), statistics, Holm, the pass rule |
| `calibration.py` | production's walk-forward replicated with the real engine on the live 24 names, next to this study's V0 |
| `report.py` | markdown tables from the JSON outputs |
