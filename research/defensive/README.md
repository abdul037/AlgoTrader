# Defensive trend study (research only)

Would a simple trend rule on index funds have protected capital in 2008, 2020 and 2022 at an
acceptable cost in return? The report is `docs/research/defensive_trend_study.md`.

Nothing here is imported by `app/`, and nothing here imports `app` or changes live trading.

## Rerun

From the repository root, with Python 3.12, pandas, numpy and yfinance. `-I` keeps the
downloaded data and the current directory off `sys.path`; `cli.py` adds the repository root
itself. Keep `DATA` and `OUT` outside the repository (never commit data).

```
python -I research/defensive/cli.py data  --data-dir DATA             # yfinance download (~1 min)
python -I research/defensive/cli.py check --data-dir DATA             # data-quality checks
python -I research/defensive/cli.py run   --data-dir DATA --out OUT   # everything (~10 s)
python -I research/defensive/report.py OUT > OUT/tables.md            # tables again from JSON
```

`data` stores adjusted bars (`auto_adjust=True`) in `DATA/adjusted` and unadjusted closes in
`DATA/raw`, for 2003-01-02 .. 2026-10-30 (`data.END` is exclusive). `run` writes
`OUT/results.json` and `OUT/tables.md`; the report's tables come from `tables.md`. Yahoo
revises history, so a later download can move the numbers slightly.

## Modules

| file | what |
|---|---|
| `data.py` | yfinance download; the cash series (^IRX before BIL's first day, BIL after) |
| `checks.py` | coverage, opens, largest moves, adjusted-vs-raw and cash checks (run before results) |
| `strategies.py` | target-weight schedules: D1-D4, SPY, 60/40, equal-weight five-asset |
| `backtest.py` | daily engine: next-open fills, $1 + 2 bps per order, fees out of equity |
| `metrics.py` | CAGR, drawdown, Sharpe/Sortino on monthly excess returns, episodes, block bootstrap |
| `study.py` | all runs (five cost/account settings), engine checks, verdicts, diagnostics |
| `report.py` | markdown tables from `results.json` |
