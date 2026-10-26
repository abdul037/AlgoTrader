# Intraday edge study (2026-10-26) -- research only

**Question.** Is there any intraday strategy with a real edge after honest costs that this bot
could trade? Production's Stage 1 evidence has 29 intraday keys and none pass; live tracked
signals earn about +0.06..+0.12R before costs, ~0 after Alpaca costs and less under stress.

**Answer: no intraday edge found.** Seven intraday-native candidates (opening-range breakout
15/30 min, overnight gap go and gap fade, relative-volume momentum, VWAP 2-sigma reversion,
late-day momentum) were judged exactly as production judges a live 5m/10m/15m key, on the live
24-name universe and a 20-name high-beta universe. **0 of 45 long cells clear the production
gate** (`passed` and the Alpaca and stress profiles, or the option-B paper OOS bar). A two-year
1h check of the five that work on hourly bars passes 0 of 10 long cells. The best-looking long
results are consistent with chance across 45 tries: no day-clustered t-stat under stress costs
exceeds +0.8, against ~3.0 needed for one-in-45 significance.

Nothing here touches live trading: the candidates live in `app/research/intraday/`, are not in
`STRATEGY_REGISTRY` or `STRATEGY_SPECS` (the registry has no research-only flag that keeps a key
out of the live scans), and no live module imports them.

## Data

| | source | window | symbols |
|---|---|---|---|
| 5m (and 10m/15m resampled from it, anchored at 09:30 ET) | yfinance, keyless, `prepost=False`, `auto_adjust=False` | 60 sessions, 2026-07-31 .. 2026-10-23 | 41 |
| 1h (robustness) | yfinance, same options | ~725 sessions, 2023-11-27 .. 2026-10-23 | 41 |

* **live24**: AAPL ADBE AMD AMZN AVGO BAC COST CRM CSCO DIS GOOGL INTC IWM JPM META MSFT NFLX NVDA
  PYPL QQQ SPY TSLA WMT XOM (the live scan universe).
* **volatile20**: TSLA NVDA AMD COIN MSTR PLTR SMCI SHOP ROKU MARA RIOT UBER ABNB SNOW CRWD NET DKNG
  AFRM SOFI RIVN (all had full data; TSLA/NVDA/AMD are in both).
* **etf3**: SPY QQQ IWM, the pre-declared universe for VWAP reversion.

Quality: 60/60 sessions and 78 bars a session for every symbol (ROKU missing one bar); no OHLC
inconsistencies; ~0.2% of 5m bars carry zero volume (a yfinance artefact, mid-session; prices
are intact, so VWAP and relative volume are marginally affected). Regular hours only. The data
is not Alpaca's IEX feed the bot sees, and 60 days is half production's 120-day window.

Market backdrop: the intraday drift (09:40 open to 15:50 open, average over symbol-days) was
+4.4 bps (live24) and +7.8 bps (volatile20) a day over the OOS folds, and -15.3 / -39.2 bps a
day over the 10-session holdout (2026-10-12 .. 10-23): the holdout was a hostile window for
longs.

## How each candidate was judged

Exactly the production path for a 5m/10m/15m equity key, called in-process on the downloaded
bars (`app/research/intraday/harness.py`):

* **Walk-forward**: `BatchBacktestService._run_strategy` itself, with the deployed intraday plan
  (`splitter_for`: 5-day warm-up, 7-day test folds stepping 7 days, 1-day embargo, last 14 days
  sealed as holdout). On 60 days that is **9 folds** per symbol (production's 120-day Alpaca
  window gives ~13) and a 10-session holdout.
* **Engine config**: what `BatchBacktestService.run` builds for a minute-bar key: $10,000, 1%
  risk ($100 = 1 R; notional capped at $10,000), session flatten, no stop floor,
  `entry_delay_bars=1` (a signal on bar N's close fills at bar N+2's open), never across a
  session close. Long only, as the evidence engine and the bot trade.
* **Costs**: the eToro fee-inclusive model every row is priced with (10 bps spread + $1 a leg),
  and the two venue profiles the paper gate also requires, replayed on the same signals through
  `RecordedSignals`: **Alpaca** (~4.3 bps a round trip) and **stress** (~12.3 bps). A zero-cost
  "gross" replay is shown for diagnosis only; it never votes.
* **Verdict**: the symbols' rows are pooled by `compute_verdicts` (the production function, over
  a private in-memory table): >= 40 pooled OOS trades and pooled OOS expectancy > 0, >= 10
  holdout trades with holdout expectancy > 0, under eToro costs (`passed`) and under both
  Alpaca and stress (`paper_passed`); "OOS bar only" is option B's admission (OOS parts of all
  three, holdout ignored). R = pooled expectancy in $ / 100.
* **Statistics** the gate does not compute: the t-stat of daily summed R under stress costs
  ("stress day-t"; trades on one day are not independent across symbols) and its one-sided
  p-value, Holm- and BH-adjusted over the cells tried.

Two conservative choices inside the candidates (not changes to the engine):

1. Every signal carries `max_hold_bars`, so the engine's time stop exits at the open of the
   15:50 ET bar (15:45 on 15m bars): the live bot flattens 10 minutes before the bell.
2. A gap or a day change is measured only against an **adjacent** prior session (the previous
   weekday). The walk-forward drops the embargo day between warm-up and test, so on each fold's
   first test day the frame's "previous session" is two sessions back.

