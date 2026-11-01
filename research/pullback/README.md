# Pullback study (research only)

Does the daily `ema_trend_stack` rule set (buy the dip in a rising stock) have a cost-surviving
edge over many stocks and years? The report is `docs/research/pullback_study.md`.

Nothing here is imported by `app/`, and nothing here changes live trading. The package imports
`app` read-only: the real strategy class (parity check), `BacktestEngine` and the production
walk-forward helpers (calibration).

## Rerun

From the repository root, with Python 3.12 and pandas, numpy, scipy, pyarrow, yfinance and bs4.
`-I` keeps the downloaded data and the current directory off `sys.path`; `cli.py` adds the
repository root itself. Keep `DATA` and `OUT` outside the repository (never commit data).

```
python -I research/pullback/cli.py data      --data-dir DATA            # members + prices (~2 min)
python -I research/pullback/cli.py parity    --data-dir DATA --out OUT  # vs EMATrendStackStrategy (~4 min)
python -I research/pullback/cli.py run       --data-dir DATA --out OUT  # 9 variants (~2 min)
python -I research/pullback/cli.py calibrate --data-dir DATA --out OUT  # engine parity, production, control
python -I research/pullback/report.py OUT > OUT/tables.md               # the report's tables
```

`data` stores adjusted bars in `DATA/prices`, unadjusted bars (used only to find corporate-action
breaks) in `DATA/raw`, and the member list in `DATA/constituents`. Yahoo revises history, so a
later download can move numbers slightly.

## Modules

| file | what |
|---|---|
| `data.py` | member list (Wikipedia, GitHub CSV cross-check), yfinance download, cleaning rules |
| `signals.py` | vectorised `ema_trend_stack` 1d signals (BUY with stop/target, and SELL) |
| `parity.py` | bar-for-bar check against the real strategy on rolling 400-bar windows |
| `trades.py` | per-signal trade paths for each exit rule, costs in R, one-position-per-symbol chain |
| `portfolio.py` | $100k portfolio: 0.5% risk, 25% cap, 12 slots, relative-volume tie-break, sector cap |
| `stats.py` | day- and month-clustered t, Holm, year and market-condition splits |
| `study.py` | the nine pre-registered variants, filter removed-vs-kept, survivorship sensitivity |
| `calibration.py` | engine parity, production walk-forward replication, random-entry control |
| `report.py` | markdown tables from the JSON outputs |
