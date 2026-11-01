# Momentum study (research only)

Does monthly cross-sectional momentum (top 20 of today's S&P 500 members by past return) add
anything beyond owning these stocks, SPY or RSP after realistic eToro costs? The report is
`docs/research/momentum_study.md`.

Nothing here is imported by `app/`, nothing here imports `app`, and nothing here changes live
trading.

## Rerun

Run from the repository root, with Python 3.12 and pandas, numpy, scipy, pyarrow and yfinance.
`-I` keeps the data directories and the current directory off `sys.path`; `cli.py` adds the
repository root itself. Keep every data directory outside the repository (never commit data).

```
python -I research/momentum/cli.py data   --extra-dir EXTRA                                 # ^IRX and BIL
python -I research/momentum/cli.py run    --price-dir PRICES --extra-dir EXTRA --out OUT    # ~35 s
python -I research/momentum/report.py OUT > OUT/tables.md                                  # report tables
```

`PRICES` is the pullback study's cache (`research/pullback/cli.py data` on
`claude/wip-pullback-study`), which this study only reads:

* `prices/` holds the adjusted daily bars of the 503 members plus SPY and RSP;
* `raw/` holds the unadjusted bars, used only to find corporate-action breaks;
* `constituents/members.csv` holds sectors and "date added".

`run` writes `OUT/results.json`, with every number in the report, and `OUT/monthly.csv`, with
the monthly return series.

## Modules

| file | what |
|---|---|
| `data.py` | cache loading, the pullback study's cleaning rules, the HWM 2020-04-01 break, ^IRX/BIL download |
| `engine.py` | monthly calendar, eligibility, 12-1 / 6-1 / volatility scores, top-20 selection, the equal-weight cost-aware portfolio |
| `stats.py` | CAGR, drawdown, Sharpe, t and Newey-West t, Holm, calendar years, relative drawdown |
| `study.py` | the four pre-registered variants, random / SPY / RSP / all-members controls, tests, survivorship diagnostics, pass rule |
| `report.py` | markdown tables from `results.json` |
| `cli.py` | `data` and `run` commands |