## The candidates (parameters fixed before any backtest ran: commit 79bde7a)

All are session-native (New York session dates, regular-hours bars only, time-of-day relative
volume, session VWAP on the typical price), take at most one setup per session (VWAP reversion:
one position at a time), enter on a bracketed signal at a bar's close and leave by the 15:50
time stop if neither bracket leg fills.

| variant | rule (long) | stop | target |
|---|---|---|---|
| `research_orb30` | first close above the 09:30-10:00 range high before 12:00; opening-range relative volume >= 1.0 | range low | 2R |
| `research_orb15` | the same with the 09:30-09:45 range | range low | 2R |
| `research_gap_go` | gap up >= 1% vs the adjacent prior close and the first regular bar closes green (signal at its close) | first bar low | 2R |
| `research_gap_fade` | gap down >= 1% and the first bar closes green | first bar low | prior close (gap fill), >= 1R |
| `research_rvol_momentum` | 10:00-14:00: first close at a new session high with time-of-day cumulative relative volume >= 1.5, up >= 1% on the day, above VWAP | VWAP (>= 0.25% away) | 2R |
| `research_vwap_reversion` | 10:00-15:00: the bar's low <= VWAP - 2 session sigma and it closes green | as far below as the target is above | VWAP |
| `research_late_day_momentum` | the bar that fills at 15:00: up >= 0.5% on the day and above VWAP | VWAP (>= 0.25% away) | 3R (the 15:50 exit usually binds) |

Shorts (a diagnostic: the bot does not short) are the exact mirror image, computed on negated
prices.

## Results: 60 days, long (what the bot could trade)

"fail" lists the eToro-priced reasons; the Alpaca/stress columns show where only the cheaper
venue would have been positive.

| strategy | tf | universe | OOS trades | win % | gross R | eToro R | Alpaca R | stress R | stress day-t | holdout trades | holdout Alpaca R | holdout stress R | production gate |
|---|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|
| orb30 | 5m | live24 | 156 | 38 | -0.045 | -0.128 | -0.073 | -0.124 | -2.29 | 35 | +0.129 | +0.073 | fail: OOS<=0 |
| orb30 | 10m | live24 | 138 | 43 | -0.009 | -0.090 | -0.036 | -0.085 | -1.55 | 35 | +0.078 | +0.024 | fail: OOS<=0 |
| orb30 | 15m | live24 | 137 | 40 | -0.011 | -0.094 | -0.039 | -0.089 | -1.80 | 33 | +0.122 | +0.068 | fail: OOS<=0 |
| orb30 | 5m | volatile20 | 125 | 46 | +0.024 | -0.032 | +0.008 | -0.021 | -0.28 | 29 | +0.273 | +0.234 | fail: OOS<=0 |
| orb30 | 10m | volatile20 | 117 | 51 | +0.023 | -0.032 | +0.008 | -0.020 | -0.32 | 29 | +0.236 | +0.197 | fail: OOS<=0 |
| orb30 | 15m | volatile20 | 115 | 51 | +0.033 | -0.021 | +0.018 | -0.010 | -0.15 | 28 | +0.261 | +0.222 | fail: OOS<=0 |
| orb15 | 5m | live24 | 172 | 44 | +0.011 | -0.077 | -0.019 | -0.074 | -1.37 | 50 | +0.052 | -0.005 | fail: holdout<=0, OOS<=0 |
| orb15 | 10m | live24 | 142 | 44 | -0.009 | -0.094 | -0.037 | -0.090 | -1.62 | 39 | +0.070 | +0.014 | fail: OOS<=0 |
| orb15 | 15m | live24 | 156 | 45 | -0.008 | -0.094 | -0.037 | -0.091 | -1.68 | 44 | +0.180 | +0.122 | fail: OOS<=0 |
| orb15 | 5m | volatile20 | 147 | 47 | +0.055 | -0.009 | +0.036 | +0.001 | +0.01 | 33 | +0.266 | +0.223 | fail: OOS<=0 |
| orb15 | 10m | volatile20 | 129 | 49 | +0.018 | -0.042 | +0.001 | -0.032 | -0.49 | 33 | +0.127 | +0.084 | fail: OOS<=0 |
| orb15 | 15m | volatile20 | 131 | 48 | +0.035 | -0.027 | +0.017 | -0.017 | -0.30 | 32 | +0.179 | +0.137 | fail: OOS<=0 |
| gap_go | 5m | live24 | 48 | 27 | -0.125 | -0.226 | -0.161 | -0.227 | -1.76 | 19 | +0.076 | +0.005 | fail: OOS<=0 |
| gap_go | 10m | live24 | 41 | 41 | -0.004 | -0.099 | -0.037 | -0.096 | -0.80 | 15 | +0.099 | +0.030 | fail: OOS<=0 |
| gap_go | 15m | live24 | 46 | 41 | -0.060 | -0.148 | -0.090 | -0.146 | -1.21 | 9 | +0.360 | +0.300 | fail: OOS<=0, holdout n<10 |
| gap_go | 5m | volatile20 | 71 | 45 | +0.052 | -0.032 | +0.024 | -0.026 | -0.18 | 10 | -0.139 | -0.206 | fail: holdout<=0, OOS<=0 |
| gap_go | 10m | volatile20 | 68 | 44 | -0.043 | -0.113 | -0.065 | -0.107 | -0.86 | 10 | +0.277 | +0.223 | fail: OOS<=0 |
| gap_go | 15m | volatile20 | 76 | 32 | -0.174 | -0.241 | -0.196 | -0.235 | -1.88 | 9 | +0.360 | +0.298 | fail: OOS<=0, holdout n<10 |
| gap_fade | 5m | live24 | 31 | 45 | +0.147 | +0.034 | +0.108 | +0.035 | +0.31 | 17 | -0.296 | -0.361 | fail: holdout<=0, n<40 |
| gap_fade | 10m | live24 | 19 | 32 | -0.064 | -0.163 | -0.098 | -0.162 | -1.66 | 26 | -0.311 | -0.381 | fail: holdout<=0, OOS<=0, n<40 |
| gap_fade | 15m | live24 | 19 | 37 | +0.143 | +0.036 | +0.106 | +0.038 | +0.46 | 21 | -0.145 | -0.210 | fail: holdout<=0, n<40 |
| gap_fade | 5m | volatile20 | 37 | 22 | -0.141 | -0.236 | -0.174 | -0.236 | -1.10 | 18 | -0.282 | -0.339 | fail: holdout<=0, OOS<=0, n<40 |
| gap_fade | 10m | volatile20 | 24 | 25 | +0.010 | -0.087 | -0.024 | -0.085 | -0.44 | 25 | -0.177 | -0.240 | fail: holdout<=0, OOS<=0, n<40 |
| gap_fade | 15m | volatile20 | 23 | 30 | +0.110 | +0.007 | +0.074 | +0.008 | +0.04 | 23 | +0.245 | +0.187 | fail: n<40 |
| rvol_momentum | 5m | live24 | 36 | 56 | +0.146 | +0.039 | +0.109 | +0.041 | +0.28 | 9 | -0.106 | -0.182 | fail: holdout n<10, n<40 |
| rvol_momentum | 10m | live24 | 31 | 52 | +0.185 | +0.079 | +0.148 | +0.080 | +0.43 | 9 | -0.069 | -0.149 | fail: holdout n<10, n<40 |
| rvol_momentum | 15m | live24 | 30 | 57 | +0.226 | +0.118 | +0.189 | +0.121 | +0.77 | 8 | -0.136 | -0.217 | fail: holdout n<10, n<40 |
| rvol_momentum | 5m | volatile20 | 32 | 44 | +0.107 | +0.033 | +0.084 | +0.042 | +0.23 | 6 | +0.160 | +0.087 | fail: holdout n<10, n<40 |
| rvol_momentum | 10m | volatile20 | 31 | 48 | +0.086 | +0.015 | +0.064 | +0.023 | +0.13 | 5 | +0.214 | +0.146 | fail: holdout n<10, n<40 |
| rvol_momentum | 15m | volatile20 | 33 | 52 | +0.175 | +0.090 | +0.147 | +0.096 | +0.54 | 6 | -0.104 | -0.161 | fail: holdout n<10, n<40 |
| vwap_reversion | 5m | live24 | 632 | 47 | +0.064 | -0.054 | +0.021 | -0.057 | -2.81 | 178 | -0.214 | -0.287 | fail: holdout<=0, OOS<=0 |
| vwap_reversion | 10m | live24 | 529 | 38 | +0.010 | -0.107 | -0.032 | -0.109 | -5.46 | 146 | -0.227 | -0.301 | fail: holdout<=0, OOS<=0 |
| vwap_reversion | 15m | live24 | 436 | 34 | +0.007 | -0.111 | -0.035 | -0.113 | -4.68 | 114 | -0.196 | -0.271 | fail: holdout<=0, OOS<=0 |
| vwap_reversion | 5m | volatile20 | 540 | 44 | +0.017 | -0.091 | -0.022 | -0.091 | -3.07 | 148 | -0.339 | -0.400 | fail: holdout<=0, OOS<=0 |
| vwap_reversion | 10m | volatile20 | 455 | 39 | -0.009 | -0.118 | -0.048 | -0.120 | -4.44 | 138 | -0.289 | -0.351 | fail: holdout<=0, OOS<=0 |
| vwap_reversion | 15m | volatile20 | 426 | 32 | -0.059 | -0.168 | -0.098 | -0.170 | -5.07 | 109 | -0.245 | -0.314 | fail: holdout<=0, OOS<=0 |
| late_day_momentum | 5m | live24 | 204 | 31 | -0.009 | -0.127 | -0.051 | -0.129 | -4.46 | 51 | +0.031 | -0.048 | fail: holdout<=0, OOS<=0 |
| late_day_momentum | 10m | live24 | 208 | 31 | +0.000 | -0.117 | -0.042 | -0.120 | -4.13 | 46 | +0.009 | -0.070 | fail: holdout<=0, OOS<=0 |
| late_day_momentum | 15m | live24 | 205 | 34 | +0.008 | -0.109 | -0.034 | -0.112 | -4.71 | 51 | +0.005 | -0.074 | fail: holdout<=0, OOS<=0 |
| late_day_momentum | 5m | volatile20 | 172 | 37 | -0.016 | -0.122 | -0.054 | -0.123 | -2.12 | 46 | +0.027 | -0.040 | fail: holdout<=0, OOS<=0 |
| late_day_momentum | 10m | volatile20 | 178 | 36 | -0.011 | -0.116 | -0.048 | -0.117 | -2.08 | 45 | +0.045 | -0.022 | fail: holdout<=0, OOS<=0 |
| late_day_momentum | 15m | volatile20 | 179 | 34 | +0.005 | -0.101 | -0.032 | -0.101 | -1.91 | 41 | +0.009 | -0.058 | fail: holdout<=0, OOS<=0 |

VWAP reversion on its pre-declared ETF universe:

| strategy | tf | universe | OOS trades | win % | gross R | eToro R | Alpaca R | stress R | stress day-t | holdout trades | holdout Alpaca R | holdout stress R | production gate |
|---|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|
| vwap_reversion | 5m | etf3 | 72 | 42 | +0.036 | -0.084 | -0.007 | -0.087 | -2.60 | 18 | -0.272 | -0.349 | fail: holdout<=0, OOS<=0 |
| vwap_reversion | 10m | etf3 | 43 | 26 | +0.017 | -0.102 | -0.025 | -0.105 | -3.50 | 12 | -0.252 | -0.331 | fail: holdout<=0, OOS<=0 |
| vwap_reversion | 15m | etf3 | 34 | 15 | -0.025 | -0.145 | -0.068 | -0.147 | -4.66 | 9 | -0.341 | -0.420 | fail: OOS<=0, holdout n<10, n<40 |

### Reading the table

* **Costs, not signals, decide it.** 25 of 42 long cells are positive at zero cost, 17 after
  Alpaca costs, 10 after stress costs, 9 after eToro costs. The typical gross edge is
  +0.01..+0.05R a trade. Costs take a median 0.037R a trade on live24 under Alpaca, 0.105R
  under stress and 0.107R under eToro (0.028 / 0.078 / 0.084R on volatile20, whose wider stops
  mean smaller positions per R). Frequent setups (VWAP reversion, late-day momentum: 170-630
  trades a cell) are clearly negative net (stress day-t -1.9 to -5.5).
* **Relative-volume momentum** is the only family positive under all three cost models in all
  six cells (stress +0.02..+0.12R), but with 30-36 OOS trades a cell (below the 40-trade bar),
  5-9 holdout trades (below 10), a negative live24 holdout, and stress day-t <= +0.77. Over two
  years of 1h bars it is negative on live24 (-0.12R eToro, day-t -3.5) and flat on volatile20.
* **ORB on high-beta names** is the near miss: positive after Alpaca costs on all six
  volatile20 cells and positive in the holdout, but ~0 under stress (-0.03..+0.001R) and
  negative under eToro costs, so it fails `passed`. On the mega-cap live24 universe ORB is
  negative after costs in every cell.
* **Gap strategies** have too few trades (19-76 a cell) to say anything; gap fade's holdout is
  negative on 5 of 6 cells.

## Short-side diagnostic (not tradeable: the bot and the eToro mirror are long-only)

Mirrored shorts pass the gate in 2 of 42 cells (`gap_go` 10m and `orb15` 15m, both volatile20,
stress day-t +0.84 and +0.58); `gap_go` 5m and 15m volatile20 pass the OOS bar only (15m: the
largest t in the study, +1.86). Neither family replicates over two years of 1h bars (gap-go
short volatile20: -0.08R stress, day-t -2.1; first-hour ORB short volatile20: -0.01R, day-t
-0.6), and shorting gap-downs would also meet Reg SHO's short-sale restriction on -10% days
and borrow limits on names like MARA, RIOT and SOFI.

<details><summary>All 42 short cells (60 days)</summary>

| strategy | tf | universe | OOS trades | win % | gross R | eToro R | Alpaca R | stress R | stress day-t | holdout trades | holdout Alpaca R | holdout stress R | production gate |
|---|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|
| orb30 | 5m | live24 | 130 | 28 | -0.127 | -0.217 | -0.158 | -0.214 | -4.22 | 39 | +0.278 | +0.220 | fail: OOS<=0 |
| orb30 | 10m | live24 | 115 | 26 | -0.143 | -0.231 | -0.173 | -0.228 | -4.03 | 37 | +0.289 | +0.232 | fail: OOS<=0 |
| orb30 | 15m | live24 | 114 | 27 | -0.128 | -0.219 | -0.159 | -0.216 | -3.98 | 35 | +0.334 | +0.275 | fail: OOS<=0 |
| orb30 | 5m | volatile20 | 104 | 47 | +0.049 | -0.008 | +0.033 | +0.004 | +0.06 | 25 | +0.413 | +0.379 | fail: OOS<=0 |
| orb30 | 10m | volatile20 | 99 | 43 | -0.028 | -0.082 | -0.043 | -0.071 | -1.38 | 23 | +0.345 | +0.312 | fail: OOS<=0 |
| orb30 | 15m | volatile20 | 94 | 41 | -0.010 | -0.066 | -0.026 | -0.055 | -1.02 | 23 | +0.404 | +0.370 | fail: OOS<=0 |
| orb15 | 5m | live24 | 154 | 34 | -0.067 | -0.164 | -0.100 | -0.162 | -3.96 | 50 | +0.260 | +0.197 | fail: OOS<=0 |
| orb15 | 10m | live24 | 122 | 28 | -0.097 | -0.188 | -0.128 | -0.186 | -3.65 | 42 | +0.266 | +0.209 | fail: OOS<=0 |
| orb15 | 15m | live24 | 140 | 31 | -0.094 | -0.187 | -0.126 | -0.185 | -3.61 | 43 | +0.328 | +0.268 | fail: OOS<=0 |
| orb15 | 5m | volatile20 | 136 | 50 | +0.035 | -0.028 | +0.016 | -0.019 | -0.30 | 36 | +0.156 | +0.116 | fail: OOS<=0 |
| orb15 | 10m | volatile20 | 111 | 48 | +0.059 | -0.001 | +0.042 | +0.010 | +0.17 | 30 | +0.154 | +0.117 | fail: OOS<=0 |
| orb15 | 15m | volatile20 | 113 | 46 | +0.090 | +0.026 | +0.071 | +0.037 | +0.58 | 31 | +0.193 | +0.155 | **PASS** (full) |
| gap_go | 5m | live24 | 51 | 37 | -0.024 | -0.125 | -0.059 | -0.124 | -0.85 | 25 | -0.014 | -0.088 | fail: holdout<=0, OOS<=0 |
| gap_go | 10m | live24 | 52 | 40 | -0.006 | -0.105 | -0.040 | -0.102 | -0.95 | 14 | +0.423 | +0.360 | fail: OOS<=0 |
| gap_go | 15m | live24 | 53 | 43 | +0.076 | -0.023 | +0.043 | -0.019 | -0.17 | 16 | -0.030 | -0.084 | fail: holdout<=0, OOS<=0 |
| gap_go | 5m | volatile20 | 64 | 45 | +0.147 | +0.058 | +0.118 | +0.065 | +0.31 | 37 | -0.226 | -0.290 | **PASS** (OOS bar only) |
| gap_go | 10m | volatile20 | 71 | 46 | +0.172 | +0.086 | +0.144 | +0.093 | +0.84 | 24 | +0.145 | +0.085 | **PASS** (full) |
| gap_go | 15m | volatile20 | 71 | 59 | +0.365 | +0.275 | +0.336 | +0.284 | +1.86 | 29 | -0.052 | -0.107 | **PASS** (OOS bar only) |
| gap_fade | 5m | live24 | 27 | 33 | -0.023 | -0.130 | -0.060 | -0.130 | -0.94 | 10 | -0.016 | -0.086 | fail: holdout<=0, OOS<=0, n<40 |
| gap_fade | 10m | live24 | 35 | 37 | +0.080 | -0.030 | +0.041 | -0.033 | -0.20 | 9 | -0.019 | -0.099 | fail: OOS<=0, holdout n<10, n<40 |
| gap_fade | 15m | live24 | 28 | 39 | +0.061 | -0.053 | +0.021 | -0.053 | -0.39 | 9 | +0.390 | +0.316 | fail: OOS<=0, holdout n<10, n<40 |
| gap_fade | 5m | volatile20 | 37 | 59 | +0.390 | +0.279 | +0.352 | +0.282 | +1.20 | 12 | -0.168 | -0.228 | fail: holdout<=0, n<40 |
| gap_fade | 10m | volatile20 | 36 | 36 | +0.163 | +0.063 | +0.129 | +0.065 | +0.74 | 11 | -0.241 | -0.306 | fail: holdout<=0, n<40 |
| gap_fade | 15m | volatile20 | 23 | 30 | +0.112 | -0.002 | +0.070 | +0.000 | +0.00 | 10 | -0.453 | -0.519 | fail: holdout<=0, OOS<=0, n<40 |
| rvol_momentum | 5m | live24 | 25 | 28 | -0.227 | -0.328 | -0.263 | -0.329 | -2.23 | 9 | +0.137 | +0.067 | fail: OOS<=0, holdout n<10, n<40 |
| rvol_momentum | 10m | live24 | 20 | 25 | -0.193 | -0.295 | -0.229 | -0.296 | -1.43 | 8 | +0.271 | +0.199 | fail: OOS<=0, holdout n<10, n<40 |
| rvol_momentum | 15m | live24 | 20 | 25 | -0.165 | -0.267 | -0.201 | -0.269 | -1.28 | 8 | +0.283 | +0.209 | fail: OOS<=0, holdout n<10, n<40 |
| rvol_momentum | 5m | volatile20 | 21 | 29 | -0.137 | -0.209 | -0.160 | -0.202 | -1.88 | 7 | +0.404 | +0.368 | fail: OOS<=0, holdout n<10, n<40 |
| rvol_momentum | 10m | volatile20 | 16 | 25 | -0.063 | -0.141 | -0.087 | -0.132 | -0.69 | 6 | +0.948 | +0.903 | fail: OOS<=0, holdout n<10, n<40 |
| rvol_momentum | 15m | volatile20 | 19 | 32 | -0.097 | -0.175 | -0.123 | -0.170 | -1.18 | 7 | +0.961 | +0.923 | fail: OOS<=0, holdout n<10, n<40 |
| vwap_reversion | 5m | live24 | 690 | 41 | -0.005 | -0.122 | -0.047 | -0.124 | -4.96 | 153 | -0.012 | -0.089 | fail: holdout<=0, OOS<=0 |
| vwap_reversion | 10m | live24 | 539 | 39 | +0.015 | -0.102 | -0.027 | -0.104 | -4.36 | 113 | -0.006 | -0.083 | fail: holdout<=0, OOS<=0 |
| vwap_reversion | 15m | live24 | 406 | 33 | -0.009 | -0.126 | -0.051 | -0.129 | -4.45 | 104 | -0.016 | -0.093 | fail: holdout<=0, OOS<=0 |
| vwap_reversion | 5m | volatile20 | 608 | 47 | +0.035 | -0.073 | -0.003 | -0.073 | -2.49 | 144 | -0.017 | -0.087 | fail: holdout<=0, OOS<=0 |
| vwap_reversion | 10m | volatile20 | 519 | 39 | -0.005 | -0.113 | -0.043 | -0.114 | -4.08 | 107 | -0.160 | -0.227 | fail: holdout<=0, OOS<=0 |
| vwap_reversion | 15m | volatile20 | 420 | 32 | -0.044 | -0.153 | -0.083 | -0.155 | -5.40 | 80 | -0.112 | -0.180 | fail: holdout<=0, OOS<=0 |
| late_day_momentum | 5m | live24 | 218 | 29 | -0.001 | -0.117 | -0.043 | -0.120 | -3.97 | 64 | -0.061 | -0.130 | fail: holdout<=0, OOS<=0 |
| late_day_momentum | 10m | live24 | 216 | 26 | -0.014 | -0.130 | -0.055 | -0.132 | -4.53 | 64 | -0.064 | -0.133 | fail: holdout<=0, OOS<=0 |
| late_day_momentum | 15m | live24 | 214 | 26 | -0.002 | -0.118 | -0.043 | -0.120 | -4.08 | 66 | -0.042 | -0.111 | fail: holdout<=0, OOS<=0 |
| late_day_momentum | 5m | volatile20 | 232 | 31 | -0.033 | -0.137 | -0.070 | -0.138 | -3.03 | 74 | -0.111 | -0.167 | fail: holdout<=0, OOS<=0 |
| late_day_momentum | 10m | volatile20 | 238 | 31 | -0.027 | -0.131 | -0.064 | -0.132 | -3.00 | 79 | -0.106 | -0.162 | fail: holdout<=0, OOS<=0 |
| late_day_momentum | 15m | volatile20 | 239 | 30 | -0.033 | -0.137 | -0.069 | -0.137 | -3.28 | 79 | -0.075 | -0.132 | fail: holdout<=0, OOS<=0 |

</details>

## Robustness: two years of 1h bars (2023-11-27 .. 2026-10-23)

The same rules on 1h bars (fills at the next bar's open, as production fills 1h keys; the ORB
range is the first hour). Production gives 1h keys no venue profiles; the "minute-bar gate"
column applies the minute-bar bar anyway (n >= 40 and > 0 under eToro, Alpaca and stress; holdout
n >= 10 and > 0 under all three). `late_day_momentum` does not fit hourly bars and was not run.

Long:

| strategy | universe | OOS trades | days | win % | gross R | eToro R | Alpaca R | stress R | stress day-t | holdout trades | holdout eToro R | holdout Alpaca R | holdout stress R | minute-bar gate |
|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|
| orb30 | live24 | 2111 | 555 | 40 | +0.000 | -0.079 | -0.026 | -0.073 | -5.27 | 31 | -0.049 | +0.009 | -0.045 | fail: OOS<=0, holdout<=0 |
| orb30 | volatile20 | 1627 | 505 | 46 | +0.016 | -0.033 | +0.003 | -0.020 | -1.32 | 20 | +0.194 | +0.234 | +0.208 | fail: OOS<=0 |
| gap_go | live24 | 1106 | 401 | 43 | -0.017 | -0.103 | -0.046 | -0.099 | -2.87 | 14 | +0.049 | +0.113 | +0.053 | fail: OOS<=0 |
| gap_go | volatile20 | 1780 | 461 | 45 | +0.027 | -0.041 | +0.006 | -0.032 | -1.04 | 8 | -0.058 | -0.011 | -0.053 | fail: OOS<=0, holdout n<10 |
| gap_fade | live24 | 357 | 190 | 45 | +0.041 | -0.062 | +0.005 | -0.062 | -1.11 | 11 | +0.215 | +0.289 | +0.219 | fail: OOS<=0 |
| gap_fade | volatile20 | 356 | 206 | 44 | +0.014 | -0.076 | -0.017 | -0.072 | -1.10 | 10 | -0.086 | -0.023 | -0.081 | fail: OOS<=0, holdout<=0 |
| rvol_momentum | live24 | 491 | 287 | 42 | -0.022 | -0.123 | -0.057 | -0.123 | -3.54 | 4 | -0.051 | +0.028 | -0.052 | fail: OOS<=0, holdout n<10 |
| rvol_momentum | volatile20 | 580 | 312 | 46 | +0.091 | +0.010 | +0.065 | +0.017 | +0.39 | 3 | +0.258 | +0.323 | +0.269 | fail: holdout n<10 |
| vwap_reversion | live24 | 2041 | 591 | 41 | -0.034 | -0.153 | -0.077 | -0.155 | -10.70 | 22 | -0.179 | -0.103 | -0.182 | fail: OOS<=0, holdout<=0 |
| vwap_reversion | volatile20 | 2795 | 633 | 40 | -0.079 | -0.192 | -0.120 | -0.195 | -11.43 | 26 | -0.262 | -0.186 | -0.264 | fail: OOS<=0, holdout<=0 |

<details><summary>Short (diagnostic)</summary>

| strategy | universe | OOS trades | days | win % | gross R | eToro R | Alpaca R | stress R | stress day-t | holdout trades | holdout eToro R | holdout Alpaca R | holdout stress R | minute-bar gate |
|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|
| orb30 | live24 | 1914 | 533 | 39 | -0.009 | -0.086 | -0.034 | -0.079 | -4.13 | 27 | +0.138 | +0.196 | +0.146 | fail: OOS<=0 |
| orb30 | volatile20 | 1443 | 464 | 45 | +0.021 | -0.026 | +0.009 | -0.012 | -0.61 | 20 | +0.183 | +0.226 | +0.197 | fail: OOS<=0 |
| gap_go | live24 | 910 | 332 | 37 | -0.068 | -0.151 | -0.096 | -0.147 | -2.88 | 22 | +0.411 | +0.475 | +0.417 | fail: OOS<=0 |
| gap_go | volatile20 | 1443 | 401 | 42 | -0.028 | -0.092 | -0.047 | -0.084 | -2.08 | 36 | +0.162 | +0.210 | +0.171 | fail: OOS<=0 |
| gap_fade | live24 | 467 | 235 | 37 | -0.003 | -0.108 | -0.040 | -0.109 | -1.60 | 6 | -0.760 | -0.704 | -0.770 | fail: OOS<=0, holdout n<10 |
| gap_fade | volatile20 | 444 | 227 | 43 | +0.070 | -0.023 | +0.038 | -0.019 | -0.29 | 7 | -0.682 | -0.632 | -0.690 | fail: OOS<=0, holdout n<10 |
| rvol_momentum | live24 | 472 | 243 | 41 | +0.006 | -0.098 | -0.030 | -0.097 | -2.56 | 8 | +0.004 | +0.080 | +0.005 | fail: OOS<=0, holdout n<10 |
| rvol_momentum | volatile20 | 459 | 263 | 41 | +0.031 | -0.042 | +0.008 | -0.035 | -0.76 | 7 | +0.391 | +0.446 | +0.406 | fail: OOS<=0, holdout n<10 |
| vwap_reversion | live24 | 2087 | 600 | 38 | -0.033 | -0.152 | -0.075 | -0.154 | -11.97 | 38 | -0.245 | -0.172 | -0.249 | fail: OOS<=0, holdout<=0 |
| vwap_reversion | volatile20 | 2868 | 632 | 39 | -0.074 | -0.189 | -0.116 | -0.192 | -13.16 | 30 | -0.196 | -0.123 | -0.200 | fail: OOS<=0, holdout<=0 |

</details>

Over ~150 folds per symbol the samples are large (350-2,800 trades a cell) and every cell but
`rvol_momentum` volatile20 is negative after eToro and stress costs; that one is +0.017R under
stress with day-t +0.39 (p = 0.35) and 3 holdout trades. (Under production's own 1h rules --
eToro costs only -- option B's OOS bar would admit it as a paper-only key: +0.010R on 580
trades. On the live24 universe the same rule is -0.123R, day-t -3.5.)

## Calibration: production's own intraday keys on the same data

Four live registry strategies (their spec's default kwargs, long) run through the same harness
on the live24 data. The picture matches production's own Stage 1 evidence (best pooled OOS
R: 5m -0.060, 10m +0.002 for `rsi_reversal`, 15m -0.068). Here all ten keys fail with eToro R
-0.05..-0.14: best 5m `vwap_reclaim` -0.070, best 15m `gap_continuation_fade` -0.051, and
`rsi_reversal` 10m -0.097 instead of +0.002 (a different feed and window: yfinance 60 days vs
Alpaca IEX 120 days). The harness is neither clearly kinder nor clearly harsher than
production, and the native candidates do no better than the registry's on live24.

| strategy | tf | universe | OOS trades | win % | gross R | eToro R | Alpaca R | stress R | stress day-t | holdout trades | holdout Alpaca R | holdout stress R | production gate |
|---|---|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|
| live:gap_continuation_fade | 5m | live24 | 136 | 37 | -0.027 | -0.129 | -0.062 | -0.127 | -1.86 | 40 | -0.104 | -0.173 | fail: holdout<=0, OOS<=0 |
| live:gap_continuation_fade | 15m | live24 | 133 | 44 | +0.040 | -0.051 | +0.009 | -0.048 | -0.62 | 38 | +0.108 | +0.050 | fail: OOS<=0 |
| live:opening_range_breakout_retest | 5m | live24 | 496 | 37 | -0.052 | -0.141 | -0.082 | -0.138 | -4.89 | 102 | -0.018 | -0.073 | fail: holdout<=0, OOS<=0 |
| live:opening_range_breakout_retest | 15m | live24 | 251 | 31 | -0.037 | -0.117 | -0.063 | -0.111 | -3.84 | 55 | +0.033 | -0.019 | fail: holdout<=0, OOS<=0 |
| live:rsi_reversal | 5m | live24 | 276 | 36 | +0.028 | -0.088 | -0.013 | -0.090 | -1.98 | 82 | -0.046 | -0.121 | fail: holdout<=0, OOS<=0 |
| live:rsi_reversal | 10m | live24 | 160 | 38 | +0.018 | -0.097 | -0.023 | -0.099 | -2.41 | 51 | -0.133 | -0.203 | fail: holdout<=0, OOS<=0 |
| live:rsi_reversal | 15m | live24 | 120 | 32 | -0.021 | -0.132 | -0.060 | -0.134 | -2.82 | 25 | -0.008 | -0.077 | fail: holdout<=0, OOS<=0 |
| live:vwap_reclaim | 5m | live24 | 796 | 35 | +0.038 | -0.070 | +0.000 | -0.070 | -2.89 | 143 | -0.038 | -0.107 | fail: holdout<=0, OOS<=0 |
| live:vwap_reclaim | 10m | live24 | 557 | 37 | +0.003 | -0.096 | -0.031 | -0.094 | -3.13 | 114 | -0.066 | -0.129 | fail: holdout<=0, OOS<=0 |
| live:vwap_reclaim | 15m | live24 | 388 | 37 | -0.009 | -0.099 | -0.039 | -0.095 | -3.03 | 89 | -0.003 | -0.065 | fail: holdout<=0, OOS<=0 |

## Multiple testing and sample size

* **Cells tried:** 42 long cells at 5m/10m/15m on the two universes, 3 more on etf3, 10 long 1h
  cells: **55 long cells**; plus 52 short diagnostic cells. The 5m/10m/15m cells of one rule
  share most of their trades, so the effective number of independent tests is nearer 15-20,
  but even one test fails here.
* **Best long result:** `rvol_momentum` 15m live24, stress day-t +0.77, one-sided p = 0.23;
  Holm- and BH-adjusted p = 1.0 over the 42 cells. A one-in-45 significant result needs
  t ~ +3.0.
* **What the 40-trade bar can see.** Per-trade R has a standard deviation of 0.4-0.9R for these
  rules, so 150 trades detect only an edge of ~0.06-0.14R at t = 2, and a +0.05R edge needs
  ~200-1,200 independent trades (more once same-day trades are clustered). A rule with zero
  gross edge and 40-100 trades passes the whole gate by luck roughly 1-5% of the time, so 45
  cells could be expected to throw up one or two passes; 2 of the 42 short cells did. The gate
  is a filter, not a significance test: a pass on 60 days would still need months of
  out-of-sample accrual before it meant an edge.

## Recommendation

1. **Register nothing.** No candidate clears the production bar under both cost profiles, and
   none comes close to statistical significance. Registering one as a "backtest-only" key is
   not possible without a research-only flag in the registry, and adding such a flag would
   change the live scan path.
2. **Do not tune these rules on this data.** Every family was positive in some cell; picking the
   best cell and refining it would be fitting noise (45 tries, best t = +0.77).
3. **If intraday research continues, change the sample, not the rules:** wait for production's
   own 120-day Alpaca walk-forward to accumulate more history, or test only pre-registered
   variants of `rvol_momentum` and volatile-name ORB on a fresh, longer window (it would need
   ~1,000 trades to show a +0.05R stress edge). Both read a multi-session volume baseline,
   which a live 5m scan's 320 bars barely hold (see below).
4. The cost gap is the edge gap: ORB's gross edge on high-beta names (+0.02..+0.06R) sits
   between Alpaca's ~0.03R and the stress/eToro ~0.08R. An intraday plan only becomes plausible
   with fills demonstrably at or better than the Alpaca profile -- which the IEX-quote stress
   case exists to doubt -- and with eToro's fixed fees kept off it.

## Things the study noticed about the production evidence path (not changed; worth a look)

1. **Extended-hours bars and the session flatten.** `AlpacaClient.get_bars` requests stock bars
   with no session filter and nothing downstream drops pre/post-market bars. If the IEX 5m/15m
   frames carry them, `last_bar_of_session` marks the last bar of the New York *date* -- a
   post-market bar -- so a backtested intraday position would be flattened near 20:00 ET, after
   its stop and target were checked against thin post-market bars, while the live bot exits at
   15:50. One query settles it: the `exit_time` of `session_close` exits in a persisted 5m
   `walk_forward_oos` row. The candidates here carry their own 15:50 time stop, so they are
   immune either way.
2. **Fold-boundary "previous session".** Each fold's frame is warm-up + test with the embargo
   day removed, so on a fold's first test day the bar before the open is two sessions old. A
   rule that reads the previous bar's close as "yesterday" (the registry's
   `gap_continuation_fade` computes its gap that way) sees a two-day gap on one day of every
   test week.
3. **Live history length.** A live 5m scan fetches 320 bars (`bars_for_timeframe`): about four
   regular sessions, or under two if extended-hours bars are included. Anything that needs a
   multi-session baseline (time-of-day relative volume, opening-range volume against its
   average) would need a longer fetch to run live as it was backtested.

## Reproduce

```
# data (keyless; untrusted, kept outside the repo)
yfinance.download(sym, period="60d", interval="5m", prepost=False, auto_adjust=False)
yfinance.download(sym, period="730d", interval="1h", prepost=False, auto_adjust=False)
# -> DATA/5m/<SYM>.parquet, DATA/1h/<SYM>.parquet (timestamp UTC, open, high, low, close, volume)

python -I scripts/research_intraday_study.py run --data-dir DATA --out main.jsonl --workers 4
python -I scripts/research_intraday_study.py run --data-dir DATA --out robust_1h.jsonl --timeframes 1h \
    --variants research_orb30,research_gap_go,research_gap_fade,research_rvol_momentum,research_vwap_reversion --workers 4
python -I scripts/research_intraday_study.py run --data-dir DATA --out calibration.jsonl --directions long \
    --universes live24 --variants live:rsi_reversal,live:vwap_reclaim,live:opening_range_breakout_retest,live:gap_continuation_fade --workers 4
python -I scripts/research_intraday_study.py report --results main.jsonl --out main.md   # also writes main.json
```

The 60-day grid is 1,722 symbol runs (about 45 minutes on 4 cores); the 1h grid 410.
